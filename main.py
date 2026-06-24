import os
import json
import html
import time
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from apscheduler.schedulers.blocking import BlockingScheduler


load_dotenv()

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
FOOTBALL_API_KEY = os.environ["FOOTBALL_API_KEY"]

TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")
LEAGUE_ID = int(os.getenv("LEAGUE_ID", "1"))      # FIFA World Cup
SEASON = int(os.getenv("SEASON", "2026"))        # World Cup 2026

REPORT_HOUR_FROM = int(os.getenv("REPORT_HOUR_FROM", "9"))
REPORT_HOUR_TO = int(os.getenv("REPORT_HOUR_TO", "12"))
REPORT_MINUTE = int(os.getenv("REPORT_MINUTE", "30"))

SEND_TEST_ON_START = os.getenv("SEND_TEST_ON_START", "0") == "1"
RUN_REPORT_ON_START = os.getenv("RUN_REPORT_ON_START", "0") == "1"

BASE_URL = "https://v3.football.api-sports.io"
FINISHED_STATUSES = {"FT", "AET", "PEN"}

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

SENT_REPORTS_FILE = DATA_DIR / "sent_reports.json"


def api_get(endpoint: str, params: dict | None = None) -> dict:
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

    if data.get("errors"):
        print("API errors:", data["errors"])

    return data


def telegram_send(text: str) -> None:
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

        time.sleep(0.7)


def split_text(text: str, max_len: int = 3900) -> list[str]:
    parts = []
    current = ""

    for block in text.split("\n\n"):
        if len(current) + len(block) + 2 <= max_len:
            current += ("\n\n" if current else "") + block
        else:
            if current:
                parts.append(current)
            current = block

    if current:
        parts.append(current)

    return parts


def load_sent_reports() -> set[str]:
    if not SENT_REPORTS_FILE.exists():
        return set()

    try:
        return set(json.loads(SENT_REPORTS_FILE.read_text(encoding="utf-8")))
    except Exception as exc:
        print("Cannot read sent reports file:", exc)
        return set()


def save_sent_report(date_str: str) -> None:
    sent = load_sent_reports()
    sent.add(date_str)

    SENT_REPORTS_FILE.write_text(
        json.dumps(sorted(sent), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def get_yesterday_moscow_date() -> str:
    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz)
    yesterday = now.date() - timedelta(days=1)
    return yesterday.isoformat()


def get_fixtures_by_date(date_str: str) -> list[dict]:
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


def get_finished_matches(date_str: str) -> list[dict]:
    fixtures = get_fixtures_by_date(date_str)

    return [
        fixture
        for fixture in fixtures
        if fixture["fixture"]["status"]["short"] in FINISHED_STATUSES
    ]


def get_fixture_events(fixture_id: int) -> list[dict]:
    data = api_get(
        "/fixtures/events",
        {
            "fixture": fixture_id,
        },
    )

    return data.get("response", [])


def get_standings() -> list[list[dict]]:
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


def get_upcoming_matches() -> list[dict]:
    data = api_get(
        "/fixtures",
        {
            "league": LEAGUE_ID,
            "season": SEASON,
            "next": 10,
            "timezone": TIMEZONE,
        },
    )

    return data.get("response", [])


def event_minute(event: dict) -> str:
    time_data = event.get("time", {})
    elapsed = time_data.get("elapsed")
    extra = time_data.get("extra")

    if elapsed is None:
        return "?"

    if extra:
        return f"{elapsed}+{extra}’"

    return f"{elapsed}’"


def format_goals(events: list[dict]) -> list[str]:
    result = []

    for event in events:
        if event.get("type") != "Goal":
            continue

        detail = event.get("detail") or ""

        if detail == "Missed Penalty":
            continue

        minute = event_minute(event)
        player = event.get("player", {}).get("name") or "Неизвестный игрок"
        team = event.get("team", {}).get("name") or "Команда"
        assist = event.get("assist", {}).get("name")

        line = f"{minute} — {player} ({team})"

        if detail == "Penalty":
            line += " — пенальти"
        elif detail == "Own Goal":
            line += " — автогол"

        if assist:
            line += f", пас: {assist}"

        result.append(line)

    return result


def format_red_cards(events: list[dict]) -> list[str]:
    result = []

    for event in events:
        if event.get("type") != "Card":
            continue

        detail = event.get("detail") or ""

        if "Red" not in detail and "Second Yellow" not in detail:
            continue

        minute = event_minute(event)
        player = event.get("player", {}).get("name") or "Неизвестный игрок"
        team = event.get("team", {}).get("name") or "Команда"

        if "Second Yellow" in detail:
            card_type = "вторая желтая / удаление"
        else:
            card_type = "красная карточка"

        result.append(f"{minute} — {player} ({team}), {card_type}")

    return result


def format_match(match: dict, events: list[dict]) -> str:
    home = match["teams"]["home"]["name"]
    away = match["teams"]["away"]["name"]

    home_goals = match["goals"]["home"]
    away_goals = match["goals"]["away"]

    status = match["fixture"]["status"]["short"]
    round_name = match.get("league", {}).get("round", "")

    lines = [
        f"<b>{html.escape(home)} {home_goals}:{away_goals} {html.escape(away)}</b>",
    ]

    if round_name:
        lines.append(f"Стадия: {html.escape(round_name)}")

    if status == "PEN":
        penalty_home = match.get("score", {}).get("penalty", {}).get("home")
        penalty_away = match.get("score", {}).get("penalty", {}).get("away")

        if penalty_home is not None and penalty_away is not None:
            lines.append(f"По пенальти: {penalty_home}:{penalty_away}")

    goals = format_goals(events)
    red_cards = format_red_cards(events)

    if goals:
        lines.append("Голы:")
        lines.extend(f"• {html.escape(goal)}" for goal in goals)
    else:
        lines.append("Голы: нет.")

    if red_cards:
        lines.append("Удаления:")
        lines.extend(f"• {html.escape(card)}" for card in red_cards)
    else:
        lines.append("Удаления: нет.")

    return "\n".join(lines)


def build_highlights(matches_with_events: list[tuple[dict, list[dict]]]) -> list[str]:
    highlights = []

    for match, events in matches_with_events:
        home = match["teams"]["home"]["name"]
        away = match["teams"]["away"]["name"]
        home_goals = match["goals"]["home"]
        away_goals = match["goals"]["away"]

        if home_goals is not None and away_goals is not None:
            diff = abs(home_goals - away_goals)

            if diff >= 3:
                winner = home if home_goals > away_goals else away
                highlights.append(
                    f"{winner} одержала крупную победу в матче {home} — {away}."
                )

        for event in events:
            if event.get("type") == "Goal":
                elapsed = event.get("time", {}).get("elapsed") or 0

                if elapsed >= 85:
                    player = event.get("player", {}).get("name") or "игрок"
                    team = event.get("team", {}).get("name") or "команда"

                    highlights.append(
                        f"Поздний гол: {player} ({team}) забил на {event_minute(event)}."
                    )

            if event.get("type") == "Card":
                detail = event.get("detail") or ""

                if "Red" in detail or "Second Yellow" in detail:
                    player = event.get("player", {}).get("name") or "игрок"
                    team = event.get("team", {}).get("name") or "команда"

                    highlights.append(
                        f"Удаление: {player} ({team}) получил красную карточку на {event_minute(event)}."
                    )

    if not highlights:
        highlights.append("День прошел без разгромов, поздних голов и удалений.")

    return highlights[:8]


def build_bright_players(matches_with_events: list[tuple[dict, list[dict]]]) -> list[str]:
    scorers: dict[tuple[str, str], int] = {}

    for _, events in matches_with_events:
        for event in events:
            if event.get("type") != "Goal":
                continue

            detail = event.get("detail") or ""

            if detail in {"Missed Penalty", "Own Goal"}:
                continue

            player = event.get("player", {}).get("name")
            team = event.get("team", {}).get("name")

            if not player:
                continue

            key = (player, team or "Команда")
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


def format_standings(matches: list[dict]) -> str:
    try:
        standings = get_standings()
    except Exception as exc:
        print("Cannot load standings:", exc)
        return "Таблица: данные временно недоступны."

    if not standings:
        return "Таблица: данные пока недоступны или стадия без групповой таблицы."

    played_teams = set()

    for match in matches:
        played_teams.add(match["teams"]["home"]["name"])
        played_teams.add(match["teams"]["away"]["name"])

    relevant_groups = []

    for group in standings:
        group_team_names = {row["team"]["name"] for row in group}

        if played_teams & group_team_names:
            relevant_groups.append(group)

    if not relevant_groups:
        return "Таблица: для этих матчей отдельная групповая таблица не найдена."

    blocks = []

    for group in relevant_groups:
        group_name = group[0].get("group", "Группа")
        lines = [f"<b>{html.escape(group_name)}</b>"]

        for row in group:
            rank = row.get("rank")
            team = row["team"]["name"]
            points = row.get("points", 0)
            played = row.get("all", {}).get("played", 0)
            goals_diff = row.get("goalsDiff", 0)

            lines.append(
                f"{rank}. {html.escape(team)} — {points} очк., "
                f"матчей: {played}, разница: {goals_diff}"
            )

        blocks.append("\n".join(lines))

    return "\n\n".join(blocks)


def format_upcoming() -> str:
    try:
        fixtures = get_upcoming_matches()
    except Exception as exc:
        print("Cannot load upcoming matches:", exc)
        return "Расписание ближайших матчей временно недоступно."

    if not fixtures:
        return "Ближайшие матчи не найдены."

    lines = []

    for fixture in fixtures[:8]:
        dt_raw = fixture["fixture"]["date"]
        dt = datetime.fromisoformat(dt_raw.replace("Z", "+00:00"))
        dt_local = dt.astimezone(ZoneInfo(TIMEZONE))

        home = fixture["teams"]["home"]["name"]
        away = fixture["teams"]["away"]["name"]

        lines.append(
            f"• {dt_local.strftime('%d.%m %H:%M')} — "
            f"{html.escape(home)} vs {html.escape(away)}"
        )

    return "\n".join(lines)


def build_report(date_str: str) -> str | None:
    matches = get_finished_matches(date_str)

    if not matches:
        return None

    matches_with_events = []

    for match in matches:
        fixture_id = match["fixture"]["id"]

        try:
            events = get_fixture_events(fixture_id)
        except Exception as exc:
            print(f"Cannot load events for fixture {fixture_id}:", exc)
            events = []

        matches_with_events.append((match, events))

        time.sleep(0.4)

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
        "<b>Влияние на турнир</b>\n" + format_standings(matches),
        "<b>Новые яркие игроки</b>\n" + "\n".join(
            f"• {html.escape(item)}" for item in bright_players
        ),
        "<b>Что посмотреть дальше</b>\n" + format_upcoming(),
    ]

    return "\n\n".join(sections)


def send_daily_report_if_needed() -> None:
    date_str = get_yesterday_moscow_date()
    sent_reports = load_sent_reports()

    print(f"Checking report for {date_str}...")

    if date_str in sent_reports:
        print(f"Report for {date_str} already sent.")
        return

    try:
        report = build_report(date_str)
    except Exception as exc:
        print("Report build error:", exc)
        return

    if not report:
        print(f"No finished World Cup matches for {date_str}.")
        return

    try:
        telegram_send(report)
        save_sent_report(date_str)
        print(f"Report for {date_str} sent successfully.")
    except Exception as exc:
        print("Telegram send error:", exc)


def send_start_message() -> None:
    text = (
        "✅ Бот итогов ЧМ-2026 запущен.\n"
        f"Часовой пояс: {TIMEZONE}\n"
        f"Проверка: каждый день с {REPORT_HOUR_FROM}:"
        f"{REPORT_MINUTE:02d} до {REPORT_HOUR_TO}:"
        f"{REPORT_MINUTE:02d}."
    )

    telegram_send(html.escape(text))


def main() -> None:
    print("World Cup Telegram reporter started.")
    print(f"Timezone: {TIMEZONE}")
    print(f"League: {LEAGUE_ID}, season: {SEASON}")

    if SEND_TEST_ON_START:
        send_start_message()

    if RUN_REPORT_ON_START:
        send_daily_report_if_needed()

    scheduler = BlockingScheduler(timezone=TIMEZONE)

    scheduler.add_job(
        send_daily_report_if_needed,
        trigger="cron",
        hour=f"{REPORT_HOUR_FROM}-{REPORT_HOUR_TO}",
        minute=REPORT_MINUTE,
        id="daily_worldcup_report",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )

    print(
        f"Scheduler active: every day from {REPORT_HOUR_FROM}:"
        f"{REPORT_MINUTE:02d} to {REPORT_HOUR_TO}:"
        f"{REPORT_MINUTE:02d} {TIMEZONE}"
    )

    scheduler.start()


if __name__ == "__main__":
    main()