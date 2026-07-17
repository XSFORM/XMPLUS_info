from __future__ import annotations

import logging, html, calendar
from datetime import datetime, timezone, timedelta

from aiogram import Router, Bot, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from sqlalchemy import select, delete

from app.db import SessionLocal, Item, Dealer, get_price, apply_balance_change, MAIN_CODE, get_dealer
from app.config import settings
from app.states import RenewStates, DeleteStates
from app.keyboards import confirm_kb, choose_by_due_kb, main_menu_kb
from app.utils import parse_datetime_human, fmt_dt_human, now_tz, to_tz, tz_offset_str
from app.bot import _notify_fail


log = logging.getLogger(__name__)

router = Router()

# ==== Продление (/renew) — только админ ====

def add_months(dt: datetime, months: int = 1) -> datetime:
    y = dt.year + (dt.month - 1 + months) // 12
    m = (dt.month - 1 + months) % 12 + 1
    last_day = calendar.monthrange(y, m)[1]
    d = min(dt.day, last_day)
    return dt.replace(year=y, month=m, day=d)


@router.message(Command("renew"))
@router.message(F.text.in_(["/renew", "🔄 Продлить"]))
async def renew_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(RenewStates.waiting_userid)
    await message.answer("Укажи USERID клиента, которого нужно продлить:")

@router.message(RenewStates.waiting_userid)
async def renew_find_by_userid(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer("USERID должен быть числом. Введите ещё раз или /cancel.")
        return
    uid = int(text)
    async with SessionLocal() as session:
        result = await session.execute(select(Item).where(Item.user_id == uid).order_by(Item.due_date.asc()))
        items = result.scalars().all()
    if not items:
        await message.answer("Записей с таким USERID не найдено. Проверьте число или /cancel.")
        return
    if len(items) == 1:
        it = items[0]
        await state.update_data(item_id=it.id, user_id=it.user_id, username=it.username, old_due=fmt_dt_human(it.due_date))
        await state.set_state(RenewStates.waiting_new_due)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Подставить текущую", callback_data=f"renew:prefill:current:{it.id}")],
            [InlineKeyboardButton(text="Подставить +1 месяц", callback_data=f"renew:prefill:plus1m:{it.id}")],
        ])
        await message.answer(
            "Клиент:\n"
            f"USERID: {it.user_id}\n"
            f"USERNAME: {it.username}\n"
            f"Текущая дата отключения: {fmt_dt_human(it.due_date)}",
            reply_markup=kb,
        )
        await message.answer("Отправьте новую дату в формате:\nYYYY-MM-DD HH:MM:SS")
        return
    kb = choose_by_due_kb("renew", items)
    await message.answer("Найдено несколько записей по этому USERID. Выберите запись по дате:", reply_markup=kb)

@router.callback_query(F.data.startswith("renew:choose:"))
async def renew_choose_item(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    try:
        item_id = int(cb.data.split(":")[-1])
    except Exception:
        return
    async with SessionLocal() as session:
        it = await session.get(Item, item_id)
    if not it:
        await cb.message.answer("Запись не найдена. Попробуйте снова /renew.")
        return
    await state.update_data(item_id=it.id, user_id=it.user_id, username=it.username, old_due=fmt_dt_human(it.due_date))
    await state.set_state(RenewStates.waiting_new_due)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Подставить текущую", callback_data=f"renew:prefill:current:{it.id}")],
        [InlineKeyboardButton(text="Подставить +1 месяц", callback_data=f"renew:prefill:plus1m:{it.id}")],
    ])
    await cb.message.answer(
        "Клиент:\n"
        f"USERID: {it.user_id}\n"
        f"USERNAME: {it.username}\n"
        f"Текущая дата отключения: {fmt_dt_human(it.due_date)}",
        reply_markup=kb,
    )
    await cb.message.answer("Отправьте новую дату в формате:\nYYYY-MM-DD HH:MM:SS")

@router.callback_query(F.data.startswith("renew:prefill:"))
async def renew_prefill(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    try:
        _, _, kind, item_id_str = cb.data.split(":")
        item_id = int(item_id_str)
    except Exception:
        await cb.message.answer("Ошибка выбора записи. Повторите /renew.")
        return

    async with SessionLocal() as session:
        it = await session.get(Item, item_id)
    if not it:
        await cb.message.answer("Запись не найдена. Повторите /renew.")
        return

    base_dt = to_tz(it.due_date)
    if kind == "plus1m":
        new_dt = add_months(base_dt, 1)
    else:
        new_dt = base_dt

    # Заполняем состояние и сразу показываем подтверждение
    await state.update_data(item_id=it.id, user_id=it.user_id, username=it.username,
                            old_due=fmt_dt_human(base_dt), new_due=fmt_dt_human(new_dt))
    await state.set_state(RenewStates.waiting_confirm)
    await cb.message.answer(
        "Подтвердите продление:\n"
        f"USERID: {it.user_id}\n"
        f"USERNAME: {it.username}\n"
        f"Было: {fmt_dt_human(base_dt)}\n"
        f"Станет: {fmt_dt_human(new_dt)}",
        reply_markup=confirm_kb("cfr"),
    )


@router.message(RenewStates.waiting_new_due)
async def renew_get_new_due(message: Message, state: FSMContext) -> None:
    s = (message.text or "").strip()
    dt = parse_datetime_human(s)
    if not dt:
        await message.answer("Неверный формат даты. Используйте YYYY-MM-DD HH:MM:SS.\nПопробуйте ещё раз или /cancel.")
        return
    new_due = fmt_dt_human(dt)
    data = await state.get_data()
    await state.update_data(new_due=new_due)
    await state.set_state(RenewStates.waiting_confirm)
    await message.answer(
        "Подтвердите продление:\n"
        f"USERID: {data.get('user_id')}\n"
        f"USERNAME: {data.get('username')}\n"
        f"Было: {data.get('old_due')}\n"
        f"Станет: {new_due}",
        reply_markup=confirm_kb("cfr"),
    )

@router.callback_query(F.data == "cfr:edit")
async def renew_confirm_edit(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    data = await state.get_data()
    suggested = data.get("new_due")
    await state.set_state(RenewStates.waiting_new_due)
    hint = f"\nПодсказка: {suggested}" if suggested else ""
    await cb.message.answer(
        f"Отправьте новую дату в формате:\nYYYY-MM-DD HH:MM:SS{hint}",
    )


@router.callback_query(F.data == "cfr:cancel")
async def renew_confirm_cancel(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await state.clear()
    await cb.message.answer("Отменено.")


@router.callback_query(F.data == "cfr:ok", RenewStates.waiting_confirm)
async def renew_confirm(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await cb.answer()
    data = await state.get_data()
    item_id = int(data["item_id"])
    new_due_str = data["new_due"]
    dealer_code = None
    item_user_id = None
    item_username = None
    async with SessionLocal() as session:
        item = await session.get(Item, item_id)
        if not item:
            await state.clear()
            await cb.message.answer("Запись не найдена.")
            return
        dt = parse_datetime_human(new_due_str)
        if not dt:
            await state.clear()
            await cb.message.answer("Ошибка при парсинге даты. Операция отменена.")
            return
        item.due_date = dt
        item.notified_count = 0
        item.last_notified_at = None
        dealer_code = item.dealer
        item_user_id = item.user_id
        item_username = item.username
        await session.commit()
    await state.clear()
    await cb.message.answer(
        f"✅ Продлено: USERID={data['user_id']}, USERNAME={data['username']}\nНовая дата DUE={new_due_str}",
    )
    # Начислить долг дилеру за продление и уведомить его
    if dealer_code and dealer_code != MAIN_CODE:
        price = await get_price()
        new_balance = await apply_balance_change(
            dealer_code, price, "renewal", f"Продление: USERID={item_user_id}"
        )
        d = await get_dealer(dealer_code)
        if d and d.chat_id is not None:
            charge_line = ""
            if new_balance is not None:
                charge_line = f"\n\nНачислено: ${price:g}\nВаш долг: ${new_balance:g}"
            try:
                await bot.send_message(
                    d.chat_id,
                    "🔄 Клиент продлён\n\n"
                    f"USERID: {item_user_id}\n"
                    f"USERNAME: {item_username}\n"
                    f"Новая дата отключения: {new_due_str}"
                    + charge_line,
                )
            except Exception as e:
                await _notify_fail(bot, f"дилер {d.title}", e)

# ==== Удаление — только админ ====

@router.message(Command("delete"))
@router.message(F.text.in_(["/delete", "🗑 Удалить"]))
async def delete_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(DeleteStates.waiting_userid)
    await message.answer(
        "Укажи USERID клиента, которого нужно удалить.\n"
        "Если по USERID несколько записей — предложу выбрать по дате или удалить все сразу.",
    )

@router.message(DeleteStates.waiting_userid)
async def delete_by_userid(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer("USERID должен быть числом. Введите ещё раз или /cancel.")
        return
    uid = int(text)
    async with SessionLocal() as session:
        result = await session.execute(select(Item).where(Item.user_id == uid).order_by(Item.due_date.asc()))
        items = result.scalars().all()
    if not items:
        await message.answer("По этому USERID записей нет. Проверьте число или /cancel.")
        return
    if len(items) == 1:
        it = items[0]
        preview = f"USERID={it.user_id}, USERNAME={it.username}, DUE={fmt_dt_human(it.due_date)}"
        await state.update_data(action="one", item_id=it.id, user_id=it.user_id)
        await state.set_state(DeleteStates.waiting_confirm)
        await message.answer("Удалить запись?\n" + preview, reply_markup=confirm_kb("cfd", show_edit=False))
        return
    extra = [InlineKeyboardButton(text="🗑 Удалить все записи этого USERID", callback_data=f"delete:all:{uid}")]
    kb = choose_by_due_kb("delete", items, extra_row=extra)
    await message.answer("Найдено несколько записей. Выберите запись по дате или удалите все:", reply_markup=kb)

@router.callback_query(F.data.startswith("delete:choose:"))
async def delete_choose_one(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    try:
        item_id = int(cb.data.split(":")[-1])
    except Exception:
        return
    async with SessionLocal() as session:
        it = await session.get(Item, item_id)
    if not it:
        await cb.message.answer("Запись не найдена. Попробуйте снова /delete.")
        return
    preview = f"USERID={it.user_id}, USERNAME={it.username}, DUE={fmt_dt_human(it.due_date)}"
    await state.update_data(action="one", item_id=it.id, user_id=it.user_id)
    await state.set_state(DeleteStates.waiting_confirm)
    await cb.message.answer("Удалить запись?\n" + preview, reply_markup=confirm_kb("cfd", show_edit=False))

@router.callback_query(F.data.startswith("delete:all:"))
async def delete_choose_all(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    try:
        uid = int(cb.data.split(":")[-1])
    except Exception:
        return
    await state.update_data(action="all", user_id=uid)
    await state.set_state(DeleteStates.waiting_confirm)
    await cb.message.answer(f"Удалить ВСЕ записи для USERID={uid}?", reply_markup=confirm_kb("cfd", show_edit=False))

@router.callback_query(F.data == "cfd:cancel")
async def delete_confirm_cancel(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await state.clear()
    await cb.message.answer("Отменено.")


@router.callback_query(F.data == "cfd:ok", DeleteStates.waiting_confirm)
async def delete_confirm(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    data = await state.get_data()
    async with SessionLocal() as session:
        if data.get("action") == "one":
            await session.execute(delete(Item).where(Item.id == int(data["item_id"])))
            await session.commit()
            msg = f"🗑️ Удалено: запись USERID={data['user_id']}"
        else:
            await session.execute(delete(Item).where(Item.user_id == int(data["user_id"])))
            await session.commit()
            msg = f"🗑️ Удалены все записи для USERID={data['user_id']}"
    await state.clear()
    await cb.message.answer(msg)
