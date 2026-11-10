"""Telegram-бот дедлайнов: добавление в личке, напоминания в канал."""

from __future__ import annotations

import calendar
import html
import json
import logging
import os
import sqlite3
import time as _time
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from telegram import (
    BotCommand,
    ChatMember,
    InlineKeyboardButton as Btn,
    InlineKeyboardMarkup,
    MessageOriginChannel,
    Update,
)
from telegram.constants import ChatType, ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import logic
import sheet

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("deadline-bot")


def _load_dotenv(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ENV_CHANNEL = os.environ.get("CHANNEL_ID", "").strip()
TZ = ZoneInfo(os.environ.get("TZ_NAME", "Europe/Moscow").strip() or "Europe/Moscow")
DB_PATH = os.environ.get("DB_PATH", "deadlines.db").strip() or "deadlines.db"
ALLOWED_IDS = {int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x}

SHEET_URL = os.environ.get("SHEET_URL", "").strip()
SHEET_SYNC_MINUTES = max(1, int(os.environ.get("SHEET_SYNC_MINUTES", "10") or 10))
SHEET_ANNOUNCE = os.environ.get("SHEET_ANNOUNCE_CHANGES", "1").strip() not in ("0", "", "no", "false")

LABELS = {"7d": "неделя", "2d": "2 суток", "1d": "сутки", "12h": "12 часов"}

# ---------------------------------------------------------------- база


class DB:
    def __init__(self, path: str):
        self.c = sqlite3.connect(path)
        self.c.row_factory = sqlite3.Row
        self.c.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS people (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL, username TEXT, tg_id INTEGER UNIQUE);
            CREATE TABLE IF NOT EXISTS deadlines (
                id INTEGER PRIMARY KEY, title TEXT NOT NULL, link TEXT, due_ts INTEGER NOT NULL,
                created_by INTEGER, created_at INTEGER, sent TEXT NOT NULL DEFAULT '',
                done INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS deadline_people (
                deadline_id INTEGER REFERENCES deadlines(id) ON DELETE CASCADE,
                person_id INTEGER REFERENCES people(id) ON DELETE CASCADE,
                PRIMARY KEY (deadline_id, person_id));
            CREATE TABLE IF NOT EXISTS sheet_items (
                key TEXT PRIMARY KEY, title TEXT NOT NULL, due_ts INTEGER NOT NULL,
                sent TEXT NOT NULL DEFAULT '', data TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1);
            """
        )
        self.c.execute("PRAGMA foreign_keys = ON")
        self.c.commit()

    # settings
    def get(self, key: str) -> str | None:
        r = self.c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r[0] if r else None

    def set(self, key: str, value: str) -> None:
        self.c.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, value))
        self.c.commit()

    # people
    def people(self) -> list[sqlite3.Row]:
        return self.c.execute("SELECT * FROM people ORDER BY name COLLATE NOCASE").fetchall()

    def add_person(self, name: str, username: str | None, tg_id: int | None = None) -> int:
        if username:
            r = self.c.execute("SELECT id FROM people WHERE lower(username)=lower(?)", (username,)).fetchone()
            if r:
                self.c.execute("UPDATE people SET name=?, tg_id=COALESCE(?, tg_id) WHERE id=?", (name, tg_id, r[0]))
                self.c.commit()
                return r[0]
        cur = self.c.execute("INSERT INTO people (name, username, tg_id) VALUES (?, ?, ?)", (name, username, tg_id))
        self.c.commit()
        return cur.lastrowid

    def ensure_user(self, tg_id: int, name: str, username: str | None) -> None:
        r = self.c.execute("SELECT id FROM people WHERE tg_id=?", (tg_id,)).fetchone()
        if r:
            self.c.execute("UPDATE people SET username=COALESCE(?, username) WHERE id=?", (username, r[0]))
            self.c.commit()
        else:
            self.add_person(name, username, tg_id)

    def del_person(self, pid: int) -> None:
        self.c.execute("DELETE FROM people WHERE id=?", (pid,))
        self.c.commit()

    # deadlines
    def add_deadline(self, title: str, link: str | None, due_ts: int, by: int, people: list[int]) -> int:
        now = int(_time.time())
        sent = ",".join(sorted(logic.passed_reminders(due_ts, now)))
        cur = self.c.execute(
            "INSERT INTO deadlines (title, link, due_ts, created_by, created_at, sent) VALUES (?,?,?,?,?,?)",
            (title, link, due_ts, by, now, sent),
        )
        did = cur.lastrowid
        self.c.executemany("INSERT OR IGNORE INTO deadline_people VALUES (?, ?)", [(did, p) for p in people])
        self.c.commit()
        return did

    def deadline(self, did: int) -> sqlite3.Row | None:
        return self.c.execute("SELECT * FROM deadlines WHERE id=?", (did,)).fetchone()

    def active(self) -> list[sqlite3.Row]:
        return self.c.execute("SELECT * FROM deadlines WHERE done=0 ORDER BY due_ts").fetchall()

    def responsible(self, did: int) -> list[sqlite3.Row]:
        return self.c.execute(
            "SELECT p.* FROM people p JOIN deadline_people dp ON dp.person_id=p.id WHERE dp.deadline_id=? "
            "ORDER BY p.name COLLATE NOCASE",
            (did,),
        ).fetchall()

    def mark_sent(self, did: int, keys: set[str]) -> None:
        row = self.deadline(did)
        cur = set(filter(None, row["sent"].split(","))) | keys
        self.c.execute("UPDATE deadlines SET sent=? WHERE id=?", (",".join(sorted(cur)), did))
        self.c.commit()

    def mark_done(self, did: int) -> None:
        self.c.execute("UPDATE deadlines SET done=1 WHERE id=?", (did,))
        self.c.commit()

    def delete(self, did: int) -> None:
        self.c.execute("DELETE FROM deadlines WHERE id=?", (did,))
        self.c.commit()

    # строки Google-таблицы
    def sheet_item(self, key: str) -> sqlite3.Row | None:
        return self.c.execute("SELECT * FROM sheet_items WHERE key=?", (key,)).fetchone()

    def sheet_active(self) -> list[sqlite3.Row]:
        return self.c.execute("SELECT * FROM sheet_items WHERE active=1 ORDER BY due_ts").fetchall()

    def sheet_upsert(self, key: str, title: str, due_ts: int, data: dict, sent: str | None) -> None:
        payload = json.dumps(data, ensure_ascii=False)
        if sent is None:
            self.c.execute("UPDATE sheet_items SET title=?, due_ts=?, data=?, active=1 WHERE key=?",
                           (title, due_ts, payload, key))
        else:
            self.c.execute("INSERT OR REPLACE INTO sheet_items (key, title, due_ts, sent, data, active) "
                           "VALUES (?, ?, ?, ?, ?, 1)", (key, title, due_ts, sent, payload))
        self.c.commit()

    def sheet_deactivate_except(self, keys: set[str]) -> None:
        for row in self.sheet_active():
            if row["key"] not in keys:
                self.c.execute("UPDATE sheet_items SET active=0 WHERE key=?", (row["key"],))
        self.c.commit()

    def sheet_mark(self, key: str, sent: set[str] | None = None, active: int | None = None) -> None:
        if sent is not None:
            row = self.sheet_item(key)
            cur = set(filter(None, row["sent"].split(","))) | sent
            self.c.execute("UPDATE sheet_items SET sent=? WHERE key=?", (",".join(sorted(cur)), key))
        if active is not None:
            self.c.execute("UPDATE sheet_items SET active=? WHERE key=?", (active, key))
        self.c.commit()


db = DB(DB_PATH)


def channel_id() -> str | int | None:
    ch = ENV_CHANNEL or db.get("channel_id")
    if not ch:
        return None
    return int(ch) if ch.lstrip("-").isdigit() else ch


# ---------------------------------------------------------------- тексты


def mention(p: sqlite3.Row) -> str:
    if p["username"]:
        return "@" + html.escape(p["username"])
    if p["tg_id"]:
        return f'<a href="tg://user?id={p["tg_id"]}">{html.escape(p["name"])}</a>'
    return html.escape(p["name"])


def card(title: str, link: str | None, due_ts: int, people: list[sqlite3.Row]) -> str:
    lines = [f"📌 <b>{html.escape(title)}</b>", f"🗓 {logic.format_dt(logic.from_ts(due_ts, TZ))}"]
    if link:
        lines.append(f"🔗 {html.escape(link)}")
    if people:
        lines.append("👥 " + ", ".join(mention(p) for p in people))
    return "\n".join(lines)


def deadline_card(row: sqlite3.Row) -> str:
    return card(row["title"], row["link"], row["due_ts"], db.responsible(row["id"]))


# ---------------------------------------------------------------- доступ

_access_cache: dict[int, tuple[float, bool]] = {}


async def allowed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if not user:
        return False
    if ALLOWED_IDS:
        ok = user.id in ALLOWED_IDS
    else:
        ch = channel_id()
        if ch is None:
            ok = True  # канал ещё не настроен - пускаем всех, чтобы можно было начать
        else:
            hit = _access_cache.get(user.id)
            if hit and _time.time() - hit[0] < 600:
                ok = hit[1]
            else:
                try:
                    m = await context.bot.get_chat_member(ch, user.id)
                    ok = m.status in (ChatMember.OWNER, ChatMember.ADMINISTRATOR, ChatMember.MEMBER)
                except TelegramError:
                    ok = False
                _access_cache[user.id] = (_time.time(), ok)
    if not ok:
        target = update.effective_message
        text = "⛔ Добавлять дедлайны могут только подписчики канала группы."
        if update.callback_query:
            await update.callback_query.answer(text, show_alert=True)
        elif target:
            await target.reply_text(text)
    return ok


def remember_user(update: Update) -> None:
    u = update.effective_user
    if u and not u.is_bot:
        db.ensure_user(u.id, u.full_name, u.username)


# ---------------------------------------------------------------- клавиатуры


def kb_calendar(year: int, month: int) -> InlineKeyboardMarkup:
    today = datetime.now(TZ).date()
    rows = [[
        Btn("◀️", callback_data=f"cal:{logic.month_shift(year, month, -1)[0]}-{logic.month_shift(year, month, -1)[1]}"),
        Btn(f"{logic.MONTHS[month - 1]} {year}", callback_data="ign"),
        Btn("▶️", callback_data=f"cal:{logic.month_shift(year, month, 1)[0]}-{logic.month_shift(year, month, 1)[1]}"),
    ], [Btn(d, callback_data="ign") for d in ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]]]
    for week in calendar.Calendar().monthdayscalendar(year, month):
        row = []
        for d in week:
            if d == 0 or date(year, month, d) < today:
                row.append(Btn(" " if d == 0 else "·", callback_data="ign"))
            else:
                label = f"[{d}]" if date(year, month, d) == today else str(d)
                row.append(Btn(label, callback_data=f"day:{year}-{month}-{d}"))
        rows.append(row)
    rows.append([Btn("❌ Отмена", callback_data="cancel")])
    return InlineKeyboardMarkup(rows)


def kb_hours() -> InlineKeyboardMarkup:
    rows = [[Btn(f"{h:02d}", callback_data=f"hr:{h}") for h in range(r, r + 6)] for r in range(0, 24, 6)]
    rows.append([Btn("⬅️ Другая дата", callback_data="back:date"), Btn("❌ Отмена", callback_data="cancel")])
    return InlineKeyboardMarkup(rows)


def kb_minutes(h: int) -> InlineKeyboardMarkup:
    rows = [[Btn(f"{h:02d}:{m:02d}", callback_data=f"mn:{m}") for m in (0, 15, 30, 45)],
            [Btn(f"{h:02d}:59", callback_data="mn:59")],
            [Btn("⬅️ Другой час", callback_data="back:hour"), Btn("❌ Отмена", callback_data="cancel")]]
    return InlineKeyboardMarkup(rows)


def kb_people(selected: set[int]) -> InlineKeyboardMarkup:
    rows = []
    for p in db.people():
        mark = "✅" if p["id"] in selected else "▫️"
        uname = f" (@{p['username']})" if p["username"] else ""
        rows.append([Btn(f"{mark} {p['name']}{uname}", callback_data=f"tog:{p['id']}")])
    rows.append([Btn("➡️ Готово", callback_data="people:done")])
    rows.append([Btn("❌ Отмена", callback_data="cancel")])
    return InlineKeyboardMarkup(rows)


# ---------------------------------------------------------------- добавление дедлайна

TITLE, LINK, PEOPLE, DATE, HOUR, MINUTE, CONFIRM = range(7)


async def new_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await allowed(update, context):
        return ConversationHandler.END
    remember_user(update)
    context.user_data["new"] = {"people": set()}
    await update.effective_message.reply_text(
        "📝 Новый дедлайн.\n\nНапиши <b>название</b> - по какой работе дедлайн.\n\n/cancel - отмена",
        parse_mode=ParseMode.HTML,
    )
    return TITLE


async def got_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["new"]["title"] = update.message.text.strip()[:300]
    await update.message.reply_text(
        "🔗 Пришли <b>ссылку</b> на работу (файл, сайт для регистрации и т.п.) или нажми «Без ссылки».",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[Btn("Без ссылки", callback_data="nolink")],
                                           [Btn("❌ Отмена", callback_data="cancel")]]),
    )
    return LINK


async def ask_people(message, context: ContextTypes.DEFAULT_TYPE, edit: bool = False) -> int:
    sel = context.user_data["new"]["people"]
    text = ("👥 Выбери <b>ответственных</b> (можно несколько) и нажми «Готово».\n\n"
            "Нет нужного человека? Пришли сообщением: <code>Имя @username</code> - он добавится в список.")
    if not db.people():
        text += "\n\nСписок пока пуст."
    if edit:
        await message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb_people(sel))
    else:
        await message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb_people(sel))
    return PEOPLE


async def got_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    link = update.message.text.strip()
    context.user_data["new"]["link"] = link[:1000]
    return await ask_people(update.message, context)


async def no_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    context.user_data["new"]["link"] = None
    return await ask_people(q.message, context, edit=True)


def parse_person(text: str) -> tuple[str, str | None] | None:
    parts = text.split()
    if not parts:
        return None
    usernames = [p for p in parts if p.startswith("@") and len(p) > 1]
    username = usernames[0][1:] if usernames else None
    name = " ".join(p for p in parts if not p.startswith("@")) or (username or "")
    return (name[:100], username) if name else None


async def people_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    parsed = parse_person(update.message.text)
    if not parsed:
        await update.message.reply_text("Формат: <code>Имя @username</code>", parse_mode=ParseMode.HTML)
        return PEOPLE
    pid = db.add_person(*parsed)
    context.user_data["new"]["people"].add(pid)
    return await ask_people(update.message, context)


async def people_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    pid = int(q.data.split(":")[1])
    sel = context.user_data["new"]["people"]
    sel.symmetric_difference_update({pid})
    await q.edit_message_reply_markup(kb_people(sel))
    return PEOPLE


async def ask_date(message, edit: bool = True, year: int | None = None, month: int | None = None) -> int:
    now = datetime.now(TZ)
    text = ("🗓 Выбери <b>дату</b> дедлайна или напиши её: <code>25.11</code>, "
            "<code>25.11.2026</code>, <code>25.11 18:00</code>")
    markup = kb_calendar(year or now.year, month or now.month)
    if edit:
        await message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
    else:
        await message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
    return DATE


async def people_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    return await ask_date(q.message)


async def cal_nav(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    y, m = map(int, q.data.split(":")[1].split("-"))
    await q.edit_message_reply_markup(kb_calendar(y, m))
    return DATE


async def ask_hour(message, context, edit: bool = True) -> int:
    d: date = context.user_data["new"]["date"]
    text = (f"🕐 Дата: <b>{d:%d.%m.%Y}</b>. Выбери <b>час</b> или напиши время: <code>18:30</code>")
    if edit:
        await message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb_hours())
    else:
        await message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb_hours())
    return HOUR


async def cal_day(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    y, m, d = map(int, q.data.split(":")[1].split("-"))
    context.user_data["new"]["date"] = date(y, m, d)
    return await ask_hour(q.message, context)


async def date_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    parsed = logic.parse_date(update.message.text, datetime.now(TZ).date())
    if not parsed:
        await update.message.reply_text("Не понял дату. Пример: <code>25.11</code> или <code>25.11 18:00</code>",
                                        parse_mode=ParseMode.HTML)
        return DATE
    d, t = parsed
    context.user_data["new"]["date"] = d
    if t is None:
        return await ask_hour(update.message, context, edit=False)
    return await set_time(update.message, context, t, edit=False)


async def hour_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    h = int(q.data.split(":")[1])
    context.user_data["new"]["hour"] = h
    await q.edit_message_text("🕐 Выбери <b>минуты</b>:", parse_mode=ParseMode.HTML, reply_markup=kb_minutes(h))
    return MINUTE


async def minute_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    t = time(context.user_data["new"]["hour"], int(q.data.split(":")[1]))
    return await set_time(q.message, context, t, edit=True)


async def time_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    t = logic.parse_time(update.message.text)
    if not t:
        await update.message.reply_text("Не понял время. Пример: <code>18:30</code>", parse_mode=ParseMode.HTML)
        return HOUR
    return await set_time(update.message, context, t, edit=False)


async def back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    if q.data == "back:date":
        return await ask_date(q.message)
    return await ask_hour(q.message, context)


async def set_time(message, context, t: time, edit: bool) -> int:
    new = context.user_data["new"]
    due_ts = logic.to_ts(new["date"], t, TZ)
    if due_ts <= _time.time():
        text = "⚠️ Это время уже прошло. Выбери дату заново."
        if edit:
            await message.edit_text(text)
        else:
            await message.reply_text(text)
        return await ask_date(message, edit=False)
    new["due_ts"] = due_ts
    people = [p for p in db.people() if p["id"] in new["people"]]
    text = "Проверь:\n\n" + card(new["title"], new.get("link"), due_ts, people)
    left = logic.humanize_left(due_ts - int(_time.time()))
    upcoming = [LABELS[k] for k, _ in logic.REMINDERS if k not in logic.passed_reminders(due_ts, int(_time.time()))]
    text += f"\n\n⏳ До дедлайна: {left}."
    text += ("\n🔔 Напомню в канале за: " + ", ".join(upcoming) + ".") if upcoming else \
        "\n🔔 До дедлайна меньше 12 часов - будет только объявление."
    markup = InlineKeyboardMarkup([[Btn("✅ Сохранить", callback_data="save")],
                                   [Btn("⬅️ Изменить дату", callback_data="back:date"),
                                    Btn("❌ Отмена", callback_data="cancel")]])
    if edit:
        await message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=markup,
                                disable_web_page_preview=True)
    else:
        await message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup,
                                 disable_web_page_preview=True)
    return CONFIRM


async def save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    new = context.user_data.pop("new", None)
    if not new or "due_ts" not in new:
        await q.edit_message_text("Что-то пошло не так, начни заново: /new")
        return ConversationHandler.END
    did = db.add_deadline(new["title"], new.get("link"), new["due_ts"], update.effective_user.id,
                          sorted(new["people"]))
    row = db.deadline(did)
    ch = channel_id()
    status = ""
    if ch is None:
        status = "\n\n⚠️ Канал не настроен - объявление не отправлено. См. /help."
    else:
        try:
            left = logic.humanize_left(row["due_ts"] - int(_time.time()))
            await context.bot.send_message(
                ch, f"🆕 <b>Новый дедлайн</b> (осталось {left})\n\n" + deadline_card(row),
                parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            status = "\n\n📣 Объявление отправлено в канал."
        except TelegramError as e:
            status = f"\n\n⚠️ Не удалось написать в канал: {html.escape(str(e))}"
    await q.edit_message_text("✅ Сохранено!\n\n" + deadline_card(row) + status, parse_mode=ParseMode.HTML,
                              disable_web_page_preview=True)
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("new", None)
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text("Отменено.")
    else:
        await update.effective_message.reply_text("Отменено.")
    return ConversationHandler.END


async def ignore(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()


# ---------------------------------------------------------------- прочие команды

HELP = """🤖 Бот дедлайнов группы.

/new - добавить дедлайн
/list - актуальные дедлайны
/delete - удалить дедлайн
/people - список людей (ответственных)
/addperson Имя @username - добавить человека
/alias имя_в_таблице @username - чьё имя в таблице кому соответствует
/sync - перечитать Google-таблицу
/cancel - отменить ввод

Бот пишет в канал объявление о новом дедлайне и напоминает за неделю, за 2 суток, за сутки и за 12 часов.
Время - {tz}."""


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await allowed(update, context):
        return
    remember_user(update)
    text = HELP.format(tz=TZ.key)
    ch = channel_id()
    if ch is None:
        text += ("\n\n⚠️ Канал ещё не настроен. Перешли мне любой пост из канала (бот должен быть там админом) "
                 "или укажи CHANNEL_ID в настройках.")
    await update.message.reply_text(text)


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await allowed(update, context):
        return
    now = int(_time.time())
    items = [(r["due_ts"], deadline_card(r)) for r in db.active() if r["due_ts"] > now]
    items += [(r["due_ts"], sheet_card(r)) for r in db.sheet_active() if r["due_ts"] > now]
    if not items:
        await update.message.reply_text("Актуальных дедлайнов нет 🎉")
        return
    items.sort(key=lambda x: x[0])
    parts = [f"{text}\n⏳ осталось {logic.humanize_left(ts - now)}" for ts, text in items]
    # Telegram ограничивает сообщение 4096 символами - режем на части.
    chunk = "📋 <b>Актуальные дедлайны</b>"
    for part in parts:
        if len(chunk) + len(part) + 2 > 4000:
            await update.message.reply_text(chunk, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            chunk = part
        else:
            chunk += "\n\n" + part
    await update.message.reply_text(chunk, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await allowed(update, context):
        return
    rows = db.active()
    if not rows:
        await update.message.reply_text("Актуальных дедлайнов нет.")
        return
    kb = [[Btn(f"🗑 {r['title'][:40]} - {logic.from_ts(r['due_ts'], TZ):%d.%m %H:%M}",
               callback_data=f"ddel:{r['id']}")] for r in rows]
    kb.append([Btn("Закрыть", callback_data="close")])
    await update.message.reply_text("Какой дедлайн удалить?", reply_markup=InlineKeyboardMarkup(kb))


async def delete_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not await allowed(update, context):
        return
    await q.answer()
    did = int(q.data.split(":")[1])
    row = db.deadline(did)
    if not row:
        await q.edit_message_text("Этого дедлайна уже нет.")
        return
    await q.edit_message_text(
        "Удалить?\n\n" + deadline_card(row), parse_mode=ParseMode.HTML, disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup([
            [Btn("🗑 Удалить", callback_data=f"dyes:{did}:0")],
            [Btn("🗑 Удалить и сообщить в канале", callback_data=f"dyes:{did}:1")],
            [Btn("Не удалять", callback_data="close")],
        ]))


async def delete_yes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not await allowed(update, context):
        return
    await q.answer()
    _, did, notify = q.data.split(":")
    row = db.deadline(int(did))
    if not row:
        await q.edit_message_text("Этого дедлайна уже нет.")
        return
    text = deadline_card(row)
    db.delete(int(did))
    if notify == "1" and channel_id() is not None:
        try:
            await context.bot.send_message(channel_id(), "❌ <b>Дедлайн отменён</b>\n\n" + text,
                                           parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        except TelegramError as e:
            log.warning("cancel post failed: %s", e)
    await q.edit_message_text("Удалено:\n\n" + text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def close(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()
    await update.callback_query.message.delete()


def kb_people_admin() -> InlineKeyboardMarkup:
    kb = [[Btn(f"❌ {p['name']}" + (f" (@{p['username']})" if p["username"] else ""),
               callback_data=f"pdel:{p['id']}")] for p in db.people()]
    kb.append([Btn("Закрыть", callback_data="close")])
    return InlineKeyboardMarkup(kb)


PEOPLE_HELP = ("👥 <b>Люди</b>\n\nДобавить: <code>/addperson Имя @username</code>\n"
               "Кто напишет боту /start, добавляется сам.\nНажми на человека, чтобы убрать его из списка.")


async def people_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await allowed(update, context):
        return
    await update.message.reply_text(PEOPLE_HELP, parse_mode=ParseMode.HTML, reply_markup=kb_people_admin())


async def addperson_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await allowed(update, context):
        return
    parsed = parse_person(" ".join(context.args))
    if not parsed:
        await update.message.reply_text("Формат: <code>/addperson Имя @username</code>", parse_mode=ParseMode.HTML)
        return
    db.add_person(*parsed)
    await update.message.reply_text(f"Добавлен(а): {html.escape(parsed[0])}", parse_mode=ParseMode.HTML)


async def person_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not await allowed(update, context):
        return
    await q.answer("Удалено")
    db.del_person(int(q.data.split(":")[1]))
    await q.edit_message_reply_markup(kb_people_admin())


# ---------------------------------------------------------------- канал


async def my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Бота сделали админом канала - запоминаем канал."""
    cm = update.my_chat_member
    if cm.chat.type == ChatType.CHANNEL and cm.new_chat_member.status == ChatMember.ADMINISTRATOR:
        if not ENV_CHANNEL:
            db.set("channel_id", str(cm.chat.id))
            log.info("channel set: %s (%s)", cm.chat.id, cm.chat.title)


async def forwarded(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Пересланный пост из канала - узнаём ID и запоминаем, если бот там админ."""
    origin = update.message.forward_origin
    if not isinstance(origin, MessageOriginChannel):
        return
    ch = origin.chat
    try:
        me = await context.bot.get_chat_member(ch.id, context.bot.id)
        is_admin = me.status == ChatMember.ADMINISTRATOR and getattr(me, "can_post_messages", None) is not False
    except TelegramError:
        is_admin = False
    if not is_admin:
        await update.message.reply_text(
            f"ID канала «{ch.title}»: <code>{ch.id}</code>\n⚠️ Я не админ в нём (или нет права публиковать).",
            parse_mode=ParseMode.HTML)
        return
    if ENV_CHANNEL:
        await update.message.reply_text(
            f"ID канала: <code>{ch.id}</code>. Канал задан через CHANNEL_ID={html.escape(ENV_CHANNEL)}.",
            parse_mode=ParseMode.HTML)
        return
    current = db.get("channel_id")
    if current and current != str(ch.id) and not await allowed(update, context):
        return
    db.set("channel_id", str(ch.id))
    await update.message.reply_text(f"✅ Канал «{html.escape(ch.title)}» подключён. Уведомления будут приходить туда.",
                                    parse_mode=ParseMode.HTML)


# ---------------------------------------------------------------- Google-таблица


def sheet_person(name: str) -> str:
    """Имя из таблицы -> @username, если задано через /alias или совпадает с человеком из /people."""
    alias = db.get("alias:" + name.strip().lower())
    if alias:
        return "@" + html.escape(alias)
    for p in db.people():
        if p["name"].strip().lower() == name.strip().lower():
            return mention(p)
    return html.escape(name)


def sheet_card(row: sqlite3.Row) -> str:
    d = json.loads(row["data"])
    title = f"📌 <b>{html.escape(row['title'])}</b>"
    if d.get("stage"):
        title += f" - {html.escape(d['stage'])}"
    lines = [title, f"🗓 {logic.format_dt(logic.from_ts(row['due_ts'], TZ))}"]
    if d.get("link"):
        lines.append(f"🔗 {html.escape(d['link'])}")
    if d.get("people"):
        lines.append("👥 " + ", ".join(sheet_person(p) for p in d["people"]))
    if d.get("status"):
        lines.append(f"💬 {html.escape(d['status'][:200])}")
    if d.get("extra"):
        lines.append(f"ℹ️ {html.escape(d['extra'][:200])}")
    lines.append("<i>из таблицы</i>")
    return "\n".join(lines)


async def post(context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    ch = channel_id()
    if ch is None:
        return False
    try:
        await context.bot.send_message(ch, text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        return True
    except TelegramError as e:
        log.warning("channel post failed: %s", e)
        return False


async def sync_sheet(context: ContextTypes.DEFAULT_TYPE) -> sheet.ParseResult | None:
    """Перечитать таблицу. Новые и перенесённые дедлайны объявляются в канале."""
    if not SHEET_URL:
        return None
    try:
        text = await sheet.fetch_csv(SHEET_URL)
    except Exception as e:  # сеть, доступ - не роняем бота, просто запоминаем ошибку
        db.set("sheet_error", f"{datetime.now(TZ):%d.%m %H:%M}: {e}")
        log.warning("sheet fetch failed: %s", e)
        raise
    now = int(_time.time())
    res = sheet.parse_csv(text, datetime.now(TZ).date(), TZ)
    if res.missing:
        msg = "нет колонок: " + ", ".join(res.missing)
        db.set("sheet_error", f"{datetime.now(TZ):%d.%m %H:%M}: {msg}")
        raise ValueError(msg)
    db.set("sheet_error", "")
    first_sync = db.get("sheet_synced") is None
    keep: set[str] = set()
    for it in res.items:
        due_ts = int(it.due.timestamp())
        if it.done or due_ts <= now:
            continue
        keep.add(it.key)
        data = {"link": it.link, "people": it.people, "status": it.status, "stage": it.stage,
                "extra": it.extra, "row": it.row}
        old = db.sheet_item(it.key)
        passed = ",".join(sorted(logic.passed_reminders(due_ts, now)))
        if old is None:
            db.sheet_upsert(it.key, it.title, due_ts, data, passed)
            if not first_sync and SHEET_ANNOUNCE:
                left = logic.humanize_left(due_ts - now)
                await post(context, f"🆕 <b>Новый дедлайн в таблице</b> (осталось {left})\n\n"
                           + sheet_card(db.sheet_item(it.key)))
        elif old["due_ts"] != due_ts:
            db.sheet_upsert(it.key, it.title, due_ts, data, passed)
            if SHEET_ANNOUNCE and old["active"]:
                was = logic.format_dt(logic.from_ts(old["due_ts"], TZ))
                await post(context, f"📅 <b>Дедлайн изменён</b> (было: {was})\n\n" + sheet_card(db.sheet_item(it.key)))
        else:
            db.sheet_upsert(it.key, it.title, due_ts, data, None)
    db.sheet_deactivate_except(keep)
    db.set("sheet_synced", datetime.now(TZ).strftime("%d.%m %H:%M"))
    log.info("sheet synced: %d active, %d unparsed", len(keep), len(res.problems))
    return res


async def sync_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await sync_sheet(context)
    except Exception:
        pass  # ошибка уже записана, её видно в /sync


async def sync_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await allowed(update, context):
        return
    if not SHEET_URL:
        await update.message.reply_text("Таблица не подключена: укажи SHEET_URL в настройках бота.")
        return
    try:
        res = await sync_sheet(context)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Не удалось прочитать таблицу: {html.escape(str(e))}",
                                        parse_mode=ParseMode.HTML)
        return
    active = db.sheet_active()
    text = f"✅ Таблица прочитана. Актуальных дедлайнов из неё: {len(active)}."
    if res and res.problems:
        text += "\n\n⚠️ Не понял дату дедлайна (напоминаний по ним не будет):\n" + "\n".join(
            "• " + html.escape(p) for p in res.problems[:20])
    text += "\n\nСписок: /list"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def alias_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await allowed(update, context):
        return
    args = context.args
    if len(args) < 2 or not args[-1].startswith("@"):
        aliases = [(k[6:], v) for k, v in db.c.execute(
            "SELECT key, value FROM settings WHERE key LIKE 'alias:%' ORDER BY key").fetchall()]
        text = ("Как в таблице пишут имя -> чей это @username.\n"
                "Пример: <code>/alias айс @aisaar</code>\nУдалить: <code>/alias айс @-</code>")
        if aliases:
            text += "\n\n" + "\n".join(f"{html.escape(a)} → @{html.escape(u)}" for a, u in aliases)
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)
        return
    name = " ".join(args[:-1]).strip().lower()
    username = args[-1][1:]
    if username in ("", "-"):
        db.c.execute("DELETE FROM settings WHERE key=?", ("alias:" + name,))
        db.c.commit()
        await update.message.reply_text(f"Удалено: {name}")
        return
    db.set("alias:" + name, username)
    await update.message.reply_text(f"Готово: «{name}» в таблице → @{username}")


# ---------------------------------------------------------------- напоминания


async def check_reminders(context: ContextTypes.DEFAULT_TYPE) -> None:
    ch = channel_id()
    now = int(_time.time())
    for row in db.active():
        if now >= row["due_ts"]:
            db.mark_done(row["id"])
            continue
        if ch is None:
            continue
        sent = set(filter(None, row["sent"].split(",")))
        key, mark = logic.reminder_to_send(row["due_ts"], now, sent)
        if not key:
            continue
        left = logic.humanize_left(row["due_ts"] - now)
        icon = "🔥" if key in ("1d", "12h") else "⏰"
        text = f"{icon} <b>До дедлайна {left}!</b>\n\n" + deadline_card(row)
        try:
            await context.bot.send_message(ch, text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            db.mark_sent(row["id"], mark)
            log.info("reminder %s sent for #%s", key, row["id"])
        except TelegramError as e:
            log.warning("reminder for #%s failed: %s", row["id"], e)

    for row in db.sheet_active():
        if now >= row["due_ts"]:
            db.sheet_mark(row["key"], active=0)
            continue
        if ch is None:
            continue
        sent = set(filter(None, row["sent"].split(",")))
        key, mark = logic.reminder_to_send(row["due_ts"], now, sent)
        if not key:
            continue
        left = logic.humanize_left(row["due_ts"] - now)
        icon = "🔥" if key in ("1d", "12h") else "⏰"
        if await post(context, f"{icon} <b>До дедлайна {left}!</b>\n\n" + sheet_card(row)):
            db.sheet_mark(row["key"], sent=mark)
            log.info("reminder %s sent for sheet %s", key, row["title"])


# ---------------------------------------------------------------- запуск


async def post_init(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("new", "Добавить дедлайн"),
        BotCommand("list", "Актуальные дедлайны"),
        BotCommand("delete", "Удалить дедлайн"),
        BotCommand("people", "Список людей"),
        BotCommand("addperson", "Добавить человека"),
        BotCommand("sync", "Перечитать Google-таблицу"),
        BotCommand("alias", "Имена из таблицы"),
        BotCommand("help", "Помощь"),
    ])


def build_app() -> Application:
    if not BOT_TOKEN:
        raise SystemExit("Нет BOT_TOKEN. Скопируй .env.example в .env и впиши токен.")
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    private = filters.ChatType.PRIVATE
    text = filters.TEXT & ~filters.COMMAND & private & ~filters.FORWARDED
    cancel_cb = CallbackQueryHandler(cancel, pattern="^cancel$")

    conv = ConversationHandler(
        entry_points=[CommandHandler("new", new_start, filters=private)],
        states={
            TITLE: [MessageHandler(text, got_title)],
            LINK: [MessageHandler(text, got_link), CallbackQueryHandler(no_link, pattern="^nolink$")],
            PEOPLE: [MessageHandler(text, people_text),
                     CallbackQueryHandler(people_toggle, pattern=r"^tog:\d+$"),
                     CallbackQueryHandler(people_done, pattern="^people:done$")],
            DATE: [MessageHandler(text, date_text),
                   CallbackQueryHandler(cal_nav, pattern=r"^cal:\d+-\d+$"),
                   CallbackQueryHandler(cal_day, pattern=r"^day:\d+-\d+-\d+$")],
            HOUR: [MessageHandler(text, time_text),
                   CallbackQueryHandler(hour_pick, pattern=r"^hr:\d+$"),
                   CallbackQueryHandler(back, pattern="^back:date$")],
            MINUTE: [MessageHandler(text, time_text),
                     CallbackQueryHandler(minute_pick, pattern=r"^mn:\d+$"),
                     CallbackQueryHandler(back, pattern="^back:(hour|date)$")],
            CONFIRM: [CallbackQueryHandler(save, pattern="^save$"),
                      CallbackQueryHandler(back, pattern="^back:date$")],
        },
        fallbacks=[CommandHandler("cancel", cancel), cancel_cb, CommandHandler("new", new_start, filters=private)],
        allow_reentry=True,
    )
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(ignore, pattern="^ign$"))
    app.add_handler(cancel_cb)
    app.add_handler(CommandHandler(["start", "help"], start, filters=private))
    app.add_handler(CommandHandler("list", list_cmd, filters=private))
    app.add_handler(CommandHandler("delete", delete_cmd, filters=private))
    app.add_handler(CommandHandler("people", people_cmd, filters=private))
    app.add_handler(CommandHandler("addperson", addperson_cmd, filters=private))
    app.add_handler(CommandHandler("cancel", cancel, filters=private))
    app.add_handler(CommandHandler("sync", sync_cmd, filters=private))
    app.add_handler(CommandHandler("alias", alias_cmd, filters=private))
    app.add_handler(CallbackQueryHandler(delete_pick, pattern=r"^ddel:\d+$"))
    app.add_handler(CallbackQueryHandler(delete_yes, pattern=r"^dyes:\d+:[01]$"))
    app.add_handler(CallbackQueryHandler(person_delete, pattern=r"^pdel:\d+$"))
    app.add_handler(CallbackQueryHandler(close, pattern="^close$"))
    app.add_handler(MessageHandler(filters.FORWARDED & private, forwarded))
    app.add_handler(ChatMemberHandler(my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.job_queue.run_repeating(check_reminders, interval=60, first=20)
    if SHEET_URL:
        app.job_queue.run_repeating(sync_job, interval=SHEET_SYNC_MINUTES * 60, first=3)
    return app


def main() -> None:
    log.info("starting, tz=%s, db=%s, channel=%s, sheet=%s", TZ.key, DB_PATH, channel_id(), bool(SHEET_URL))
    build_app().run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
