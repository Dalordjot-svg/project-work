import json
import logging
import os
import secrets
import sqlite3
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from calendar_web import start_calendar


SHIFT_BUTTON = "Мои смены"


def api(token, method, **payload):
    request = Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urlopen(request, timeout=40) as response:
        result = json.load(response)
    if not result["ok"]:
        raise RuntimeError("Telegram API error")
    return result["result"]


def open_database(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS employees (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS shifts (
            employee_id INTEGER NOT NULL REFERENCES employees(id),
            day TEXT NOT NULL,
            starts TEXT NOT NULL,
            ends TEXT NOT NULL,
            PRIMARY KEY (employee_id, day)
        );
        CREATE TABLE IF NOT EXISTS schedule_links (
            chat_id INTEGER PRIMARY KEY,
            token TEXT NOT NULL UNIQUE
        );
    """)
    db.execute("PRAGMA foreign_keys = ON")
    return db


def handle(db, message, admins, today):
    if message.get("chat", {}).get("type") != "private":
        return None
    user = message.get("from")
    raw = message.get("text", "").strip()
    if not user or not raw:
        return None

    user_id = user["id"]
    command, *args = raw.split()
    if command == "/start":
        name = " ".join(filter(None, [user.get("first_name"), user.get("last_name")]))
        db.execute(
            "INSERT INTO employees VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET name = excluded.name",
            (user_id, name),
        )
        welcome = f"COFFEEMAN Расписание\nНажми «{SHIFT_BUTTON}», чтобы открыть график."
        if user_id in admins:
            welcome += (
                "\n\n/staff — сотрудники, которые нажали «Старт»."
                "\n/shift ID ГГГГ-ММ-ДД ЧЧ:ММ ЧЧ:ММ — добавить или заменить смену."
                "\n/unshift ID ГГГГ-ММ-ДД — удалить смену."
            )
        return welcome
    if command == "/id":
        return f"Твой Telegram ID: {user_id}"
    if command == "/shifts" or raw == SHIFT_BUTTON:
        end = today + timedelta(days=7)
        shifts = db.execute(
            "SELECT day, starts, ends FROM shifts WHERE employee_id = ? AND day >= ? AND day < ? ORDER BY day",
            (user_id, today.isoformat(), end.isoformat()),
        ).fetchall()
        if not shifts:
            return "На ближайшие семь дней смен нет."
        lines = [f"{date.fromisoformat(day):%d.%m} · {start}–{finish}" for day, start, finish in shifts]
        return "Твои смены на ближайшие семь дней\n" + "\n".join(lines)
    if command not in {"/staff", "/shift", "/unshift"}:
        return "Нажми /start, чтобы открыть меню."
    if user_id not in admins:
        return "Эта команда доступна администратору."
    if command == "/staff":
        employees = db.execute("SELECT id, name FROM employees ORDER BY name, id").fetchall()
        lines = [f"{employee_id} · {name}" for employee_id, name in employees]
        return "Сотрудники\n" + "\n".join(lines) if lines else "Пока никто не нажал «Старт»."
    try:
        if len(args) != (4 if command == "/shift" else 2):
            raise ValueError
        employee_id = int(args[0])
        if not 0 < employee_id < 2**63:
            raise ValueError
        day = datetime.strptime(args[1], "%Y-%m-%d").date().isoformat()
        if command == "/shift":
            starts, ends = [datetime.strptime(value, "%H:%M").strftime("%H:%M") for value in args[2:]]
            if starts == ends:
                return "Время начала и окончания должно различаться."
    except ValueError:
        return (
            "Формат /shift ID ГГГГ-ММ-ДД ЧЧ:ММ ЧЧ:ММ"
            if command == "/shift" else "Формат /unshift ID ГГГГ-ММ-ДД"
        )
    if command == "/unshift":
        deleted = db.execute("DELETE FROM shifts WHERE employee_id = ? AND day = ?", (employee_id, day))
        return "Смена удалена." if deleted.rowcount else "Смена на эту дату не найдена."
    employee = db.execute("SELECT name FROM employees WHERE id = ?", (employee_id,)).fetchone()
    if not employee:
        return "Сотрудник должен сначала нажать «Старт» в боте."
    db.execute(
        "INSERT INTO shifts VALUES (?, ?, ?, ?) ON CONFLICT(employee_id, day) "
        "DO UPDATE SET starts = excluded.starts, ends = excluded.ends",
        (employee_id, day, starts, ends),
    )
    return f"Смена сохранена. {employee[0]} · {day} · {starts}–{ends}"


def calendar_reply(db, message, admins, public_url):
    text = message.get("text", "").strip()
    if not text.startswith("/") or not message.get("from"):
        return None
    command, _, mention = text.split()[0].partition("@")
    if mention and mention.lower() != "coffeemanshiftsbot":
        return None
    chat = message["chat"]
    if chat["type"] not in {"group", "supergroup"}:
        if command == "/schedule":
            return "Календарь команды открывается по кнопке «Расписание» в рабочем чате.", None
        return None
    if command not in {"/schedule", "/schedule_on", "/schedule_off"}:
        return None
    if command in {"/schedule_on", "/schedule_off"} and message["from"]["id"] not in admins:
        return "Подключать расписание может администратор бота.", None
    if command == "/schedule_off":
        db.execute("DELETE FROM schedule_links WHERE chat_id = ?", (chat["id"],))
        return "Ссылка на расписание для этого чата отключена.", None
    if not public_url:
        return "Для календаря ещё не настроен адрес сайта.", None
    if command == "/schedule_on":
        db.execute(
            "INSERT INTO schedule_links VALUES (?, ?) ON CONFLICT(chat_id) DO NOTHING",
            (chat["id"], secrets.token_urlsafe(32)),
        )
    link = db.execute("SELECT token FROM schedule_links WHERE chat_id = ?", (chat["id"],)).fetchone()
    if not link:
        return "Администратору нужно подключить расписание командой /schedule_on.", None
    return "График смен COFFEEMAN", f"{public_url}/schedule/{link[0]}"


def send_reply(token, chat_id, reply, markup=None):
    while reply:
        split = reply.rfind("\n", 0, 3000) if len(reply) > 3000 else len(reply)
        split = split if split > 0 else 3000
        options = {"reply_markup": markup} if markup else {}
        api(token, "sendMessage", chat_id=chat_id, text=reply[:split], **options)
        reply = reply[split:].lstrip("\n")


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    token = os.environ["BOT_TOKEN"]
    admins = {int(value) for value in os.getenv("ADMIN_IDS", "").split(",") if value.strip()}
    zone = ZoneInfo(os.getenv("BOT_TIMEZONE", "Asia/Novosibirsk"))
    db_path = os.getenv("DB_PATH", "schedule.sqlite3")
    db = open_database(db_path)
    public_url = os.getenv("SCHEDULE_BASE_URL", "").rstrip("/")
    if public_url and not public_url.startswith("https://"):
        raise ValueError("SCHEDULE_BASE_URL must use HTTPS")
    start_calendar(db_path, zone, os.getenv("CALENDAR_BIND", "127.0.0.1"),
                   int(os.getenv("PORT", "8765")))
    offset = 0
    while True:
        try:
            updates = api(token, "getUpdates", offset=offset, timeout=25, allowed_updates=["message"])
            for update in updates:
                message = update.get("message", {})
                markup = None
                with db:
                    calendar = calendar_reply(db, message, admins, public_url)
                    if calendar:
                        reply, url = calendar
                        if url:
                            markup = {"inline_keyboard": [[{"text": "Расписание", "url": url}]]}
                    else:
                        reply = handle(db, message, admins, datetime.now(zone).date())
                        if reply:
                            markup = {"keyboard": [[{"text": SHIFT_BUTTON}]], "resize_keyboard": True}
                offset = update["update_id"] + 1
                if reply:
                    send_reply(token, message["chat"]["id"], reply, markup)
        except (OSError, ValueError, RuntimeError) as error:
            logging.warning("Ошибка связи с Telegram (%s). Повтор через 5 секунд.", type(error).__name__)
            time.sleep(5)


if __name__ == "__main__":
    main()
