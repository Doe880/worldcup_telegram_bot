import os
import json
import html
import time
import threading
from pathlib import Path
from datetime import datetime, timedelta, time as dt_time
from zoneinfo import ZoneInfo
from typing import Any

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query


load_dotenv()

APP_NAME = "worldcup-telegram-reporter"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
FOOTBALL_API_KEY = os.getenv("FOOTBALL_API_KEY", "")
APP_SECRET = os.getenv("APP_SECRET", "")

TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")
LEAGUE_ID = int(os.getenv("LEAGUE_ID", "1"))
SEASON = int(os.getenv("SEASON", "2026"))

REPORT_WINDOW_START = os.getenv("REPORT_WINDOW_START", "09:30")
REPORT_WINDOW_END = os.getenv("REPORT_WINDOW_END", "10:30")

BASE_URL = os.getenv("FOOTBALL_API_BASE_URL", "https://v3.football.api-sports.io")
FINISHED_STATUSES = {"FT", "AET", "PEN"}

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
        "FOOTBALL_API_KEY": FOOTBALL_API_KEY,
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
        "no_matches": {},
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

    for key in default_state:
        data.setdefault(key, default_state[key])

    if not isinstance(data["sent"], dict):
        data["sent"] = {}

    if not isinstance(data["no_matches"], dict):
        data["no_matches"] = {}

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
    state["no_matches"].pop(date_str, None)
    save_state(state)


def mark_no_matches(date_str: str) -> None:
    state = load_state()
    state["no_matches"][date_str] = now_local().isoformat()
    save_state(state)


def mark_error(message: str) -> None:
    state = load_state()
    state["errors"].append(
        {
            "time": now_local().isoformat(),
            "message": message[:500],
        }
    )
    state["errors"] = state["errors"][-20:]
    save_state(state)


def already_sent(date_str: str) -> bool:
    return date_str in load_state().get("sent", {})


def already_checked_no_matches(date_str: str) -> bool:
    return date_str in load_state().get("no_matches", {})


def api_get(endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    require_env()

    url = f"{BASE_URL}{endpoint}"
    headers = {
        "x-apisports-key": FOOTBALL_API_KEY,
    }

    response = requests.get(
        url,
        headers=headers,
        params=params or {},
        timeout=30,
    )
    response.raise_for_status()

    data = response.json()
    errors = data.get("errors")

    if errors:
        raise RuntimeError(f"API-Football error: {errors}")

    return data


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


def get_fixtures_by_date(date_str: str) -> list[dict[str, Any]]:
    data = api_get(
        "/fixtures",
        {
            "date": date_str,
            "league": LEAGUE_ID,
            "season": SEASON,
            "timezone": TIMEZONE,
        },
    )

    return data.get("response", [])


def get_finished_matches(date_str: str) -> list[dict[str, Any]]:
    fixtures = get_fixtures_by_date(date_str)

    return [
        fixture
        for fixture in fixtures
        if fixture.get("fixture", {}).get("status", {}).get("short")
        in FINISHED_STATUSES
    ]


def get_fixture_events(fixture_id: int) -> list[dict[str, Any]]:
    data = api_get(
        "/fixtures/events",
        {
            "fixture": fixture_id,
        },
    )

    return data.get("response", [])


def get_standings() -> list[list[dict[str, Any]]]:
    data = api_get(
        "/standings",
        {
            "league": LEAGUE_ID,
            "season": SEASON,
        },
    )

    response = data.get("response", [])

    if not response:
        return []

    return response[0].get("league", {}).get("standings", [])


def get_upcoming_matches(limit: int = 10) -> list[dict[str, Any]]:
    data = api_get(
        "/fixtures",
        {
            "league": LEAGUE_ID,
            "season": SEASON,
            "next": limit,
            "timezone": TIMEZONE,
        },
    )

    return data.get("response", [])


def event_minute(event: dict[str, Any]) -> str:
    time_data = event.get("time", {}) or {}
    elapsed = time_data.get("elapsed")
    extra = time_data.get("extra")

    if elapsed is None:
        return "?"

    if extra:
        return f"{elapsed}+{extra}’"

    return f"{elapsed}’"


def safe_name(value: Any, fallback: str) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()

    return fallback


def format_goals(events: list[dict[str, Any]]) -> list[str]:
    result = []

    for event in events:
        if event.get("type") != "Goal":
            continue

        detail = event.get("detail") or ""

        if detail == "Missed Penalty":
            continue

        player = safe_name(
            (event.get("player") or {}).get("name"),
            "Неизвестный игрок",
        )
        team = safe_name(
            (event.get("team") or {}).get("name"),
            "Команда",
        )
        assist = (event.get("assist") or {}).get("name")

        line = f"{event_minute(event)} — {player} ({team})"

        if detail == "Penalty":
            line += " — пенальти"
        elif detail == "Own Goal":
            line += " — автогол"

        if assist:
            line += f", пас: {assist}"

        result.append(line)

    return result


def format_red_cards(events: list[dict[str, Any]]) -> list[str]:
    result = []

    for event in events:
        if event.get("type") != "Card":
            continue

        detail = event.get("detail") or ""

        if "Red" not in detail and "Second Yellow" not in detail:
            continue

        player = safe_name(
            (event.get("player") or {}).get("name"),
            "Неизвестный игрок",
        )
        team = safe_name(
            (event.get("team") or {}).get("name"),
            "Команда",
        )

        if "Second Yellow" in detail:
            card_type = "вторая желтая / удаление"
        else:
            card_type = "красная карточка"

        result.append(f"{event_minute(event)} — {player} ({team}), {card_type}")

    return result


def format_match(match: dict[str, Any], events: list[dict[str, Any]]) -> str:
    home = safe_name(
        (match.get("teams", {}).get("home") or {}).get("name"),
        "Хозяева",
    )
    away = safe_name(
        (match.get("teams", {}).get("away") or {}).get("name"),
        "Гости",
    )

    goals_data = match.get("goals") or {}
    home_goals = goals_data.get("home")
    away_goals = goals_data.get("away")

    score_text = f"{home_goals}:{away_goals}"
    round_name = (match.get("league") or {}).get("round") or ""
    status = (match.get("fixture") or {}).get("status", {}).get("short")

    lines = [
        f"<b>{html.escape(home)} {html.escape(score_text)} {html.escape(away)}</b>"
    ]

    if round_name:
        lines.append(f"Стадия: {html.escape(round_name)}")

    if status == "PEN":
        penalty = ((match.get("score") or {}).get("penalty") or {})
        penalty_home = penalty.get("home")
        penalty_away = penalty.get("away")

        if penalty_home is not None and penalty_away is not None:
            lines.append(f"По пенальти: {penalty_home}:{penalty_away}")

    goals = format_goals(events)
    red_cards = format_red_cards(events)

    if goals:
        lines.append("Голы:")
        lines.extend(f"• {html.escape(goal)}" for goal in goals)
    else:
        if home_goals == 0 and away_goals == 0:
            lines.append("Голы: нет.")
        else:
            lines.append("Голы: данные по авторам пока недоступны в API.")

    if red_cards:
        lines.append("Удаления:")
        lines.extend(f"• {html.escape(card)}" for card in red_cards)
    else:
        lines.append("Удаления: нет.")

    return "\n".join(lines)


def build_highlights(
    matches_with_events: list[tuple[dict[str, Any], list[dict[str, Any]]]]
) -> list[str]:
    highlights = []

    for match, events in matches_with_events:
        home = safe_name(
            (match.get("teams", {}).get("home") or {}).get("name"),
            "Хозяева",
        )
        away = safe_name(
            (match.get("teams", {}).get("away") or {}).get("name"),
            "Гости",
        )

        goals_data = match.get("goals") or {}
        home_goals = goals_data.get("home")
        away_goals = goals_data.get("away")

        if home_goals is not None and away_goals is not None:
            diff = abs(home_goals - away_goals)

            if diff >= 3:
                winner = home if home_goals > away_goals else away
                highlights.append(
                    f"{winner} одержала крупную победу в матче {home} — {away}."
                )

        for event in events:
            event_type = event.get("type")
            detail = event.get("detail") or ""
            elapsed = (event.get("time") or {}).get("elapsed") or 0

            if event_type == "Goal" and elapsed >= 85:
                player = safe_name(
                    (event.get("player") or {}).get("name"),
                    "игрок",
                )
                team = safe_name(
                    (event.get("team") or {}).get("name"),
                    "команда",
                )

                highlights.append(
                    f"Поздний гол: {player} ({team}) забил на {event_minute(event)}."
                )

            if event_type == "Card" and (
                "Red" in detail or "Second Yellow" in detail
            ):
                player = safe_name(
                    (event.get("player") or {}).get("name"),
                    "игрок",
                )
                team = safe_name(
                    (event.get("team") or {}).get("name"),
                    "команда",
                )

                highlights.append(
                    f"Удаление: {player} ({team}) получил красную карточку "
                    f"на {event_minute(event)}."
                )

    if not highlights:
        highlights.append("День прошел без разгромов, поздних голов и удалений.")

    return highlights[:8]


def build_bright_players(
    matches_with_events: list[tuple[dict[str, Any], list[dict[str, Any]]]]
) -> list[str]:
    scorers: dict[tuple[str, str], int] = {}

    for _, events in matches_with_events:
        for event in events:
            if event.get("type") != "Goal":
                continue

            detail = event.get("detail") or ""

            if detail in {"Missed Penalty", "Own Goal"}:
                continue

            player = (event.get("player") or {}).get("name")
            team = (event.get("team") or {}).get("name") or "Команда"

            if not player:
                continue

            key = (player, team)
            scorers[key] = scorers.get(key, 0) + 1

    multi_goals = [
        f"{player} ({team}) — {goals} гол(а)"
        for (player, team), goals in scorers.items()
        if goals >= 2
    ]

    if multi_goals:
        return multi_goals[:5]

    one_goal = [
        f"{player} ({team}) — забил важный гол"
        for (player, team), goals in scorers.items()
        if goals == 1
    ]

    if one_goal:
        return one_goal[:5]

    return ["Ярких индивидуальных всплесков по голам не было."]


def format_standings_for_matches(matches: list[dict[str, Any]]) -> str:
    try:
        standings = get_standings()
    except Exception as exc:
        print("Cannot load standings:", exc)
        return "Таблица: данные временно недоступны."

    if not standings:
        return "Таблица: данные пока недоступны или стадия без групповой таблицы."

    played_teams = set()

    for match in matches:
        home = (match.get("teams", {}).get("home") or {}).get("name")
        away = (match.get("teams", {}).get("away") or {}).get("name")

        if home:
            played_teams.add(home)

        if away:
            played_teams.add(away)

    relevant_groups = []

    for group in standings:
        group_team_names = {
            (row.get("team") or {}).get("name")
            for row in group
            if (row.get("team") or {}).get("name")
        }

        if played_teams & group_team_names:
            relevant_groups.append(group)

    if not relevant_groups:
        return "Таблица: для этих матчей отдельная групповая таблица не найдена."

    blocks = []

    for group in relevant_groups:
        group_name = group[0].get("group", "Группа") if group else "Группа"
        lines = [f"<b>{html.escape(group_name)}</b>"]

        for row in group:
            rank = row.get("rank")
            team = safe_name((row.get("team") or {}).get("name"), "Команда")
            points = row.get("points", 0)
            played = (row.get("all") or {}).get("played", 0)
            goals_diff = row.get("goalsDiff", 0)

            lines.append(
                f"{rank}. {html.escape(team)} — {points} очк., "
                f"матчей: {played}, разница: {goals_diff}"
            )

        blocks.append("\n".join(lines))

    return "\n\n".join(blocks)


def parse_api_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def format_upcoming() -> str:
    try:
        fixtures = get_upcoming_matches(limit=10)
    except Exception as exc:
        print("Cannot load upcoming matches:", exc)
        return "Расписание ближайших матчей временно недоступно."

    if not fixtures:
        return "Ближайшие матчи не найдены."

    lines = []

    for fixture in fixtures[:8]:
        dt_raw = fixture.get("fixture", {}).get("date")

        if not dt_raw:
            continue

        dt_local = parse_api_datetime(dt_raw).astimezone(get_tz())

        home = safe_name(
            (fixture.get("teams", {}).get("home") or {}).get("name"),
            "Команда 1",
        )
        away = safe_name(
            (fixture.get("teams", {}).get("away") or {}).get("name"),
            "Команда 2",
        )

        lines.append(
            f"• {dt_local.strftime('%d.%m %H:%M')} — "
            f"{html.escape(home)} vs {html.escape(away)}"
        )

    return "\n".join(lines) if lines else "Ближайшие матчи не найдены."


def build_report(date_str: str) -> str | None:
    matches = get_finished_matches(date_str)

    if not matches:
        return None

    matches_with_events: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []

    for match in matches:
        fixture_id = (match.get("fixture") or {}).get("id")
        events: list[dict[str, Any]] = []

        if fixture_id:
            try:
                events = get_fixture_events(int(fixture_id))
            except Exception as exc:
                print(f"Cannot load events for fixture {fixture_id}:", exc)

        matches_with_events.append((match, events))
        time.sleep(0.3)

    match_blocks = [
        format_match(match, events)
        for match, events in matches_with_events
    ]

    highlights = build_highlights(matches_with_events)
    bright_players = build_bright_players(matches_with_events)

    date_title = datetime.fromisoformat(date_str).strftime("%d.%m.%Y")

    sections = [
        f"🏆 <b>Итоги дня ЧМ-2026 — {date_title}</b>",
        "<b>Результаты матчей</b>\n\n" + "\n\n".join(match_blocks),
        "<b>Главные моменты</b>\n" + "\n".join(
            f"• {html.escape(item)}" for item in highlights
        ),
        "<b>Влияние на турнир</b>\n" + format_standings_for_matches(matches),
        "<b>Новые яркие игроки</b>\n" + "\n".join(
            f"• {html.escape(item)}" for item in bright_players
        ),
        "<b>Что посмотреть дальше</b>\n" + format_upcoming(),
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

        if already_checked_no_matches(date_str) and not force:
            print(f"No-match check for {date_str} already done.")
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
            mark_no_matches(date_str)
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

    if already_checked_no_matches(date_str) and not force:
        return {
            "status": "already_checked_no_matches",
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
    print(f"League ID: {LEAGUE_ID}")
    print(f"Season: {SEASON}")
    print(f"Report window: {REPORT_WINDOW_START}-{REPORT_WINDOW_END}")

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


@app.get("/")
def root() -> dict[str, Any]:
    return {
        "service": APP_NAME,
        "status": "ok",
        "health": "/health",
        "tick": "/tick?secret=...",
    }


@app.get("/health")
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
        "no_match_dates": sorted(state.get("no_matches", {}).keys()),
        "job_running": RUN_LOCK.locked(),
    }


@app.get("/tick")
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


@app.get("/run-report")
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


@app.get("/test-telegram")
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