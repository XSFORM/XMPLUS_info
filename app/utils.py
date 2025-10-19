from __future__ import annotations

from datetime import datetime
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


def parse_datetime_human(text: str) -> datetime | None:
    """
    Гибкий парсер даты/времени: '2025-11-06 15:35:43', '31.01.2026 04:39', '2026-01-15 04:27:09', и т.д.
    Если время не указано, берём 00:00:00 этого дня.
    """
    try:
        dt = parser.parse(text, dayfirst=True, yearfirst=False, fuzzy=True, default=datetime(2000, 1, 1, 0, 0, 0))
        dt = to_tz(dt)
        return dt
    except Exception:
        return None


def fmt_dt_human(dt: datetime) -> str:
    dt = to_tz(dt)
    return dt.strftime("%Y-%m-%d %H:%M:%S")