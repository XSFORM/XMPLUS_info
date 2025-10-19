from __future__ import annotations

from datetime import datetime
import re
import unicodedata
import pytz

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
    """
    Нормализация строки даты:
    - Unicode NFKC (цифры, двоеточия, пробелы во фуллвиде → ASCII)
    - экзотические пробелы → обычный пробел
    - все виды тире/минуса → '-'
    - экзотические двоеточия → ':'
    - схлопываем кратные пробелы
    """
    s = unicodedata.normalize("NFKC", str(text))

    # Пробелы
    s = (
        s.replace("\u00A0", " ")  # NBSP
         .replace("\u202F", " ")  # NARROW NBSP
         .replace("\u2009", " ")  # THIN SPACE
         .replace("\u2002", " ")
         .replace("\u2003", " ")
    )

    # Все разновидности тире/минуса → '-'
    s = re.sub(r"[\u2010\u2011\u2012\u2013\u2014\u2212]", "-", s)

    # Экзотические двоеточия (полноширинные и т.п.) → ':'
    s = s.replace("\uFF1A", ":")

    # Кратные пробелы -> один
    s = re.sub(r"\s+", " ", s).strip()
    return s


def parse_datetime_human(text: str) -> datetime | None:
    """
    СТРОГИЙ формат: YYYY-MM-DD HH:MM:SS
    Пример: 2025-10-20 15:35:43
    """
    s = _clean_dt_text(text)
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})", s)
    if not m:
        return None
    y, mo, d, hh, mm, ss = map(int, m.groups())
    try:
        dt = datetime(y, mo, d, hh, mm, ss)
    except ValueError:
        return None
    return to_tz(dt)


def fmt_dt_human(dt: datetime) -> str:
    dt = to_tz(dt)
    return dt.strftime("%Y-%m-%d %H:%M:%S")