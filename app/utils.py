from __future__ import annotations

from datetime import datetime
import re
import pytz
from dateutil import parser

from app.config import settings


def now_tz() -> datetime:
    tz = pytz.timezone(settings.TIMEZONE)
    return datetime.now(tz)


def to_tz(dt: datetime) -> datetime:
    tz = pytz.timezone(settings.TIMEZONE)
    if dt.tzinfo is None:
        return tz.localize(dt)
    return dt.astimezone(tz)


def _clean_dt_text(text: str) -> str:
    # Нормализуем экзотические пробелы/тире
    s = str(text)
    s = (
        s.replace("\u00A0", " ")
         .replace("\u202F", " ")
         .replace("\u2009", " ")
         .replace("\u2002", " ")
         .replace("\u2003", " ")
    )
    s = re.sub(r"[–—−]", "-", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def parse_datetime_human(text: str) -> datetime | None:
    """
    Жёсткий парсер:
    - YYYY-MM-DD HH:MM[:SS]
    - DD.MM.YYYY HH:MM[:SS]
    - YYYY-MM-DD (время = 00:00:00)
    - DD.MM.YYYY (время = 00:00:00)
    Если не распознали — fallback на dateutil.parse (dayfirst=True).
    """
    s = _clean_dt_text(text)

    # 1) ISO: 2025-10-20 22:35 или 22:35:43
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2}))?)?", s)
    if m:
        y, mo, d = map(int, m.group(1, 2, 3))
        hh = int(m.group(4) or 0)
        mm = int(m.group(5) or 0)
        ss = int(m.group(6) or 0)
        return to_tz(datetime(y, mo, d, hh, mm, ss))

    # 2) DMY: 31.01.2026 04:39 или 04:39:05
    m = re.fullmatch(r"(\d{2})\.(\d{2})\.(\d{4})(?:\s+(\d{2}):(\d{2})(?::(\d{2}))?)?", s)
    if m:
        d, mo, y = map(int, m.group(1, 2, 3))
        hh = int(m.group(4) or 0)
        mm = int(m.group(5) or 0)
        ss = int(m.group(6) or 0)
        return to_tz(datetime(y, mo, d, hh, mm, ss))

    # 3) Fallback: dateutil (на случай «чудо-строк»)
    try:
        base = datetime(2000, 1, 1, 0, 0, 0)
        dt2 = parser.parse(s, dayfirst=True, yearfirst=False, fuzzy=True, default=base)
        return to_tz(dt2)
    except Exception:
        return None


def fmt_dt_human(dt: datetime) -> str:
    dt = to_tz(dt)
    return dt.strftime("%Y-%m-%d %H:%M:%S")