from __future__ import annotations

import logging
from datetime import datetime

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from sqlalchemy import select

from app.db import SessionLocal, Item
from app.config import settings
from app.states import AddStates
from app.keyboards import main_menu_kb
from app.utils import parse_datetime_human, fmt_dt_human, now_tz, to_tz

log = logging.getLogger(__name__)

router = Router()

# ==== Добавление (только админ) ====

@router.message(Command("add"))
@router.message(F.text.in_(["/add", "➕ Добавить"]))
async def add_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(AddStates.waiting_user_id)
    await message.answer("Шаг 1/4. Введите USER ID (число):")

@router.message(AddStates.waiting_user_id)
async def add_user_id(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer("USER ID должен быть числом. Попробуйте ещё раз или /cancel.")
        return
    await state.update_data(user_id=int(text))
    await state.set_state(AddStates.waiting_username)
    await message.answer("Шаг 2/4. Введите USERNAME (например, XmADMIN):")

@router.message(AddStates.waiting_username)
async def add_username(message: Message, state: FSMContext) -> None:
    username = (message.text or "").strip()
    if not username:
        await message.answer("USERNAME не может быть пустым. Попробуйте ещё раз или /cancel.")
        return
    await state.update_data(username=username)
    await state.set_state(AddStates.waiting_duedatetime)
    await message.answer(
        "Шаг 3/4. Введите дату и время отключения строго в формате:\n"
        "YYYY-MM-DD HH:MM:SS\n"
        "Пример: 2025-10-20 15:35:43",
    )

@router.message(AddStates.waiting_duedatetime)
async def add_duedatetime(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    dt = parse_datetime_human(text)
    if not dt:
        await message.answer(
            "Неверный формат. Используйте только YYYY-MM-DD HH:MM:SS, например: 2025-10-20 15:35:43\n"
            "Попробуйте ещё раз или /cancel.",
        )
        return
    await state.update_data(due_date=dt.isoformat())
    await state.set_state(AddStates.waiting_note)
    await message.answer(
        "Шаг 4/4. Введите заметку (имя клиента) или нажмите «Пропустить»:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⏭ Пропустить", callback_data="add:skip_note")],
        ]),
    )

@router.callback_query(F.data == "add:skip_note")
async def add_skip_note(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await _save_new_item(cb.message, state, note="")

@router.message(AddStates.waiting_note)
async def add_note(message: Message, state: FSMContext) -> None:
    note = (message.text or "").strip()
    await _save_new_item(message, state, note=note)

async def _save_new_item(message: Message, state: FSMContext, note: str) -> None:
    data = await state.get_data()
    user_id = data["user_id"]
    username = data["username"]
    dt = datetime.fromisoformat(data["due_date"])
    async with SessionLocal() as session:
        item = Item(user_id=user_id, username=username, due_date=dt, note=note, chat_id=message.chat.id)
        session.add(item)
        await session.commit()
        await session.refresh(item)
    await state.clear()
    note_str = f", Заметка: {note}" if note else ""
    await message.answer(
        f"Добавлено: [{item.id}] USERID={user_id}, USERNAME={username}, DUE={fmt_dt_human(dt)}{note_str}",
    )

