"""Общие помощники для раздела «🧾 Сервисы» (используются и хендлерами, и планировщиком)."""
from __future__ import annotations

import calendar
import re
from datetime import date, timedelta

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

PERIOD_LABELS = {1: "ежемесячно", 3: "раз в 3 мес.", 6: "раз в полгода", 12: "ежегодно", 24: "раз в 2 года"}
PERIOD_SHORT = {1: "мес", 3: "3 мес", 6: "6 мес", 12: "год", 24: "2 года"}


def period_label(months: int, days: int = 0) -> str:
    if days:
        return "ежедневно" if days == 1 else f"каждые {days} дн."
    return PERIOD_LABELS.get(months, f"раз в {months} мес.")


def period_short(months: int, days: int = 0) -> str:
    if days:
        return f"{days} дн"
    return PERIOD_SHORT.get(months, f"{months} мес")


def period_len_days(months: int, days: int = 0) -> int:
    """Примерная длина периода в днях (для итогов и графика напоминаний)."""
    return days if days else months * 30


def parse_period(text: str) -> tuple[int, int] | None:
    """«7д», «7 дней», «7d» → (0, 7); «2», «2м», «2 мес» → (2, 0). Возвращает (месяцы, дни)."""
    t = text.strip().lower().replace(" ", "")
    m = re.fullmatch(r"(\d+)(д|дн|дня|дней|d|day|days)", t)
    if m:
        d = int(m.group(1))
        return (0, d) if 1 <= d <= 366 else None
    m = re.fullmatch(r"(\d+)(м|мес|месяц|месяца|месяцев|m)?", t)
    if m:
        mo = int(m.group(1))
        return (mo, 0) if 1 <= mo <= 120 else None
    return None


def next_due(s) -> date:
    """Следующая дата оплаты после текущей (по дням или по месяцам)."""
    if getattr(s, "period_days", 0):
        return s.due_date + timedelta(days=s.period_days)
    return shift_months(s.due_date, s.period_months, s.billing_day)


def remind_days(s) -> tuple[int, ...]:
    """За сколько дней напоминать: 7, 3, 1, 0 — но не больше половины периода (для коротких)."""
    half = period_len_days(s.period_months, getattr(s, "period_days", 0)) // 2
    return tuple(d for d in (7, 3, 1, 0) if d <= max(half, 1))


def shift_months(d: date, months: int, billing_day: int | None = None) -> date:
    """Сдвиг даты на N месяцев с сохранением «якорного» дня (31 → 28/29/30 → снова 31)."""
    y = d.year + (d.month - 1 + months) // 12
    m = (d.month - 1 + months) % 12 + 1
    day = billing_day or d.day
    return date(y, m, min(day, calendar.monthrange(y, m)[1]))


def fmt_amount(amount: float, currency: str) -> str:
    if not amount:
        return "—"
    return f"{amount:g} {currency}"


CURRENCY_ALIASES = {
    "$": "$", "usd": "$", "дол": "$", "долл": "$",
    "€": "€", "eur": "€", "евро": "€",
    "₽": "₽", "р": "₽", "руб": "₽", "rub": "₽", "рублей": "₽",
    "tmt": "TMT", "ман": "TMT", "манат": "TMT",
}


def parse_amount_currency(text: str) -> tuple[float, str] | None:
    """«5.5», «5,5 $», «$5», «500 руб», «12 eur» → (сумма, валюта). Валюта по умолчанию — $."""
    t = text.strip().replace(",", ".")
    m = re.search(r"\d+(?:\.\d+)?", t)
    if not m:
        return None
    amount = float(m.group(0))
    rest = (t[: m.start()] + t[m.end():]).strip().lower()
    if not rest:
        return amount, "$"
    return amount, CURRENCY_ALIASES.get(rest, rest.upper()[:8])


def parse_date_simple(text: str, today: date) -> date | None:
    """YYYY-MM-DD, DD.MM.YYYY или DD.MM (ближайшая такая дата не раньше сегодня)."""
    t = text.strip()
    try:
        if re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", t):
            y, mo, d = map(int, t.split("-"))
            return date(y, mo, d)
        if re.fullmatch(r"\d{1,2}\.\d{1,2}\.\d{4}", t):
            d, mo, y = map(int, t.split("."))
            return date(y, mo, d)
        if re.fullmatch(r"\d{1,2}\.\d{1,2}", t):
            d, mo = map(int, t.split("."))
            cand = date(today.year, mo, d)
            return cand if cand >= today else date(today.year + 1, mo, d)
    except ValueError:
        return None
    return None


def days_left_text(days: int) -> str:
    if days > 0:
        return f"через {days} дн."
    if days == 0:
        return "сегодня"
    return f"просрочено на {-days} дн."


def svc_card(s) -> str:
    lines = [
        f"🧾 {s.name}",
        f"Сумма: {fmt_amount(s.amount, s.currency)}",
        f"Оплата: {s.due_date.isoformat()} ({period_label(s.period_months, s.period_days)})",
    ]
    if s.url:
        lines.append(f"Ссылка: {s.url}")
    if s.note:
        lines.append(f"Заметка: {s.note}")
    return "\n".join(lines)


def svc_notify_kb(s) -> InlineKeyboardMarkup:
    rows = []
    if s.url and s.url.startswith(("http://", "https://")):
        rows.append([InlineKeyboardButton(text="🔗 Открыть панель", url=s.url)])
    rows.append([
        InlineKeyboardButton(text="✅ Оплачено", callback_data=f"svc:paid:{s.id}"),
        InlineKeyboardButton(text="⏰ Напомнить завтра", callback_data=f"svc:snz:{s.id}"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)
