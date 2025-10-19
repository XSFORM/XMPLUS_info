from __future__ import annotations

from datetime import datetime
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


def parse_date_human(text: str) -> datetime | None:
    """
    Парсер человеческих дат: поддерживает YYYY-MM-DD, DD.MM.YYYY, '31 Dec 2025', и т.п.
    Возвращает aware datetime в таймзоне настроек (начало суток).
    """
    try:
        dt = parser.parse(text, dayfirst=True, yearfirst=False, fuzzy=True)
        dt = to_tz(dt)
        # нормализуем на 00:00:00, чтобы «день истечения» считался целиком
        return dt.replace(hour=0, minute=0, second=0, microsecond=0)
    except Exception:
        return None


def fmt_dt_human(dt: datetime) -> str:
    dt = to_tz(dt)
    return dt.strftime("%d.%m.%Y %H:%M %Z")