from __future__ import annotations

import logging
from datetime import datetime

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from sqlalchemy import select, delete, update

from app.db import SessionLocal, RouterItem
from app.config import settings
from app.states import RouterAddStates, RouterEditStates, RouterRenewStates, RouterDeleteStates
from app.keyboards import main_menu_kb
from app.utils import parse_datetime_human, fmt_dt_human, now_tz, to_tz, get_active_timezone_name
from app.bot import _trunc, split_text_chunks, send_pre_chunk
from app.handlers.renew import add_months

log = logging.getLogger(__name__)

router = Router()

# =====================================================================
#   📡 РОУТЕРЫ — раздел только для администратора
# =====================================================================

# ---- helpers ----

RT_NAME_W = 14
RT_NOTE_W = 10


def _rt_table_lines(items: list) -> tuple[str, list[str]]:
    header = f"{'КЛИЕНТ'.ljust(RT_NAME_W)} | {'ЗАМЕТКА'.ljust(RT_NOTE_W)} | DUE DATE"
    rows: list[str] = []
    for it in items:
        name = _trunc(it.client_name, RT_NAME_W).ljust(RT_NAME_W)
        note = _trunc(getattr(it, "note", "") or "", RT_NOTE_W).ljust(RT_NOTE_W)
        due = fmt_dt_human(it.due_date)
        rows.append(f"{name} | {note} | {due}")
    return header, rows


def _rt_card(it) -> str:
    note = getattr(it, "note", "") or ""
    note_line = f"Заметка: {note}\n" if note else ""
    return (
        f"Клиент: {it.client_name}\n"
        f"{note_line}"
        f"DUE: {fmt_dt_human(it.due_date)}"
    )


def _rt_edit_kb(item_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✏️ Клиент", callback_data=f"rte:f:{item_id}:client_name"),
            InlineKeyboardButton(text="✏️ Заметка", callback_data=f"rte:f:{item_id}:note"),
        ],
        [
            InlineKeyboardButton(text="✏️ Дата", callback_data=f"rte:f:{item_id}:due_date"),
        ],
        [InlineKeyboardButton(text="◀ Назад", callback_data="rt:menu")],
    ])


RT_FIELD_LABELS = {
    "client_name": "Имя клиента",
    "note": "Заметка",
    "due_date": "Дата отключения",
}


def router_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="➕ Добавить", callback_data="rt:add"),
            InlineKeyboardButton(text="📋 Список", callback_data="rt:list"),
        ],
        [
            InlineKeyboardButton(text="🔄 Продлить", callback_data="rt:renew"),
            InlineKeyboardButton(text="✏️ Изменить", callback_data="rt:edit"),
        ],
        [
            InlineKeyboardButton(text="⛔ Отключённые", callback_data="rt:disabled"),
            InlineKeyboardButton(text="🗑 Удалить", callback_data="rt:delete"),
        ],
        [InlineKeyboardButton(text="◀ Главное меню", callback_data="rt:back")],
    ])


# ---- Вход в раздел ----

@router.message(F.text.in_(["📡 Роутеры", "/routers"]))
async def rt_section(message: Message, state: FSMContext) -> None:
    await state.clear()
    async with SessionLocal() as session:
        count = len((await session.execute(select(RouterItem))).scalars().all())
    await message.answer(
        f"📡 Роутеры ({count} шт.)\nВыберите действие:",
        reply_markup=router_menu_kb(),
    )


@router.callback_query(F.data == "rt:menu")
async def rt_menu_cb(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await state.clear()
    async with SessionLocal() as session:
        count = len((await session.execute(select(RouterItem))).scalars().all())
    await cb.message.answer(
        f"📡 Роутеры ({count} шт.)\nВыберите действие:",
        reply_markup=router_menu_kb(),
    )


@router.callback_query(F.data == "rt:back")
async def rt_back(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await state.clear()
    await cb.message.answer("Главное меню.", reply_markup=main_menu_kb())


# ---- Добавить роутер ----

@router.callback_query(F.data == "rt:add")
async def rt_add_start(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await state.set_state(RouterAddStates.waiting_client_name)
    await cb.message.answer("📡 Добавить роутер\n\nШаг 1/3. Введите имя клиента:")


@router.message(RouterAddStates.waiting_client_name)
async def rt_add_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name:
        await message.answer("Имя не может быть пустым. Введите имя клиента:")
        return
    await state.update_data(rt_client_name=name)
    await state.set_state(RouterAddStates.waiting_due)
    tz = get_active_timezone_name()
    await message.answer(
        f"Шаг 2/3. Введите дату/время отключения ({tz}):\n"
        "Формат: YYYY-MM-DD HH:MM:SS или YYYY-MM-DD"
    )


@router.message(RouterAddStates.waiting_due)
async def rt_add_due(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    dt = parse_datetime_human(text)
    if dt is None:
        await message.answer("Не удалось разобрать дату. Попробуйте ещё раз (YYYY-MM-DD HH:MM:SS):")
        return
    await state.update_data(rt_due=dt.isoformat())
    await state.set_state(RouterAddStates.waiting_note)
    await message.answer("Шаг 3/3. Введите заметку (или отправьте «-» чтобы пропустить):")


@router.message(RouterAddStates.waiting_note)
async def rt_add_note(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    note = "" if text in ("-", "") else text
    data = await state.get_data()
    await state.clear()
    due = datetime.fromisoformat(data["rt_due"])
    client_name = data["rt_client_name"]
    async with SessionLocal() as session:
        item = RouterItem(client_name=client_name, due_date=due, note=note)
        session.add(item)
        await session.commit()
    await message.answer(
        f"✅ Роутер добавлен!\n\n"
        f"Клиент: {client_name}\n"
        f"DUE: {fmt_dt_human(due)}\n"
        + (f"Заметка: {note}" if note else ""),
    )


# ---- Список роутеров ----

@router.callback_query(F.data == "rt:list")
async def rt_list(cb: CallbackQuery) -> None:
    await cb.answer()
    async with SessionLocal() as session:
        items = (await session.execute(
            select(RouterItem).order_by(RouterItem.due_date.asc())
        )).scalars().all()
    if not items:
        await cb.message.answer("📡 Список роутеров пуст.", reply_markup=router_menu_kb())
        return
    header, rows = _rt_table_lines(items)
    chunks = split_text_chunks(f"📡 Роутеры ({len(items)}):\n{header}\n{'─' * len(header)}", rows)
    for ch in chunks:
        await send_pre_chunk(cb.message, ch)
    await cb.message.answer(f"Всего: {len(items)}", reply_markup=router_menu_kb())


# ---- Отключённые роутеры ----

@router.callback_query(F.data == "rt:disabled")
async def rt_disabled(cb: CallbackQuery) -> None:
    await cb.answer()
    now = now_tz()
    async with SessionLocal() as session:
        items = (await session.execute(
            select(RouterItem).order_by(RouterItem.due_date.asc())
        )).scalars().all()
    expired = [it for it in items if to_tz(it.due_date) <= now]
    if not expired:
        await cb.message.answer("📡 Отключённых роутеров нет.", reply_markup=router_menu_kb())
        return
    header, rows = _rt_table_lines(expired)
    chunks = split_text_chunks(
        f"⛔ Отключённые роутеры ({len(expired)}):\n{header}\n{'─' * len(header)}", rows
    )
    for ch in chunks:
        await send_pre_chunk(cb.message, ch)
    await cb.message.answer(f"Отключённых: {len(expired)}", reply_markup=router_menu_kb())


# ---- Продлить роутер ----

@router.callback_query(F.data == "rt:renew")
async def rt_renew_start(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await state.set_state(RouterRenewStates.waiting_search)
    await cb.message.answer("🔄 Продлить роутер\n\nВведите имя клиента для поиска:")


@router.message(RouterRenewStates.waiting_search)
async def rt_renew_search(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Введите имя клиента:")
        return
    async with SessionLocal() as session:
        from sqlalchemy import or_
        items = (await session.execute(
            select(RouterItem).where(or_(
                RouterItem.client_name.ilike(f"%{text}%"),
                RouterItem.note.ilike(f"%{text}%"),
            ))
        )).scalars().all()
    if not items:
        await message.answer("Ничего не найдено. Попробуйте ещё раз или /cancel.")
        return
    if len(items) == 1:
        it = items[0]
        await state.update_data(rt_renew_id=it.id)
        await state.set_state(RouterRenewStates.waiting_due)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Подставить текущую", callback_data=f"rt:rnpre:cur:{it.id}")],
            [InlineKeyboardButton(text="Подставить +1 месяц", callback_data=f"rt:rnpre:p1m:{it.id}")],
        ])
        tz = get_active_timezone_name()
        await message.answer(
            f"📋 {_rt_card(it)}\n\n"
            f"Текущая дата: {fmt_dt_human(it.due_date)}\n\n"
            f"Введите новую дату ({tz}):\n"
            "Формат: YYYY-MM-DD HH:MM:SS",
            reply_markup=kb,
        )
        return
    rows = []
    for it in items[:10]:
        label = f"{it.client_name}"
        if it.note:
            label += f" ({it.note})"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"rt:rnpick:{it.id}")])
    rows.append([InlineKeyboardButton(text="◀ Назад", callback_data="rt:menu")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await state.clear()
    await message.answer(f"Найдено {len(items)} записей. Выберите:", reply_markup=kb)


@router.callback_query(F.data.startswith("rt:rnpick:"))
async def rt_renew_pick(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    item_id = int(cb.data.split(":")[-1])
    async with SessionLocal() as session:
        it = (await session.execute(select(RouterItem).where(RouterItem.id == item_id))).scalars().first()
    if not it:
        await cb.message.answer("Запись не найдена.", reply_markup=router_menu_kb())
        return
    await state.set_state(RouterRenewStates.waiting_due)
    await state.update_data(rt_renew_id=it.id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Подставить текущую", callback_data=f"rt:rnpre:cur:{it.id}")],
        [InlineKeyboardButton(text="Подставить +1 месяц", callback_data=f"rt:rnpre:p1m:{it.id}")],
    ])
    tz = get_active_timezone_name()
    await cb.message.answer(
        f"📋 {_rt_card(it)}\n\n"
        f"Текущая дата: {fmt_dt_human(it.due_date)}\n\n"
        f"Введите новую дату ({tz}):\n"
        "Формат: YYYY-MM-DD HH:MM:SS",
        reply_markup=kb,
    )



@router.callback_query(F.data.startswith("rt:rnpre:"))
async def rt_renew_prefill(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    try:
        _, _, kind, item_id_str = cb.data.split(":")
        item_id = int(item_id_str)
    except Exception:
        return
    async with SessionLocal() as session:
        it = (await session.execute(select(RouterItem).where(RouterItem.id == item_id))).scalars().first()
    if not it:
        await cb.message.answer("Запись не найдена.")
        return
    base_dt = to_tz(it.due_date)
    if kind == "p1m":
        new_dt = add_months(base_dt, 1)
    else:
        new_dt = base_dt
    # Сразу применяем
    old_due = fmt_dt_human(it.due_date)
    async with SessionLocal() as session:
        item = (await session.execute(select(RouterItem).where(RouterItem.id == item_id))).scalars().first()
        if not item:
            await cb.message.answer("Запись не найдена.")
            return
        item.due_date = new_dt
        item.notified_count = 0
        item.last_notified_at = None
        await session.commit()
    await state.clear()
    await cb.message.answer(
        f"✅ Роутер продлён!\n\n"
        f"Клиент: {it.client_name}\n"
        f"Было: {old_due}\n"
        f"Стало: {fmt_dt_human(new_dt)}",
    )


@router.message(RouterRenewStates.waiting_due)
async def rt_renew_due(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    dt = parse_datetime_human(text)
    if dt is None:
        await message.answer("Не удалось разобрать дату. Попробуйте ещё раз:")
        return
    data = await state.get_data()
    item_id = data["rt_renew_id"]
    await state.clear()
    async with SessionLocal() as session:
        it = (await session.execute(select(RouterItem).where(RouterItem.id == item_id))).scalars().first()
        if not it:
            await message.answer("Запись не найдена.")
            return
        old_due = fmt_dt_human(it.due_date)
        it.due_date = dt
        it.notified_count = 0
        it.last_notified_at = None
        await session.commit()
    await message.answer(
        f"✅ Роутер продлён!\n\n"
        f"Клиент: {it.client_name}\n"
        f"Было: {old_due}\n"
        f"Стало: {fmt_dt_human(dt)}",
    )


# ---- Удалить роутер ----

@router.callback_query(F.data == "rt:delete")
async def rt_delete_start(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await state.set_state(RouterDeleteStates.waiting_search)
    await cb.message.answer("🗑 Удалить роутер\n\nВведите имя клиента для поиска:")


@router.message(RouterDeleteStates.waiting_search)
async def rt_delete_search(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Введите имя клиента:")
        return
    async with SessionLocal() as session:
        from sqlalchemy import or_
        items = (await session.execute(
            select(RouterItem).where(or_(
                RouterItem.client_name.ilike(f"%{text}%"),
                RouterItem.note.ilike(f"%{text}%"),
            ))
        )).scalars().all()
    if not items:
        await message.answer("Ничего не найдено. Попробуйте ещё раз или /cancel.")
        return
    await state.clear()
    rows = []
    for it in items[:10]:
        label = f"{it.client_name}"
        if it.note:
            label += f" ({it.note})"
        label += f" | {fmt_dt_human(it.due_date)}"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"rt:delpick:{it.id}")])
    rows.append([InlineKeyboardButton(text="◀ Назад", callback_data="rt:menu")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await message.answer(f"Найдено {len(items)}. Выберите для удаления:", reply_markup=kb)


@router.callback_query(F.data.startswith("rt:delpick:"))
async def rt_delete_confirm(cb: CallbackQuery) -> None:
    await cb.answer()
    item_id = int(cb.data.split(":")[-1])
    async with SessionLocal() as session:
        it = (await session.execute(select(RouterItem).where(RouterItem.id == item_id))).scalars().first()
    if not it:
        await cb.message.answer("Запись не найдена.", reply_markup=router_menu_kb())
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Удалить", callback_data=f"rt:delok:{item_id}"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="rt:menu"),
        ]
    ])
    await cb.message.answer(f"Удалить роутер?\n\n{_rt_card(it)}", reply_markup=kb)


@router.callback_query(F.data.startswith("rt:delok:"))
async def rt_delete_exec(cb: CallbackQuery) -> None:
    await cb.answer()
    item_id = int(cb.data.split(":")[-1])
    async with SessionLocal() as session:
        it = (await session.execute(select(RouterItem).where(RouterItem.id == item_id))).scalars().first()
        if not it:
            await cb.message.answer("Запись уже удалена.", reply_markup=router_menu_kb())
            return
        name = it.client_name
        await session.delete(it)
        await session.commit()
    await cb.message.answer(f"🗑 Роутер «{name}» удалён.", reply_markup=router_menu_kb())


# ---- Изменить роутер ----

@router.callback_query(F.data == "rt:edit")
async def rt_edit_start(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await state.set_state(RouterEditStates.waiting_search)
    await cb.message.answer("✏️ Редактор роутеров\n\nВведите имя клиента для поиска:")


@router.message(RouterEditStates.waiting_search)
async def rt_edit_search(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Введите имя клиента:")
        return
    async with SessionLocal() as session:
        from sqlalchemy import or_
        items = (await session.execute(
            select(RouterItem).where(or_(
                RouterItem.client_name.ilike(f"%{text}%"),
                RouterItem.note.ilike(f"%{text}%"),
            ))
        )).scalars().all()
    if not items:
        await message.answer("Ничего не найдено. Попробуйте ещё раз или /cancel.")
        return
    if len(items) == 1:
        it = items[0]
        await state.clear()
        await message.answer(f"📋 {_rt_card(it)}", reply_markup=_rt_edit_kb(it.id))
        return
    rows = []
    for it in items[:10]:
        label = f"{it.client_name}"
        if it.note:
            label += f" ({it.note})"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"rte:pick:{it.id}")])
    rows.append([InlineKeyboardButton(text="◀ Назад", callback_data="rt:menu")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await state.clear()
    await message.answer(f"Найдено {len(items)} записей. Выберите:", reply_markup=kb)


@router.callback_query(F.data.startswith("rte:pick:"))
async def rt_edit_pick(cb: CallbackQuery) -> None:
    await cb.answer()
    item_id = int(cb.data.split(":")[-1])
    async with SessionLocal() as session:
        it = (await session.execute(select(RouterItem).where(RouterItem.id == item_id))).scalars().first()
    if not it:
        await cb.message.answer("Запись не найдена.", reply_markup=router_menu_kb())
        return
    await cb.message.answer(f"📋 {_rt_card(it)}", reply_markup=_rt_edit_kb(it.id))


@router.callback_query(F.data.startswith("rte:f:"))
async def rt_edit_field_start(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    parts = cb.data.split(":")
    item_id = int(parts[2])
    field = parts[3]
    label = RT_FIELD_LABELS.get(field, field)
    await state.set_state(RouterEditStates.waiting_value)
    await state.update_data(rte_item_id=item_id, rte_field=field)
    if field == "due_date":
        await cb.message.answer("Введите новую дату (YYYY-MM-DD HH:MM:SS):")
    else:
        await cb.message.answer(f"Введите новое значение для «{label}»:")


@router.message(RouterEditStates.waiting_value)
async def rt_edit_field_save(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Значение не может быть пустым.")
        return
    data = await state.get_data()
    item_id = data["rte_item_id"]
    field = data["rte_field"]
    await state.clear()
    async with SessionLocal() as session:
        it = (await session.execute(select(RouterItem).where(RouterItem.id == item_id))).scalars().first()
        if not it:
            await message.answer("Запись не найдена.")
            return
        if field == "due_date":
            dt = parse_datetime_human(text)
            if dt is None:
                await message.answer("Не удалось разобрать дату. Попробуйте ещё раз через ✒️ Изменить.")
                return
            it.due_date = dt
            it.notified_count = 0
            it.last_notified_at = None
        elif field == "client_name":
            it.client_name = text
        elif field == "note":
            it.note = text
        await session.commit()
    label = RT_FIELD_LABELS.get(field, field)
    await message.answer(
        f"\u2705 \u041e\u0431\u043d\u043e\u0432\u043b\u0435\u043d\u043e: {label}\n\n{_rt_card(it)}",
        reply_markup=_rt_edit_kb(it.id),
    )
