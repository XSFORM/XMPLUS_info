from __future__ import annotations

from typing import Optional

from aiogram import Router, Bot, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, BotCommand, BotCommandScopeDefault
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext

from sqlalchemy import select, delete

from app.db import SessionLocal, Item
from app.utils import parse_datetime_human, fmt_dt_human

router = Router()

BOT_COMMANDS = [
    BotCommand(command="start", description="Запуск бота"),
    BotCommand(command="help", description="Справка по командам"),
    BotCommand(command="add", description="Добавить запись (мастер: USERID → USERNAME → DUE DATE/TIME)"),
    BotCommand(command="list", description="Список записей"),
    BotCommand(command="remove", description="Удалить запись: /remove <id>"),
    BotCommand(command="next", description="Ближайшие истечения"),
    BotCommand(command="status", description="Статус бота"),
    BotCommand(command="cancel", description="Отменить текущий ввод"),
]


async def set_bot_commands(bot: Bot) -> None:
    await bot.set_my_commands(commands=BOT_COMMANDS, scope=BotCommandScopeDefault())


@router.message(CommandStart())
async def on_start(message: Message) -> None:
    await message.answer(
        "✅ XMPLUS запущен.\n"
        "Открой меню команд (кнопка с точками) или напиши /help."
    )


@router.message(Command("help"))
async def on_help(message: Message) -> None:
    text = "Доступные команды:\n" + "\n".join([f"/{c.command} — {c.description}" for c in BOT_COMMANDS])
    await message.answer(text)


@router.message(Command("status"))
async def on_status(message: Message) -> None:
    async with SessionLocal() as session:
        total = (await session.execute(select(Item))).scalars().unique().all()
        await message.answer(f"Бот работает ✅\nВ базе записей: {len(total)}")


class AddStates(StatesGroup):
    waiting_user_id = State()
    waiting_username = State()
    waiting_duedatetime = State()


@router.message(Command("cancel"))
async def on_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.")


@router.message(Command("add"))
async def add_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(AddStates.waiting_user_id)
    await message.answer("Шаг 1/3. Введите USER ID (число):")


@router.message(AddStates.waiting_user_id)
async def add_user_id(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer("USER ID должен быть числом. Попробуйте ещё раз или /cancel.")
        return
    await state.update_data(user_id=int(text))
    await state.set_state(AddStates.waiting_username)
    await message.answer("Шаг 2/3. Введите USERNAME (например, XmADMIN):")


@router.message(AddStates.waiting_username)
async def add_username(message: Message, state: FSMContext) -> None:
    username = (message.text or "").strip()
    if not username:
        await message.answer("USERNAME не может быть пустым. Попробуйте ещё раз или /cancel.")
        return
    await state.update_data(username=username)
    await state.set_state(AddStates.waiting_duedatetime)
    await message.answer(
        "Шаг 3/3. Введите дату и время отключения (DUE DATE/TIME).\n"
        "Примеры форматов:\n"
        "• 2025-11-06 15:35:43\n"
        "• 31.01.2026 04:39\n"
        "• 2025-11-06\n"
        "• 31.01.2026"
    )


@router.message(AddStates.waiting_duedatetime)
async def add_duedatetime(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    dt = parse_datetime_human(text)
    if not dt:
        await message.answer(
            "Не смог распарсить дату/время.\n"
            "Примеры:\n"
            "• 2025-11-06 15:35:43\n"
            "• 31.01.2026 04:39\n"
            "• 2025-11-06\n"
            "• 31.01.2026\n"
            "Попробуйте ещё раз или /cancel."
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
        f"Добавлено: [{item.id}] USERID={user_id}, USERNAME={username}, DUE={fmt_dt_human(dt)}"
    )