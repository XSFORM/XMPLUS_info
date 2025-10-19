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
    """
    Делает datetime timezone-aware в таймзоне настроек, если он naive.
    """
    tz = pytz.timezone(settings.TIMEZONE)
    if dt.tzinfo is None:
        return tz.localize(dt)
    return dt.astimezone(tz)


def _clean_dt_text(text: str) -> str:
    """
    Нормализует строку даты/времени:
    - заменяет неразрывные/тонкие пробелы на обычный
    - убирает повторяющиеся пробелы
    - нормализует тире к дефису (на всякий случай)
    """
    s = str(text)
    s = (
        s.replace("\u00A0", " ")   # NBSP
         .replace("\u202F", " ")   # NARROW NBSP
         .replace("\u2009", " ")   # THIN SPACE
         .replace("\u2002", " ")
         .replace("\u2003", " ")
    )
    s = re.sub(r"[–—−]", "-", s)   # en/em dash → hyphen-minus
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _try_strptime(s: str) -> datetime | None:
    """
    Строгие форматы, которые ожидаем чаще всего.
    """
    fmts = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%d.%m.%Y %H:%M:%S",
        "%d.%m.%Y %H:%M",
        "%Y-%m-%d",
        "%d.%m.%Y",
    ]
    for fmt in fmts:
        try:
            dt = datetime.strptime(s, fmt)
            return to_tz(dt)
        except ValueError:
            continue
    return None


def parse_datetime_human(text: str) -> datetime | None:
    """
    Гибкий парсер даты/времени.
    1) Пытаемся строгие форматы:
       - YYYY-MM-DD HH:MM:SS
       - YYYY-MM-DD HH:MM
       - DD.MM.YYYY HH:MM:SS
       - DD.MM.YYYY HH:MM
       - YYYY-MM-DD
       - DD.MM.YYYY
    2) Если не получилось — используем dateutil.parse с dayfirst=True.
       Если время опущено, берём 00:00:00 локальной TZ.
    """
    s = _clean_dt_text(text)

    dt = _try_strptime(s)
    if dt:
        return dt

    try:
        # default задаёт время по умолчанию, если в строке нет времени
        base = datetime(2000, 1, 1, 0, 0, 0)
        dt2 = parser.parse(s, dayfirst=True, yearfirst=False, fuzzy=True, default=base)
        return to_tz(dt2)
    except Exception:
        return None


def fmt_dt_human(dt: datetime) -> str:
    dt = to_tz(dt)
    return dt.strftime("%Y-%m-%d %H:%M:%S")