from __future__ import annotations

from datetime import datetime, timedelta, timezone
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


def tz_offset_str() -> str:
    """
    Возвращает строку смещения локальной TZ формата +05:00 / -03:00.
    """
    local_now = now_tz()
    offset = local_now.utcoffset() or timedelta(0)
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    hh = abs(total_minutes) // 60
    mm = abs(total_minutes) % 60
    return f"{sign}{hh:02d}:{mm:02d}"


def _clean_dt_text(text: str) -> str:
    s = unicodedata.normalize("NFKC", str(text))
    s = (
        s.replace("\u00A0", " ")
         .replace("\u202F", " ")
         .replace("\u2009", " ")
         .replace("\u2002", " ")
         .replace("\u2003", " ")
    )
    s = re.sub(r"[\u2010\u2011\u2012\u2013\u2014\u2212]", "-", s)
    s = s.replace("\uFF1A", ":")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def parse_datetime_human(text: str) -> datetime | None:
    # Строгий формат: YYYY-MM-DD HH:MM:SS
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