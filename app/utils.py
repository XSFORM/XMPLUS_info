from __future__ import annotations

from datetime import datetime
import re
import unicodedata
import pytz
from dotenv import find_dotenv

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


# ---- Timezone helpers ----

def is_valid_timezone(tz_name: str) -> bool:
    return tz_name in pytz.all_timezones


def common_timezones() -> list[str]:
    # Под рукой — кнопки с самыми частыми вариантами
    return [
        "Europe/Moscow",
        "Europe/Kyiv",
        "Asia/Tashkent",
        "Asia/Almaty",
        "Asia/Bishkek",
        "Asia/Yekaterinburg",
        "UTC",
    ]


def update_dotenv_var(key: str, value: str) -> str | None:
    """
    Обновляет/добавляет переменную в .env.
    Возвращает путь к файлу .env, если найден/создан, иначе None.
    """
    path = find_dotenv(usecwd=True)
    if not path:
        # пробуем стандартное расположение в контейнере
        path = "/app/.env"
    try:
        # читаем текущие строки (если файла нет — создадим)
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()
        except FileNotFoundError:
            lines = []

        key_found = False
        out_lines: list[str] = []
        for line in lines:
            if line.startswith(f"{key}="):
                out_lines.append(f"{key}={value}")
                key_found = True
            else:
                out_lines.append(line)
        if not key_found:
            out_lines.append(f"{key}={value}")

        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(out_lines) + "\n")
        return path
    except Exception:
        return None