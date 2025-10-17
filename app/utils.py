from __future__ import annotations

from datetime import datetime
import pytz

from app.config import settings


def now_tz() -> datetime:
    tz = pytz.timezone(settings.TIMEZONE)
    return datetime.now(tz)