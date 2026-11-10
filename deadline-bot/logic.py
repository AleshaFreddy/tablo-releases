"""Чистая логика без Telegram: когда слать напоминания, как писать время."""

from __future__ import annotations

import re
from datetime import date, datetime, time, tzinfo

HOUR = 3600
DAY = 24 * HOUR

# За сколько до дедлайна напоминать: (ключ, секунды).
REMINDERS: list[tuple[str, int]] = [
    ("7d", 7 * DAY),
    ("2d", 2 * DAY),
    ("1d", 1 * DAY),
    ("12h", 12 * HOUR),
]


def passed_reminders(due_ts: int, now_ts: int) -> set[str]:
    """Напоминания, время которых уже наступило (их не нужно слать при создании)."""
    return {key for key, off in REMINDERS if now_ts >= due_ts - off}


def reminder_to_send(due_ts: int, now_ts: int, sent: set[str]) -> tuple[str | None, set[str]]:
    """Какое напоминание отправить сейчас.

    Возвращает (ключ или None, ключи, которые надо пометить отправленными).
    Если бот был выключен и пропустил несколько порогов, отправляется
    одно - самое свежее, остальные просто помечаются.
    """
    if now_ts >= due_ts:
        return None, set()
    due_now = [(key, off) for key, off in REMINDERS if now_ts >= due_ts - off and key not in sent]
    if not due_now:
        return None, set()
    key = min(due_now, key=lambda kv: kv[1])[0]
    return key, {k for k, _ in due_now}


def _plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def humanize_left(seconds: int) -> str:
    """Сколько осталось, по-русски: «неделя», «2 суток», «12 часов», «1 сутки 5 часов»."""
    hours = max(0, round(seconds / HOUR))
    if hours == 0:
        minutes = max(1, round(seconds / 60))
        return f"{minutes} {_plural(minutes, 'минута', 'минуты', 'минут')}"
    days, hours = divmod(hours, 24)
    if days == 7 and hours == 0:
        return "неделя"
    if days == 1 and hours == 0:
        return "сутки"
    parts = []
    if days:
        parts.append(f"{days} {_plural(days, 'сутки', 'суток', 'суток')}")
    if hours:
        parts.append(f"{hours} {_plural(hours, 'час', 'часа', 'часов')}")
    return " ".join(parts)


WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
MONTHS_GEN = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]
MONTHS = [
    "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
]


def format_dt(dt: datetime) -> str:
    """«25 ноября (ср), 18:00»."""
    return f"{dt.day} {MONTHS_GEN[dt.month - 1]} ({WEEKDAYS[dt.weekday()]}), {dt:%H:%M}"


_DATE_RE = re.compile(r"^\s*(\d{1,2})[./-](\d{1,2})(?:[./-](\d{2,4}))?\s*(?:(\d{1,2})[:.](\d{2}))?\s*$")
_TIME_RE = re.compile(r"^\s*(\d{1,2})[:.](\d{2})\s*$")


def parse_time(text: str) -> time | None:
    m = _TIME_RE.match(text)
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        return None
    return time(h, mi)


def parse_date(text: str, today: date) -> tuple[date, time | None] | None:
    """«25.11», «25.11.2026», «25.11.26 18:00». Без года - ближайшая такая дата в будущем."""
    m = _DATE_RE.match(text)
    if not m:
        return None
    d, mo, y, h, mi = m.groups()
    try:
        if y:
            year = int(y) + (2000 if len(y) == 2 else 0)
            day = date(year, int(mo), int(d))
        else:
            day = date(today.year, int(mo), int(d))
            if day < today:
                day = date(today.year + 1, int(mo), int(d))
        t = None
        if h is not None:
            if int(h) > 23 or int(mi) > 59:
                return None
            t = time(int(h), int(mi))
    except ValueError:
        return None
    return day, t


def to_ts(day: date, t: time, tz: tzinfo) -> int:
    return int(datetime.combine(day, t, tzinfo=tz).timestamp())


def from_ts(ts: int, tz: tzinfo) -> datetime:
    return datetime.fromtimestamp(ts, tz)


def month_shift(year: int, month: int, delta: int) -> tuple[int, int]:
    idx = year * 12 + (month - 1) + delta
    return idx // 12, idx % 12 + 1


__all__ = [
    "REMINDERS", "passed_reminders", "reminder_to_send", "humanize_left",
    "format_dt", "parse_date", "parse_time", "to_ts", "from_ts", "month_shift",
    "MONTHS",
]
