from __future__ import annotations

from datetime import datetime, timezone

from aiogram import Router, Bot
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    BotCommand,
    BotCommandScopeDefault,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
    MenuButtonCommands,
)
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext

from sqlalchemy import select, delete

from app.db import SessionLocal, Item
from app.utils import parse_datetime_human, fmt_dt_human, now_tz
from app.config import settings

router = Router()

# Команды для меню Telegram
BOT_COMMANDS = [
    BotCommand(command="start", description="Запуск бота"),
    BotCommand(command="help", description="Справка по командам"),
    BotCommand(command="add", description="Добавить (мастер: USERID → USERNAME → дата/время)"),
    BotCommand(command="list", description="Список записей"),
    BotCommand(command="next", description="Ближайшие истечения"),
    BotCommand(command="status", description="Статус бота"),
    BotCommand(command="timezone", description="Показать локальное время (TZ)"),
    BotCommand(command="cancel", description="Отменить текущий ввод"),
    BotCommand(command="menu", description="Показать клавиатуру"),
    BotCommand(command="hide", description="Скрыть клавиатуру"),
]


def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="/add"), KeyboardButton(text="/list")],
            [KeyboardButton(text="/next"), KeyboardButton(text="/status")],
            [KeyboardButton(text="/timezone"), KeyboardButton(text="/cancel")],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выберите команду…",
        selective=True,
    )


async def set_bot_commands(bot: Bot) -> None:
    await bot.set_my_commands(commands=BOT_COMMANDS, scope=BotCommandScopeDefault())
    try:
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    except Exception:
        pass


@router.message(CommandStart())
async def on_start(message: Message) -> None:
    await message.answer(
        "✅ XMPLUS запущен.\n"
        "Команды — в меню (кнопка с квадратами) и на клавиатуре ниже.",
        reply_markup=main_menu_kb(),
    )


@router.message(Command("help"))
async def on_help(message: Message) -> None:
    text = (
        "Доступные команды:\n"
        + "\n".join([f"/{c.command} — {c.description}" for c in BOT_COMMANDS])
        + "\n\nПодсказка: /menu — показать клавиатуру, /hide — скрыть."
    )
    await message.answer(text, reply_markup=main_menu_kb())


@router.message(Command("menu"))
async def show_menu(message: Message) -> None:
    await message.answer("Клавиатура показана.", reply_markup=main_menu_kb())


@router.message(Command("hide"))
async def hide_menu(message: Message) -> None:
    await message.answer("Клавиатура скрыта.", reply_markup=ReplyKeyboardRemove())


@router.message(Command("status"))
async def on_status(message: Message) -> None:
    async with SessionLocal() as session:
        total = (await session.execute(select(Item))).scalars().unique().all()
    await message.answer(
        f"Бот работает ✅\nВ базе записей: {len(total)}\nTIMEZONE: {settings.TIMEZONE}",
        reply_markup=main_menu_kb(),
    )


# ---- Показ текущего локального времени (без выбора/изменений) ----

@router.message(Command("timezone"))
async def show_timezone(message: Message) -> None:
    local_now = now_tz()
    utc_now = datetime.now(timezone.utc)
    offset_td = local_now.utcoffset() or timezone.utc.utcoffset(utc_now)
    # формат смещения +05:00/-03:00
    total_minutes = int(offset_td.total_seconds() // 60) if offset_td else 0
    sign = "+" if total_minutes >= 0 else "-"
    hh = abs(total_minutes) // 60
    mm = abs(total_minutes) % 60
    offset_str = f"{sign}{hh:02d}:{mm:02d}"

    text = (
        f"Часовой пояс бота: {settings.TIMEZONE} (UTC{offset_str})\n"
        f"Локальное время: {local_now.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
        f"UTC:            {utc_now.strftime('%Y-%m-%d %H:%M:%S UTC')}"
    )
    await message.answer(text, reply_markup=main_menu_kb())


# ==== Мастер добавления ====

class AddStates(StatesGroup):
    waiting_user_id = State()
    waiting_username = State()
    waiting_duedatetime = State()


@router.message(Command("cancel"))
async def on_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.", reply_markup=main_menu_kb())


@router.message(Command("add"))
async def add_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(AddStates.waiting_user_id)
    await message.answer("Шаг 1/3. Введите USER ID (число):", reply_markup=main_menu_kb())


@router.message(AddStates.waiting_user_id)
async def add_user_id(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer("USER ID должен быть числом. Попробуйте ещё раз или /cancel.", reply_markup=main_menu_kb())
        return
    await state.update_data(user_id=int(text))
    await state.set_state(AddStates.waiting_username)
    await message.answer("Шаг 2/3. Введите USERNAME (например, XmADMIN):", reply_markup=main_menu_kb())


@router.message(AddStates.waiting_username)
async def add_username(message: Message, state: FSMContext) -> None:
    username = (message.text or "").strip()
    if not username:
        await message.answer("USERNAME не может быть пустым. Попробуйте ещё раз или /cancel.", reply_markup=main_menu_kb())
        return
    await state.update_data(username=username)
    await state.set_state(AddStates.waiting_duedatetime)
    await message.answer(
        "Шаг 3/3. Введите дату и время отключения строго в формате:\n"
        "YYYY-MM-DD HH:MM:SS\n"
        "Пример: 2025-10-20 15:35:43",
        reply_markup=main_menu_kb(),
    )


@router.message(AddStates.waiting_duedatetime)
async def add_duedatetime(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    dt = parse_datetime_human(text)
    if not dt:
        await message.answer(
            "Неверный формат. Используйте только YYYY-MM-DD HH:MM:SS, например: 2025-10-20 15:35:43\n"
            "Попробуйте ещё раз или /cancel.",
            reply_markup=main_menu_kb(),
        )
        return

    data = await state.get_data()
    user_id = data["user_id"]
    username = data["username"]

    async with SessionLocal() as session:
        item = Item(
            user_id=user_id,
            username=username,
            due_date=dt,
            chat_id=message.chat.id,
        )
        session.add(item)
        await session.commit()
        await session.refresh(item)

    await state.clear()
    await message.answer(
        f"Добавлено: [{item.id}] USERID={user_id}, USERNAME={username}, DUE={fmt_dt_human(dt)}",
        reply_markup=main_menu_kb(),
    )


# ==== Списки/удаление/ближайшие ====

@router.message(Command("list"))
async def on_list(message: Message) -> None:
    async with SessionLocal() as session:
        result = await session.execute(select(Item).order_by(Item.due_date.asc()))
        items = result.scalars().all()

    if not items:
        await message.answer("Список пуст.", reply_markup=main_menu_kb())
        return

    lines = [f"[{it.id}] {it.user_id} | {it.username} | {fmt_dt_human(it.due_date)}" for it in items]
    header = "ID | USERID | USERNAME | DUE DATE\n" + "-" * 40
    await message.answer(header + "\n" + "\n".join(lines), reply_markup=main_menu_kb())


@router.message(Command("remove"))
async def on_remove(message: Message) -> None:
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Использование: /remove <id>", reply_markup=main_menu_kb())
        return
    item_id = int(parts[1])

    async with SessionLocal() as session:
        await session.execute(delete(Item).where(Item.id == item_id))
        await session.commit()

    await message.answer(f"Удалено (если было): id={item_id}", reply_markup=main_menu_kb())


@router.message(Command("next"))
async def on_next(message: Message) -> None:
    async with SessionLocal() as session:
        result = await session.execute(select(Item).order_by(Item.due_date.asc()).limit(10))
        items = result.scalars().all()

    if not items:
        await message.answer("Нет ближайших истечений.", reply_markup=main_menu_kb())
        return

    lines = [f"[{it.id}] {it.user_id} | {it.username} | {fmt_dt_human(it.due_date)}" for it in items]
    await message.answer("Ближайшие:\n" + "\n".join(lines), reply_markup=main_menu_kb())