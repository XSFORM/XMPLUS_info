from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

from app.config import settings

try:
    from zoneinfo import ZoneInfo
except Exception:  # python<3.9 fallback (не планируем)
    ZoneInfo = None  # type: ignore


# Файл-переключатель активной TZ (читается на каждом вызове)
TZ_OVERRIDE_FILE = Path("/app/.tz_override")

# Допуск форматов ввода времени (строгий YYYY-MM-DD HH:MM:SS)
DT_FORMAT = "%Y-%m-%d %H:%M:%S"


def _safe_zoneinfo(tz_name: str):
    if ZoneInfo is None:
        raise RuntimeError("Python >= 3.9 required for zoneinfo")
    return ZoneInfo(tz_name)


def get_active_timezone_name() -> str:
    """
    Текущее имя часового пояса.
    Приоритет: содержимое /app/.tz_override -> settings.TIMEZONE
    """
    try:
        if TZ_OVERRIDE_FILE.exists():
            v = TZ_OVERRIDE_FILE.read_text(encoding="utf-8").strip()
            if v:
                return v
    except Exception:
        pass
    return settings.TIMEZONE


def get_active_timezone():
    return _safe_zoneinfo(get_active_timezone_name())


def set_active_timezone_name(tz_name: str) -> bool:
    """
    Установить активную TZ. Возвращает True при успехе.
    """
    try:
        # Проверим валидность tz
        _ = _safe_zoneinfo(tz_name)
        TZ_OVERRIDE_FILE.write_text(tz_name + "\n", encoding="utf-8")
        return True
    except Exception:
        return False


def now_tz() -> datetime:
    """
    Текущее локальное время в активной TZ (tz-aware).
    """
    return datetime.now(get_active_timezone())


def to_tz(dt: datetime) -> datetime:
    """
    Перевести datetime в активную TZ.
    - Если dt naive — считаем, что dt был в активной TZ и просто делаем его aware.
    - Если dt aware — конвертируем в активную TZ.
    """
    tz = get_active_timezone()
    if getattr(dt, "tzinfo", None) is None:
        return dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


def fmt_dt_human(dt: Optional[datetime]) -> str:
    """
    Человеческое форматирование даты в активной TZ.
    """
    if dt is None:
        return "-"
    return to_tz(dt).strftime(DT_FORMAT)


def parse_datetime_human(s: str) -> Optional[datetime]:
    """
    Парсинг строки строго в формате YYYY-MM-DD HH:MM:SS как локальное время активной TZ.
    Возвращает tz-aware datetime в активной TZ или None, если формат некорректен.
    """
    s = (s or "").strip()
    try:
        base = datetime.strptime(s, DT_FORMAT)
    except Exception:
        return None
    # Локализуем в активную TZ
    return base.replace(tzinfo=get_active_timezone())


def tz_offset_str() -> str:
    """
    Строка смещения активной TZ вида +05:00 / +08:00.
    """
    dt = now_tz()
    offset = dt.utcoffset() or (dt - dt)
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    hh = abs(total_minutes) // 60
    mm = abs(total_minutes) % 60
    return f"{sign}{hh:02d}:{mm:02d}"