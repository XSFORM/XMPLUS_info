from __future__ import annotations

from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

from app.config import settings
from app.db import Item
from app.utils import fmt_dt_human


def is_dealer_mode() -> bool:
    return settings.BOT_MODE == "dealer"


def main_menu_kb() -> ReplyKeyboardMarkup:
    if is_dealer_mode():
        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="/list"), KeyboardButton(text="/disabled")],
                [KeyboardButton(text="/next"), KeyboardButton(text="/status")],
            ],
            resize_keyboard=True,
            input_field_placeholder="Выберите команду…",
            selective=True,
        )
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Добавить"), KeyboardButton(text="🔄 Продлить"), KeyboardButton(text="🗑 Удалить")],
            [KeyboardButton(text="📋 Список"), KeyboardButton(text="⏰ Ближайшие"), KeyboardButton(text="⛔ Отключённые")],
            [KeyboardButton(text="✏️ Редактор"), KeyboardButton(text="👥 Дилеры"), KeyboardButton(text="💰 Баланс")],
            [KeyboardButton(text="💳 Оплата"), KeyboardButton(text="📡 Роутеры"), KeyboardButton(text="❌ Отмена")],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выберите команду…",
        selective=True,
    )


def confirm_kb(prefix: str, show_edit: bool = True) -> InlineKeyboardMarkup:
    buttons = [InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"{prefix}:ok")]
    if show_edit:
        buttons.append(InlineKeyboardButton(text="✏️ Изменить", callback_data=f"{prefix}:edit"))
    buttons.append(InlineKeyboardButton(text="❌ Отмена", callback_data=f"{prefix}:cancel"))
    return InlineKeyboardMarkup(inline_keyboard=[buttons])


def choose_by_due_kb(prefix: str, items: list[Item], extra_row: list[InlineKeyboardButton] | None = None) -> InlineKeyboardMarkup:
    buttons = []
    for it in items:
        label = f"{fmt_dt_human(it.due_date)} • {it.username}"
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"{prefix}:choose:{it.id}")])
    if extra_row:
        buttons.append(extra_row)
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ---- helpers: длинный текст, CSV-экспорт и аккуратные таблицы без ID ----

MESSAGE_LIMIT = 3900  # запас к лимиту 4096


def dealer_user_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Добавить"), KeyboardButton(text="🔄 Продлить")],
            [KeyboardButton(text="📋 Список"), KeyboardButton(text="⛔ Отключённые")],
            [KeyboardButton(text="⏰ Ближайшие"), KeyboardButton(text="✏️ Редактор")],
            [KeyboardButton(text="💰 Баланс"), KeyboardButton(text="💳 Оплата")],
            [KeyboardButton(text="📊 Статус")],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выберите команду…",
        selective=True,
    )


