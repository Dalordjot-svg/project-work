import calendar
import html
import hmac
import json
import re
import sqlite3
import time
from contextlib import closing
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import parse_qsl, urlsplit


MONTHS = ("Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
          "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь")
DAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")

APP_PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>График смен COFFEEMAN</title>
<style>
body{margin:0;background:#f6f4f0;color:#271d17;font:1rem/1.5 'Segoe UI',sans-serif}
header{background:#2a211b;color:white;padding:1rem;font-weight:800;letter-spacing:.12em}
main{max-width:50rem;margin:auto;padding:1rem}
</style>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
</head><body><header>COFFEEMAN</header><main>
<h1>Ты новенький?</h1>
<p>Открой календарь кнопкой «Расписание» в рабочем чате Telegram.</p>
</main><script>
const app = window.Telegram?.WebApp;
const main = document.querySelector('main');
let busy = false;
async function showMonth(month = '') {
    if (busy) return;
    busy = true;
    try {
        const reply = await fetch('/api/calendar', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({initData: app.initData, month})
        });
        if (!reply.ok) {
            const heading = document.createElement('h1');
            const detail = document.createElement('p');
            heading.textContent = reply.status === 503 ? 'Календарь временно недоступен' : 'Ты новенький?';
            detail.textContent = reply.status === 403
                ? 'Для просмотра календаря вступи в рабочий чат COFFEEMAN.'
                : 'Заново открой «Расписание» в рабочем чате Telegram.';
            main.replaceChildren(heading, detail);
            return;
        }
        const page = new DOMParser().parseFromString(await reply.text(), 'text/html');
        if (!document.querySelector('#calendar-style')) {
            const style = page.querySelector('style');
            style.id = 'calendar-style';
            document.head.append(style);
        }
        main.replaceChildren(...page.querySelector('main').childNodes);
    } catch {
        window.alert('Календарь временно недоступен. Попробуй открыть его ещё раз.');
    } finally {
        busy = false;
    }
}
main.addEventListener('click', (event) => {
    const link = event.target.closest('a[href^="?month="]');
    if (!link) return;
    event.preventDefault();
    showMonth(new URL(link.href).searchParams.get('month'));
});
if (app?.initData) {
    app.ready();
    showMonth();
}
</script></body></html>"""


def telegram_identity(init_data, bot_token, now):
    if not isinstance(init_data, str) or not init_data or len(init_data) > 12000:
        raise ValueError
    fields = dict(parse_qsl(init_data, strict_parsing=True, max_num_fields=30))
    supplied_hash = fields.pop("hash", "")
    check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    key = hmac.digest(b"WebAppData", bot_token.encode(), "sha256")
    expected_hash = hmac.digest(key, check.encode(), "sha256").hex()
    if not re.fullmatch(r"[a-f0-9]{64}", supplied_hash) or not hmac.compare_digest(supplied_hash, expected_hash):
        raise ValueError
    if not 0 <= now - int(fields.get("auth_date", "0")) <= 3600:
        raise ValueError
    user = json.loads(fields.get("user", "{}"))
    token = fields.get("start_param", "")
    if not isinstance(user, dict) or type(user.get("id")) is not int or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        raise ValueError
    return user["id"], token


def month_start(value, today):
    if value and re.fullmatch(r"\d{4}-\d{2}", value):
        try:
            month = date.fromisoformat(value + "-01")
            if 2 <= month.year <= 9998:
                return month
        except ValueError:
            pass
    return today.replace(day=1)


def month_link(month, jump):
    year, month_zero = divmod(month.year * 12 + month.month - 1 + jump, 12)
    return f"?month={year:04d}-{month_zero + 1:02d}"


def render_calendar(month, today, shifts):
    by_day = {}
    for day, starts, ends, name in shifts:
        by_day.setdefault(day, []).append(
            f"<li><time>{html.escape(starts)}–{html.escape(ends)}</time>"
            f"<span>{html.escape(name)}</span></li>"
        )
    headings = "".join(f"<div class='weekday'>{day}</div>" for day in DAYS)
    cells = []
    agenda = []
    for day in calendar.Calendar().itermonthdates(month.year, month.month):
        if day.month != month.month:
            cells.append("<div class='day outside' aria-hidden='true'></div>")
            continue
        entries = "".join(by_day.get(day.isoformat(), []))
        classes = "day today" if day == today else "day"
        if entries:
            classes += " occupied"
            agenda.append(
                f"<section id='agenda-{day.day}' aria-label='{day:%d.%m.%Y}'>"
                f"<h3>{day.day:02d}.{day.month:02d} · {DAYS[day.weekday()]}</h3>"
                f"<ul>{entries}</ul></section>"
            )
        mobile_date = (
            f"<a href='#agenda-{day.day}' aria-label='{day:%d.%m.%Y}, смены'>{day.day}</a>"
            if entries else str(day.day)
        )
        cells.append(
            f"<section class='{classes}' aria-label='{day:%d.%m.%Y}'>"
            f"<div class='date'>{day.day}</div><div class='mobile-date'>{mobile_date}</div>"
            f"<ul>{entries}</ul></section>"
        )
    empty = "" if shifts else "<p class='empty'>В этом месяце смен пока нет.</p>"
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>График смен COFFEEMAN</title>
<style>
*{{box-sizing:border-box}}body{{margin:0;background:#f6f4f0;color:#271d17;font:1rem/1.45 'Segoe UI',sans-serif}}
header{{background:#2a211b;color:#fff;padding:1.2rem max(1rem,calc((100vw - 1200px)/2));font-weight:800;letter-spacing:.12em}}
main{{max-width:1232px;margin:auto;padding:clamp(1rem,4vw,2.4rem) 1rem 3rem}}
h1{{font-size:clamp(1.85rem,5vw,2.8rem);line-height:1.1;margin:0 0 1.45rem;letter-spacing:-.04em}}
.toolbar{{display:flex;gap:.8rem;align-items:center;justify-content:space-between;flex-wrap:wrap;margin-bottom:1rem}}
h2{{font-size:1.4rem;margin:0}}nav{{display:flex;gap:.45rem;flex-wrap:wrap}}
a{{display:inline-block;padding:.45rem .68rem;border:1px solid #d9cec2;border-radius:.45rem;background:white;color:#38251a;text-decoration:none;font-weight:600}}
a:hover,a:focus-visible{{background:#e9ded1}}.empty{{color:#594a3e;margin:.65rem 0}}
.grid{{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:1px;background:#ddd3c9;border:1px solid #ddd3c9;border-radius:.8rem;overflow:hidden}}
.weekday,.day{{padding:.65rem;background:white}}.weekday{{font-weight:700;background:#eee6dd}}.day{{min-height:6rem}}
.outside{{background:#f2eee9}}.date{{font-weight:750}}.mobile-date,.agenda{{display:none}}
.today{{box-shadow:inset 0 0 0 2px #805337}}ul{{margin:.5rem 0 0;padding:0;list-style:none}}
li{{margin-top:.35rem;padding:.4rem;background:#eee5d9;border-radius:.34rem;overflow-wrap:anywhere}}time{{display:block;font-weight:750}}
@media(max-width:700px){{.weekday,.day{{text-align:center;padding:.3rem .05rem}}.day{{min-height:2.5rem;display:grid;place-items:center}}.date,.day ul{{display:none}}.mobile-date,.agenda{{display:block}}.mobile-date a{{padding:.24rem .3rem;background:#eee5d9;border-color:#ad8165}}.agenda section{{padding:.7rem;margin-top:.65rem;background:#fff;border:1px solid #ddd3c9;border-radius:.65rem}}.agenda h3{{margin:0}}}}
</style></head><body><header>COFFEEMAN</header>
<main><h1>График смен</h1><div class="toolbar"><h2>{MONTHS[month.month - 1]} {month.year}</h2>
<nav aria-label="Выбор месяца"><a href="{month_link(month, -1)}">Предыдущий</a><a href="?month={today:%Y-%m}">Сегодня</a><a href="{month_link(month, 1)}">Следующий</a></nav></div>
{empty}<div class="grid">{headings}{''.join(cells)}</div>
<div class="agenda">{''.join(agenda)}</div></main></body></html>"""


def start_calendar(db_path, zone, bind, port, bot_token, group_member):
    class CalendarHandler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            # Access URLs contain a group secret.
            pass

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/healthz":
                self.respond(200, "ok", "text/plain")
                return
            status = 200 if path in {"/", "/app"} else 404
            self.respond(status, APP_PAGE, "text/html")

        def do_POST(self):
            if self.path != "/api/calendar":
                self.respond(404, "Ссылка недоступна.", "text/plain")
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 16000:
                    raise ValueError
                payload = json.loads(self.rfile.read(size))
                if not isinstance(payload, dict):
                    raise ValueError
                user_id, token = telegram_identity(payload.get("initData"), bot_token, time.time())
            except (ValueError, TypeError):
                self.respond(401, "Открой расписание через Telegram.", "text/plain")
                return
            today = datetime.now(zone).date()
            value = payload.get("month")
            month = month_start(value if isinstance(value, str) else "", today)
            end = date(month.year + (month.month == 12), month.month % 12 + 1, 1)
            with closing(sqlite3.connect(db_path)) as db:
                group = db.execute(
                    "SELECT chat_id FROM schedule_links WHERE token = ?", (token,)
                ).fetchone()
                try:
                    allowed = group and group_member(group[0], user_id)
                except (OSError, RuntimeError):
                    self.respond(503, "Проверка Telegram временно недоступна.", "text/plain")
                    return
                if not allowed:
                    self.respond(403, "Расписание доступно участникам рабочего чата.", "text/plain")
                    return
                shifts = db.execute(
                    "SELECT s.day, s.starts, s.ends, e.name FROM shifts s "
                    "JOIN employees e ON e.id = s.employee_id "
                    "WHERE s.day >= ? AND s.day < ? ORDER BY s.day, s.starts, e.name",
                    (month.isoformat(), end.isoformat()),
                ).fetchall()
            self.respond(200, render_calendar(month, today, shifts), "text/html")

        def respond(self, status, text, content_type):
            data = text.encode()
            self.send_response(status)
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Robots-Tag", "noindex, nofollow")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer((bind, port), CalendarHandler)
    Thread(target=server.serve_forever, daemon=True).start()
    return server
