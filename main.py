import os
import re
import json
import html
import time
import threading
from pathlib import Path
from datetime import datetime, timedelta, time as dt_time, timezone
from zoneinfo import ZoneInfo
from typing import Any

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query


load_dotenv()

APP_NAME = "worldcup-telegram-reporter"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
APP_SECRET = os.getenv("APP_SECRET", "")

TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")

REPORT_WINDOW_START = os.getenv("REPORT_WINDOW_START", "09:30")
REPORT_WINDOW_END = os.getenv("REPORT_WINDOW_END", "10:30")

OPENFOOTBALL_URL = os.getenv(
    "OPENFOOTBALL_URL",
    "https://raw.githubusercontent.com/openfootball/worldcup.json/master/2026/worldcup.json",
)

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = DATA_DIR / "state.json"

RUN_LOCK = threading.Lock()

app = FastAPI(title="World Cup Telegram Reporter")


def require_env() -> None:
    missing = []

    for key, value in {
        "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
        "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
        "APP_SECRET": APP_SECRET,
    }.items():
        if not value:
            missing.append(key)

    if missing:
        raise RuntimeError(
            f"Missing required environment variables: {', '.join(missing)}"
        )


def check_secret(secret: str) -> None:
    if not APP_SECRET:
        raise HTTPException(status_code=500, detail="APP_SECRET is not configured")

    if secret != APP_SECRET:
        raise HTTPException(status_code=403, detail="Invalid secret")


def get_tz() -> ZoneInfo:
    return ZoneInfo(TIMEZONE)


def now_local() -> datetime:
    return datetime.now(get_tz())


def yesterday_local_date_str() -> str:
    return (now_local().date() - timedelta(days=1)).isoformat()


def parse_hhmm(value: str) -> dt_time:
    try:
        hour_str, minute_str = value.strip().split(":", 1)
        return dt_time(hour=int(hour_str), minute=int(minute_str))
    except Exception as exc:
        raise RuntimeError(f"Invalid HH:MM time value: {value}") from exc


def is_inside_report_window(current: datetime | None = None) -> bool:
    current = current or now_local()

    start = parse_hhmm(REPORT_WINDOW_START)
    end = parse_hhmm(REPORT_WINDOW_END)

    current_time = current.time().replace(second=0, microsecond=0)

    if start <= end:
        return start <= current_time <= end

    return current_time >= start or current_time <= end


def load_state() -> dict[str, Any]:
    default_state = {
        "sent": {},
        "errors": [],
    }

    if not STATE_FILE.exists():
        return default_state

    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return default_state

    if not isinstance(data, dict):
        return default_state

    data.setdefault("sent", {})
    data.setdefault("errors", [])

    if not isinstance(data["sent"], dict):
        data["sent"] = {}

    if not isinstance(data["errors"], list):
        data["errors"] = []

    return data


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def mark_sent(date_str: str) -> None:
    state = load_state()
    state["sent"][date_str] = now_local().isoformat()
    save_state(state)


def mark_error(message: str) -> None:
    state = load_state()
    state["errors"].append(
        {
            "time": now_local().isoformat(),
            "message": message[:700],
        }
    )
    state["errors"] = state["errors"][-20:]
    save_state(state)


def already_sent(date_str: str) -> bool:
    return date_str in load_state().get("sent", {})


def telegram_send(text: str) -> None:
    require_env()

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    for part in split_text(text, max_len=3900):
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": part,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

        response = requests.post(url, json=payload, timeout=30)
        response.raise_for_status()

        data = response.json()

        if not data.get("ok"):
            raise RuntimeError(f"Telegram error: {data}")

        time.sleep(0.5)


def split_text(text: str, max_len: int = 3900) -> list[str]:
    parts: list[str] = []
    current = ""

    for block in text.split("\n\n"):
        if len(block) > max_len:
            if current:
                parts.append(current)
                current = ""

            parts.extend(
                block[i:i + max_len]
                for i in range(0, len(block), max_len)
            )
            continue

        if len(current) + len(block) + 2 <= max_len:
            current += ("\n\n" if current else "") + block
        else:
            if current:
                parts.append(current)
            current = block

    if current:
        parts.append(current)

    return parts


def load_worldcup_data() -> dict[str, Any]:
    response = requests.get(OPENFOOTBALL_URL, timeout=30)
    response.raise_for_status()
    return response.json()


def safe_text(value: Any, fallback: str = "") -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()

    return fallback


def get_matches(data: dict[str, Any]) -> list[dict[str, Any]]:
    matches = data.get("matches", [])
    return matches if isinstance(matches, list) else []


def get_score_ft(match: dict[str, Any]) -> tuple[int | None, int | None]:
    score = match.get("score") or {}
    ft = score.get("ft")

    if (
        isinstance(ft, list)
        and len(ft) >= 2
        and ft[0] is not None
        and ft[1] is not None
    ):
        return int(ft[0]), int(ft[1])

    return None, None


def is_finished(match: dict[str, Any]) -> bool:
    home_score, away_score = get_score_ft(match)
    return home_score is not None and away_score is not None


def parse_match_datetime(match: dict[str, Any]) -> datetime:
    date_value = safe_text(match.get("date"))
    time_value = safe_text(match.get("time"), "00:00 UTC")

    if not date_value:
        raise ValueError("Match has no date")

    date_obj = datetime.fromisoformat(date_value).date()

    # Примеры:
    # "13:00 UTC-6"
    # "20:00 UTC-5"
    # "18:00 UTC"
    pattern = r"^(\d{1,2}):(\d{2})(?:\s*UTC\s*([+-]\d{1,2}))?"
    match_time = re.search(pattern, time_value)

    if match_time:
        hour = int(match_time.group(1))
        minute = int(match_time.group(2))
        offset_raw = match_time.group(3)

        if offset_raw:
            offset_hours = int(offset_raw)
        else:
            offset_hours = 0

        source_tz = timezone(timedelta(hours=offset_hours))
        dt = datetime(
            date_obj.year,
            date_obj.month,
            date_obj.day,
            hour,
            minute,
            tzinfo=source_tz,
        )
    else:
        dt = datetime(
            date_obj.year,
            date_obj.month,
            date_obj.day,
            0,
            0,
            tzinfo=timezone.utc,
        )

    return dt.astimezone(get_tz())


def match_moscow_date_str(match: dict[str, Any]) -> str:
    try:
        return parse_match_datetime(match).date().isoformat()
    except Exception:
        return safe_text(match.get("date"))


def format_goal(goal: dict[str, Any], team: str) -> str:
    name = safe_text(goal.get("name"), "Неизвестный игрок")
    minute = safe_text(goal.get("minute"), "?")

    detail_parts = []

    goal_type = safe_text(goal.get("type")).lower()
    if "pen" in goal_type:
        detail_parts.append("пенальти")
    if "own" in goal_type or "og" in goal_type:
        detail_parts.append("автогол")

    detail = f" — {', '.join(detail_parts)}" if detail_parts else ""

    return f"{minute}’ — {name} ({team}){detail}"


def get_goals(match: dict[str, Any]) -> list[str]:
    team1 = safe_text(match.get("team1"), "Команда 1")
    team2 = safe_text(match.get("team2"), "Команда 2")

    goals = []

    for goal in match.get("goals1") or []:
        if isinstance(goal, dict):
            goals.append(format_goal(goal, team1))

    for goal in match.get("goals2") or []:
        if isinstance(goal, dict):
            goals.append(format_goal(goal, team2))

    def minute_sort_key(line: str) -> int:
        found = re.search(r"(\d+)", line)
        return int(found.group(1)) if found else 999

    return sorted(goals, key=minute_sort_key)


def get_red_cards(match: dict[str, Any]) -> list[str]:
    """
    OpenFootball обычно не дает полноценную детализацию карточек.
    Но оставляем поддержку на случай, если в JSON появятся такие поля.
    """
    possible_fields = [
        "redcards",
        "red_cards",
        "cards",
        "bookings",
        "sendoffs",
        "dismissals",
    ]

    result = []

    for field in possible_fields:
        items = match.get(field)

        if not isinstance(items, list):
            continue

        for item in items:
            if not isinstance(item, dict):
                continue

            raw = json.dumps(item, ensure_ascii=False).lower()

            if "red" not in raw and "крас" not in raw:
                continue

            minute = safe_text(item.get("minute"), "?")
            name = safe_text(item.get("name") or item.get("player"), "Игрок")
            team = safe_text(item.get("team"), "Команда")

            result.append(f"{minute}’ — {name} ({team})")

    return result


def format_match(match: dict[str, Any]) -> str:
    team1 = safe_text(match.get("team1"), "Команда 1")
    team2 = safe_text(match.get("team2"), "Команда 2")

    score1, score2 = get_score_ft(match)

    score_text = f"{score1}:{score2}" if score1 is not None else "?:?"
    round_name = safe_text(match.get("round"))
    group_name = safe_text(match.get("group"))
    ground = safe_text(match.get("ground"))

    lines = [
        f"<b>{html.escape(team1)} {html.escape(score_text)} {html.escape(team2)}</b>"
    ]

    details = []

    if group_name:
        details.append(group_name)

    if round_name:
        details.append(round_name)

    if ground:
        details.append(ground)

    if details:
        lines.append(" / ".join(html.escape(x) for x in details))

    score = match.get("score") or {}

    et = score.get("et")
    if isinstance(et, list) and len(et) >= 2:
        lines.append(f"После дополнительного времени: {et[0]}:{et[1]}")

    penalties = score.get("p")
    if isinstance(penalties, list) and len(penalties) >= 2:
        lines.append(f"По пенальти: {penalties[0]}:{penalties[1]}")

    goals = get_goals(match)

    if goals:
        lines.append("Голы:")
        lines.extend(f"• {html.escape(goal)}" for goal in goals)
    else:
        if score1 == 0 and score2 == 0:
            lines.append("Голы: нет.")
        else:
            lines.append("Голы: авторы голов пока не указаны в источнике.")

    red_cards = get_red_cards(match)

    if red_cards:
        lines.append("Удаления:")
        lines.extend(f"• {html.escape(card)}" for card in red_cards)
    else:
        lines.append("Удаления: нет данных в бесплатном источнике.")

    return "\n".join(lines)


def build_highlights(matches: list[dict[str, Any]]) -> list[str]:
    highlights = []

    for match in matches:
        team1 = safe_text(match.get("team1"), "Команда 1")
        team2 = safe_text(match.get("team2"), "Команда 2")

        score1, score2 = get_score_ft(match)

        if score1 is not None and score2 is not None:
            diff = abs(score1 - score2)

            if diff >= 3:
                winner = team1 if score1 > score2 else team2
                highlights.append(
                    f"{winner} одержала крупную победу в матче {team1} — {team2}."
                )

            if score1 == score2:
                highlights.append(f"{team1} и {team2} сыграли вничью.")

        for goal in get_goals(match):
            found = re.search(r"(\d+)", goal)

            if found and int(found.group(1)) >= 85:
                highlights.append(f"Поздний гол: {goal}.")

    if not highlights:
        highlights.append("День прошел без разгромов и поздних голов.")

    return highlights[:8]


def build_bright_players(matches: list[dict[str, Any]]) -> list[str]:
    scorers: dict[str, int] = {}

    for match in matches:
        for field in ["goals1", "goals2"]:
            for goal in match.get(field) or []:
                if not isinstance(goal, dict):
                    continue

                name = safe_text(goal.get("name"))

                if not name:
                    continue

                goal_type = safe_text(goal.get("type")).lower()

                if "own" in goal_type or "og" in goal_type:
                    continue

                scorers[name] = scorers.get(name, 0) + 1

    multi = [
        f"{name} — {count} гол(а)"
        for name, count in scorers.items()
        if count >= 2
    ]

    if multi:
        return sorted(multi)[:5]

    single = [
        f"{name} — отметился голом"
        for name, count in scorers.items()
        if count == 1
    ]

    if single:
        return sorted(single)[:5]

    return ["Ярких индивидуальных всплесков по голам не было."]


def empty_table_row(team: str) -> dict[str, Any]:
    return {
        "team": team,
        "played": 0,
        "wins": 0,
        "draws": 0,
        "losses": 0,
        "gf": 0,
        "ga": 0,
        "gd": 0,
        "points": 0,
    }


def add_group_match(
    table: dict[str, dict[str, Any]],
    team1: str,
    team2: str,
    score1: int,
    score2: int,
) -> None:
    table.setdefault(team1, empty_table_row(team1))
    table.setdefault(team2, empty_table_row(team2))

    table[team1]["played"] += 1
    table[team2]["played"] += 1

    table[team1]["gf"] += score1
    table[team1]["ga"] += score2

    table[team2]["gf"] += score2
    table[team2]["ga"] += score1

    if score1 > score2:
        table[team1]["wins"] += 1
        table[team2]["losses"] += 1
        table[team1]["points"] += 3
    elif score1 < score2:
        table[team2]["wins"] += 1
        table[team1]["losses"] += 1
        table[team2]["points"] += 3
    else:
        table[team1]["draws"] += 1
        table[team2]["draws"] += 1
        table[team1]["points"] += 1
        table[team2]["points"] += 1

    table[team1]["gd"] = table[team1]["gf"] - table[team1]["ga"]
    table[team2]["gd"] = table[team2]["gf"] - table[team2]["ga"]


def format_group_standings(
    all_matches: list[dict[str, Any]],
    target_matches: list[dict[str, Any]],
    target_date_str: str,
) -> str:
    relevant_groups = {
        safe_text(match.get("group"))
        for match in target_matches
        if safe_text(match.get("group"))
    }

    if not relevant_groups:
        return "Таблица: для этой стадии групповая таблица не применяется или группа не указана."

    group_tables: dict[str, dict[str, dict[str, Any]]] = {
        group: {} for group in relevant_groups
    }

    for match in all_matches:
        group = safe_text(match.get("group"))

        if group not in relevant_groups:
            continue

        if not is_finished(match):
            continue

        match_date = match_moscow_date_str(match)

        if match_date > target_date_str:
            continue

        team1 = safe_text(match.get("team1"), "Команда 1")
        team2 = safe_text(match.get("team2"), "Команда 2")
        score1, score2 = get_score_ft(match)

        if score1 is None or score2 is None:
            continue

        add_group_match(group_tables[group], team1, team2, score1, score2)

    blocks = []

    for group in sorted(group_tables):
        rows = list(group_tables[group].values())

        rows.sort(
            key=lambda row: (
                row["points"],
                row["gd"],
                row["gf"],
                row["team"],
            ),
            reverse=True,
        )

        lines = [f"<b>{html.escape(group)}</b>"]

        for i, row in enumerate(rows, start=1):
            lines.append(
                f"{i}. {html.escape(row['team'])} — "
                f"{row['points']} очк., "
                f"матчей: {row['played']}, "
                f"разница: {row['gd']}"
            )

        blocks.append("\n".join(lines))

    return "\n\n".join(blocks) if blocks else "Таблица: данных пока нет."


def format_upcoming(all_matches: list[dict[str, Any]]) -> str:
    current = now_local()
    upcoming = []

    for match in all_matches:
        if is_finished(match):
            continue

        try:
            dt = parse_match_datetime(match)
        except Exception:
            continue

        if dt < current:
            continue

        team1 = safe_text(match.get("team1"), "Команда 1")
        team2 = safe_text(match.get("team2"), "Команда 2")

        upcoming.append(
            {
                "dt": dt,
                "text": (
                    f"• {dt.strftime('%d.%m %H:%M')} — "
                    f"{html.escape(team1)} vs {html.escape(team2)}"
                ),
            }
        )

    upcoming.sort(key=lambda item: item["dt"])

    if not upcoming:
        return "Ближайшие матчи не найдены."

    return "\n".join(item["text"] for item in upcoming[:8])


def build_report(date_str: str) -> str | None:
    data = load_worldcup_data()
    all_matches = get_matches(data)

    target_matches = [
        match
        for match in all_matches
        if is_finished(match)
        and match_moscow_date_str(match) == date_str
    ]

    if not target_matches:
        return None

    target_matches.sort(key=parse_match_datetime)

    date_title = datetime.fromisoformat(date_str).strftime("%d.%m.%Y")

    match_blocks = [format_match(match) for match in target_matches]

    highlights = build_highlights(target_matches)
    bright_players = build_bright_players(target_matches)

    sections = [
        f"🏆 <b>Итоги дня ЧМ-2026 — {date_title}</b>",
        "<b>Результаты матчей</b>\n\n" + "\n\n".join(match_blocks),
        "<b>Главные моменты</b>\n" + "\n".join(
            f"• {html.escape(item)}" for item in highlights
        ),
        "<b>Влияние на турнир</b>\n"
        + format_group_standings(all_matches, target_matches, date_str),
        "<b>Новые яркие игроки</b>\n" + "\n".join(
            f"• {html.escape(item)}" for item in bright_players
        ),
        "<b>Что посмотреть дальше</b>\n" + format_upcoming(all_matches),
    ]

    return "\n\n".join(sections)


def run_report_job(date_str: str, force: bool = False, source: str = "manual") -> None:
    if not RUN_LOCK.acquire(blocking=False):
        print("Report job is already running. Skip new launch.")
        return

    try:
        print(f"Report job started. source={source}, date={date_str}, force={force}")

        if already_sent(date_str) and not force:
            print(f"Report for {date_str} already sent.")
            return

        try:
            report = build_report(date_str)
        except Exception as exc:
            message = f"Report build error for {date_str}: {exc}"
            print(message)
            mark_error(message)
            return

        if not report:
            print(f"No finished World Cup matches for {date_str}.")
            return

        try:
            telegram_send(report)
        except Exception as exc:
            message = f"Telegram send error for {date_str}: {exc}"
            print(message)
            mark_error(message)
            return

        mark_sent(date_str)
        print(f"Report for {date_str} sent successfully.")

    finally:
        RUN_LOCK.release()


def start_report_job(
    date_str: str,
    force: bool = False,
    source: str = "manual",
) -> dict[str, Any]:
    if RUN_LOCK.locked():
        return {
            "status": "busy",
            "message": "Report job is already running",
        }

    if already_sent(date_str) and not force:
        return {
            "status": "already_sent",
            "date": date_str,
        }

    thread = threading.Thread(
        target=run_report_job,
        args=(date_str, force, source),
        daemon=True,
    )
    thread.start()

    return {
        "status": "scheduled",
        "date": date_str,
        "force": force,
        "source": source,
    }


@app.on_event("startup")
def on_startup() -> None:
    print(f"{APP_NAME} started")
    print(f"Timezone: {TIMEZONE}")
    print(f"Report window: {REPORT_WINDOW_START}-{REPORT_WINDOW_END}")
    print(f"OpenFootball URL: {OPENFOOTBALL_URL}")

    if os.getenv("SEND_TEST_ON_START", "0") == "1":
        try:
            telegram_send(
                "✅ Бот итогов ЧМ-2026 запущен.\n"
                f"Часовой пояс: {html.escape(TIMEZONE)}\n"
                f"Окно проверки: "
                f"{html.escape(REPORT_WINDOW_START)}-{html.escape(REPORT_WINDOW_END)}"
            )
        except Exception as exc:
            print("Startup Telegram test failed:", exc)


@app.api_route("/", methods=["GET", "HEAD"])
def root() -> dict[str, Any]:
    return {
        "service": APP_NAME,
        "status": "ok",
        "health": "/health",
        "tick": "/tick?secret=...",
    }


@app.api_route("/health", methods=["GET", "HEAD"])
def health() -> dict[str, Any]:
    state = load_state()

    return {
        "service": APP_NAME,
        "status": "ok",
        "now": now_local().isoformat(),
        "timezone": TIMEZONE,
        "report_window": f"{REPORT_WINDOW_START}-{REPORT_WINDOW_END}",
        "inside_report_window": is_inside_report_window(),
        "sent_dates": sorted(state.get("sent", {}).keys()),
        "job_running": RUN_LOCK.locked(),
        "data_source": "openfootball/worldcup.json",
    }


@app.api_route("/tick", methods=["GET", "HEAD"])
def tick(secret: str = Query(default="")) -> dict[str, Any]:
    check_secret(secret)

    current = now_local()
    date_str = yesterday_local_date_str()

    if not is_inside_report_window(current):
        return {
            "status": "skip",
            "reason": "outside_report_window",
            "now": current.isoformat(),
            "report_window": f"{REPORT_WINDOW_START}-{REPORT_WINDOW_END}",
            "target_date": date_str,
        }

    result = start_report_job(
        date_str=date_str,
        force=False,
        source="uptimerobot_tick",
    )

    return {
        **result,
        "now": current.isoformat(),
        "report_window": f"{REPORT_WINDOW_START}-{REPORT_WINDOW_END}",
    }


@app.api_route("/run-report", methods=["GET", "HEAD"])
def run_report(
    secret: str = Query(default=""),
    date: str | None = Query(
        default=None,
        description="YYYY-MM-DD. Empty = yesterday in configured timezone.",
    ),
    force: bool = Query(default=False),
) -> dict[str, Any]:
    check_secret(secret)

    date_str = date or yesterday_local_date_str()

    try:
        datetime.fromisoformat(date_str)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="Invalid date format. Use YYYY-MM-DD",
        ) from exc

    return start_report_job(
        date_str=date_str,
        force=force,
        source="manual_run",
    )


@app.api_route("/test-telegram", methods=["GET", "HEAD"])
def test_telegram(secret: str = Query(default="")) -> dict[str, Any]:
    check_secret(secret)

    try:
        telegram_send(
            "✅ Тестовое сообщение. Бот итогов ЧМ-2026 работает.\n"
            f"Время: "
            f"{html.escape(now_local().strftime('%d.%m.%Y %H:%M:%S'))} "
            f"{html.escape(TIMEZONE)}"
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {
        "status": "sent",
    }