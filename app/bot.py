from __future__ import annotations

from typing import Optional

from aiogram import Router, Bot
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, BotCommand, BotCommandScopeDefault

from app.db import SessionLocal, Item
from app.utils import parse_date_human, fmt_dt_human
from sqlalchemy import select, delete

router = Router()

# Команды для кнопки меню
BOT_COMMANDS = [
    BotCommand(command="start", description="Запуск бота"),
    BotCommand(command="help", description="Справка по командам"),
    BotCommand(command="add", description="Добавить запись: /add Название; 2025-12-31"),
    BotCommand(command="list", description="Список записей"),
    BotCommand(command="remove", description="Удалить запись: /remove <id>"),
    BotCommand(command="next", description="Ближайшие истечения"),
    BotCommand(command="status", description="Статус бота"),
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


def _parse_add_args(text: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """
    Ожидаем формат: /add Название; 2025-12-31
    Разделитель — точка с запятой или перенос строки.
    """
    if not text:
        return None, None
    # отрежем "/add"
    parts = text.split(maxsplit=1)
    args = parts[1] if len(parts) > 1 else ""
    if not args:
        return None, None
    # допускаем "title; date" или "title\ndate"
    if ";" in args:
        t, d = args.split(";", 1)
    elif "\n" in args:
        t, d = args.split("\n", 1)
    else:
        # Пытаемся последний «слово» считать датой
        tokens = args.rsplit(" ", 1)
        if len(tokens) == 2:
            t, d = tokens
        else:
            return None, None
    return t.strip(), d.strip()


@router.message(Command("add"))
async def on_add(message: Message) -> None:
    title, date_str = _parse_add_args(message.text)
    if not title or not date_str:
        await message.answer("Использование: /add Название; 2025-12-31\nФорматы дат: YYYY-MM-DD, DD.MM.YYYY, '31 Dec 2025'")
        return

    dt = parse_date_human(date_str)
    if not dt:
        await message.answer("Не смог распарсить дату. Поддерживаемые форматы: YYYY-MM-DD, DD.MM.YYYY, '31 Dec 2025'")
        return

    async with SessionLocal() as session:
        item = Item(
            title=title,
            expires_at=dt,
            chat_id=message.chat.id,
            notify_every_minutes=None,  # использовать глобальные настройки
            max_notifications=None,     # использовать глобальные настройки
        )
        session.add(item)
        await session.commit()
        await session.refresh(item)

    await message.answer(f"Добавлено: [{item.id}] {item.title} — истекает {fmt_dt_human(item.expires_at)}")


@router.message(Command("list"))
async def on_list(message: Message) -> None:
    async with SessionLocal() as session:
        result = await session.execute(select(Item).order_by(Item.expires_at.asc()))
        items = result.scalars().all()

    if not items:
        await message.answer("Список пуст.")
        return

    lines = [f"[{it.id}] {it.title} — {fmt_dt_human(it.expires_at)}" for it in items]
    await message.answer("Записи:\n" + "\n".join(lines))


@router.message(Command("remove"))
async def on_remove(message: Message) -> None:
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Использование: /remove <id>")
        return
    item_id = int(parts[1])

    async with SessionLocal() as session:
        await session.execute(delete(Item).where(Item.id == item_id))
        await session.commit()

    await message.answer(f"Удалено (если было): id={item_id}")


@router.message(Command("next"))
async def on_next(message: Message) -> None:
    async with SessionLocal() as session:
        result = await session.execute(select(Item).order_by(Item.expires_at.asc()).limit(10))
        items = result.scalars().all()

    if not items:
        await message.answer("Нет ближайших истечений.")
        return

    lines = [f"[{it.id}] {it.title} — {fmt_dt_human(it.expires_at)}" for it in items]
    await message.answer("Ближайшие:\n" + "\n".join(lines))