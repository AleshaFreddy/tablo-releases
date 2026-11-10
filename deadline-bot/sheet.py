"""Дедлайны из Google-таблицы: загрузка CSV и разбор строк."""

from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, tzinfo

from logic import MONTHS_GEN

# Колонки ищутся по заголовку (без учёта регистра), порядок колонок не важен.
# Сначала - названия с листа «Все дедлайны», потом - со старого листа «26/27».
COLUMNS = {
    "title": ["мероприятие", "название мероприятия", "название"],
    "link": ["ссылка", "сайт", "ссылка на сайт"],
    "people": ["ответственные", "(со)участие", "участники"],
    "status": ["статус", "статусы"],
    "deadline": ["дедлайн", "ближайший дедлайн"],
    "stage": ["этап", "что сдаём"],
    "extra": ["даты мероприятия", "дедлайн конкурса"],
    "category": ["категория"],
    "kind": ["тип"],
    "comment": ["комментарий"],
}

# Статусы, при которых напоминать уже не нужно.
DONE_WORDS = ["сдано", "подано", "подала", "подал", "не участвуем", "отказ", "готово", "неактуально", "не актуально"]

END_OF_DAY = time(23, 59)

_MONTHS = {m[:3]: i + 1 for i, m in enumerate(MONTHS_GEN)}
_MONTHS["мая"] = 5
_RU_DATE = re.compile(
    r"(?P<d>\d{1,2})\s+(?P<m>[а-яё]+)\.?(?:\s+(?P<y>\d{4})\s*(?:г\.?|года)?)?(?:\s*,?\s*(?:в\s*)?(?P<h>\d{1,2})[:.](?P<mi>\d{2}))?",
    re.I,
)
_NUM_DATE = re.compile(
    r"(?P<d>\d{1,2})\.(?P<m>\d{1,2})(?:\.(?P<y>\d{2,4}))?(?:\s*,?\s*(?P<h>\d{1,2}):(?P<mi>\d{2}))?"
)


@dataclass
class SheetItem:
    key: str
    title: str
    link: str | None
    people: list[str]
    status: str
    due: datetime
    stage: str  # что именно сдаём: «заявка», «рега»...
    extra: str
    row: int
    done: bool = False
    category: str = ""
    kind: str = ""
    comment: str = ""


@dataclass
class ParseResult:
    items: list[SheetItem] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)  # строки, где дедлайн не разобрать
    missing: list[str] = field(default_factory=list)   # колонки, которых нет


def _nearest_year(day: int, month: int, today: date) -> date | None:
    best = None
    for y in (today.year - 1, today.year, today.year + 1):
        try:
            d = date(y, month, day)
        except ValueError:
            continue
        if best is None or abs((d - today).days) < abs((best - today).days):
            best = d
    return best


def parse_sheet_date(text: str, today: date, tz: tzinfo) -> tuple[datetime, str] | None:
    """«10 ноября», «15 ноября, 23:59», «8 ноября - заявка», «25.11.2026 18:00».

    Возвращает (момент дедлайна, остаток текста). Без времени - 23:59.
    Без года - ближайшая к сегодняшнему дню такая дата.
    """
    text = (text or "").strip()
    if not text:
        return None
    month = None
    for m in _RU_DATE.finditer(text):
        month = _MONTHS.get(m.group("m").lower()[:3])
        if month:
            break
    if not month:
        m = _NUM_DATE.search(text)
        if not m:
            return None
        month = int(m.group("m"))
    day = int(m.group("d"))
    y = m.group("y")
    try:
        if y:
            year = int(y) + (2000 if len(y) == 2 else 0)
            d = date(year, month, day)
        else:
            d = _nearest_year(day, month, today)
            if d is None:
                return None
        t = END_OF_DAY
        if m.group("h") is not None:
            t = time(int(m.group("h")), int(m.group("mi")))
    except ValueError:
        return None
    rest = (text[: m.start()] + " " + text[m.end():]).strip(" -–—,:;\n\t")
    rest = re.sub(r"\s+", " ", rest)
    return datetime.combine(d, t, tzinfo=tz), rest


def _find_columns(header: list[str]) -> dict[str, int]:
    norm = [h.strip().lower() for h in header]
    found = {}
    for key, names in COLUMNS.items():
        for name in names:
            if name in norm:
                found[key] = norm.index(name)
                break
    return found


def split_people(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"[,;\n]+", text or "") if p.strip()]


def item_key(title: str) -> str:
    norm = re.sub(r"\W+", " ", title.lower()).strip()
    return hashlib.sha1(norm.encode()).hexdigest()[:16]


def parse_csv(text: str, today: date, tz: tzinfo) -> ParseResult:
    res = ParseResult()
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        res.missing = ["вся таблица пустая"]
        return res
    cols = _find_columns(rows[0])
    for need in ("title", "deadline"):
        if need not in cols:
            res.missing.append(COLUMNS[need][0])
    if res.missing:
        return res

    def cell(row: list[str], key: str) -> str:
        i = cols.get(key)
        return row[i].strip() if i is not None and i < len(row) else ""

    seen: dict[str, int] = {}
    for n, row in enumerate(rows[1:], start=2):
        title = cell(row, "title")
        if not title:
            continue
        raw = cell(row, "deadline")
        status = cell(row, "status")
        parsed = parse_sheet_date(raw, today, tz)
        if not parsed:
            if raw:
                res.problems.append(f"строка {n} «{title[:60]}»: «{raw[:40]}»")
            continue
        due, rest = parsed
        stage = cell(row, "stage") or rest
        key = item_key(title)
        if key in seen:  # одинаковые названия - различаем по этапу
            key = item_key(f"{title} {stage}")
        seen[key] = n
        link = cell(row, "link")
        res.items.append(SheetItem(
            key=key, title=title, link=link if link.startswith("http") else None,
            people=split_people(cell(row, "people")), status=status, due=due, stage=stage,
            extra=cell(row, "extra"), row=n,
            done=any(w in status.lower() for w in DONE_WORDS),
            category=cell(row, "category"), kind=cell(row, "kind"), comment=cell(row, "comment"),
        ))
    return res


def csv_url(url: str) -> str:
    """Ссылка на таблицу в любом виде -> ссылка на CSV одного листа."""
    url = url.strip()
    if "output=csv" in url or "format=csv" in url:
        return url
    m = re.search(r"/spreadsheets/(?:u/\d+/)?d/([a-zA-Z0-9_-]+)", url)
    if not m:
        raise ValueError("не похоже на ссылку на Google-таблицу")
    gid = re.search(r"[#&?]gid=(\d+)", url)
    out = f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=csv"
    if gid:
        out += f"&gid={gid.group(1)}"
    return out


async def fetch_csv(url: str) -> str:
    import httpx

    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
        r = await client.get(csv_url(url))
    ctype = r.headers.get("content-type", "")
    if r.status_code != 200 or "text/html" in ctype:
        raise PermissionError(
            "таблица не открывается без входа в Google. Включите доступ «Все, у кого есть ссылка - читатель» "
            "или опубликуйте лист в CSV (Файл - Поделиться - Опубликовать в интернете)."
        )
    r.encoding = "utf-8"
    return r.text
