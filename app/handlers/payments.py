from __future__ import annotations

import logging, html
from datetime import datetime

from aiogram import Router, Bot, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from sqlalchemy import select, delete, update

from app.db import (
    SessionLocal, Item, Dealer, BalanceTxn,
    PaymentMethod, PaymentVariant, Payment,
    get_price, set_price, apply_balance_change,
)
from app.config import settings
from app.states import BalanceStates, PayAdminStates, AdminKeyToDealerStates
from app.keyboards import main_menu_kb
from app.utils import fmt_dt_human, now_tz, to_tz
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest
from app.bot import _notify_fail
from app.handlers.dealers import list_dealers, get_dealer, dealers_menu_kb

log = logging.getLogger(__name__)

router = Router()

# ====== Баланс и долги дилеров (админ) ======

def balance_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить долг", callback_data="bal:add:start"),
         InlineKeyboardButton(text="➖ Снять долг", callback_data="bal:sub:start")],
        [InlineKeyboardButton(text="💲 Цена за продление", callback_data="bal:price:start")],
    ])


async def balance_overview_text() -> str:
    price = await get_price()
    dealers = await list_dealers()
    lines = [f"💲 Цена за продление: ${price:g}", "", "💰 Балансы дилеров (долг):"]
    if dealers:
        for d in dealers:
            lines.append(f"- {d.title}: ${(d.balance or 0.0):g}")
    else:
        lines.append("- дилеров нет")
    lines.append("")
    lines.append("Выберите действие:")
    return "\n".join(lines)


async def _balance_pick_dealer_kb(direction: str) -> InlineKeyboardMarkup:
    rows = []
    for d in await list_dealers():
        rows.append([InlineKeyboardButton(
            text=f"{d.title} (${(d.balance or 0.0):g})",
            callback_data=f"bal:pick:{direction}:{d.code}",
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _parse_amount(text: str) -> float | None:
    s = (text or "").strip().replace(",", ".").lstrip("+")
    try:
        v = float(s)
    except Exception:
        return None
    if v <= 0:
        return None
    return v


@router.message(Command("balance"))
@router.message(F.text.in_(["/balance", "💰 Баланс"]))
async def on_balance(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(await balance_overview_text(), reply_markup=balance_menu_kb())


@router.callback_query(F.data == "bal:add:start")
async def bal_add_start(cb: CallbackQuery) -> None:
    await cb.answer()
    if not await list_dealers():
        await cb.message.answer("Список дилеров пуст.", reply_markup=balance_menu_kb())
        return
    await cb.message.answer("Кому добавить долг?", reply_markup=await _balance_pick_dealer_kb("add"))


@router.callback_query(F.data == "bal:sub:start")
async def bal_sub_start(cb: CallbackQuery) -> None:
    await cb.answer()
    if not await list_dealers():
        await cb.message.answer("Список дилеров пуст.", reply_markup=balance_menu_kb())
        return
    await cb.message.answer("У кого снять долг?", reply_markup=await _balance_pick_dealer_kb("sub"))


@router.callback_query(F.data.startswith("bal:pick:"))
async def bal_pick(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    parts = cb.data.split(":")
    if len(parts) != 4:
        return
    direction, code = parts[2], parts[3]
    d = await get_dealer(code)
    if not d:
        await cb.message.answer("Дилер не найден.", reply_markup=balance_menu_kb())
        return
    await state.clear()
    await state.update_data(bal_dir=direction, bal_code=code)
    await state.set_state(BalanceStates.waiting_amount)
    word = "добавить" if direction == "add" else "снять"
    await cb.message.answer(
        f"Дилер: {d.title} (текущий долг ${(d.balance or 0.0):g})\n"
        f"Введите сумму в $, которую нужно {word}. Отмена — /cancel",
    )


@router.message(BalanceStates.waiting_amount)
async def bal_amount(message: Message, state: FSMContext) -> None:
    amount = _parse_amount(message.text or "")
    if amount is None:
        await message.answer("Введите положительное число (например 5). Ещё раз или /cancel.")
        return
    await state.update_data(bal_amount=amount)
    await state.set_state(BalanceStates.waiting_comment)
    await message.answer(
        "Введите комментарий к операции (например «корректировка») или «-», чтобы пропустить.",
    )


@router.message(BalanceStates.waiting_comment)
async def bal_comment(message: Message, state: FSMContext, bot: Bot) -> None:
    raw = (message.text or "").strip()
    comment = "" if raw in ("-", "—") else raw
    data = await state.get_data()
    await state.clear()
    direction = data.get("bal_dir")
    code = data.get("bal_code")
    amount = data.get("bal_amount")
    if not code or amount is None or direction not in ("add", "sub"):
        await message.answer("Ввод сброшен. Начните заново — /balance.")
        return
    d = await get_dealer(code)
    if not d:
        await message.answer("Дилер не найден.")
        return
    signed = amount if direction == "add" else -amount
    kind = "admin_add" if direction == "add" else "admin_sub"
    new_balance = await apply_balance_change(code, signed, kind, comment)
    if new_balance is None:
        await message.answer("Не удалось изменить баланс.")
        return
    word = "начислил" if direction == "add" else "списал"
    if d.chat_id is not None:
        dealer_text = (
            f"{'➕' if direction == 'add' else '➖'} Администратор {word} "
            f"{'вам' if direction == 'add' else 'с вашего долга'} ${amount:g}.\n"
        )
        if comment:
            dealer_text += f"Комментарий: {comment}\n"
        dealer_text += f"Ваш долг: ${new_balance:g}"
        try:
            await bot.send_message(d.chat_id, dealer_text)
        except Exception as e:
            await _notify_fail(bot, f"дилер {d.title}", e)
    await message.answer(
        f"✅ Готово. Дилеру «{d.title}» {word} ${amount:g}.\nНовый долг: ${new_balance:g}",
    )
    await message.answer(await balance_overview_text(), reply_markup=balance_menu_kb())


@router.callback_query(F.data == "bal:price:start")
async def bal_price_start(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    price = await get_price()
    await state.clear()
    await state.set_state(BalanceStates.waiting_price)
    await cb.message.answer(
        f"Текущая цена за продление: ${price:g}\n"
        "Введите новую цену в $ (например 5). Отмена — /cancel",
    )


@router.message(BalanceStates.waiting_price)
async def bal_price_set(message: Message, state: FSMContext) -> None:
    amount = _parse_amount(message.text or "")
    if amount is None:
        await message.answer("Введите положительное число (например 5). Ещё раз или /cancel.")
        return
    await state.clear()
    await set_price(amount)
    await message.answer(f"✅ Цена за продление установлена: ${amount:g}")
    await message.answer(await balance_overview_text(), reply_markup=balance_menu_kb())


# ====== Методы оплаты и подтверждение оплат (админ) ======

async def list_payment_methods(active_only: bool = False) -> list[PaymentMethod]:
    async with SessionLocal() as session:
        q = select(PaymentMethod).order_by(PaymentMethod.id.asc())
        if active_only:
            q = q.where(PaymentMethod.active.is_(True))
        return (await session.execute(q)).scalars().all()


async def get_payment_method(pm_id: int) -> PaymentMethod | None:
    async with SessionLocal() as session:
        return await session.get(PaymentMethod, pm_id)


async def pay_admin_text() -> str:
    methods = await list_payment_methods()
    lines = ["💳 Методы оплаты:"]
    if methods:
        for m in methods:
            st = "вкл" if m.active else "выкл"
            variants = await list_payment_variants(m.id)
            v_active = sum(1 for v in variants if v.active)
            lines.append(f"- {m.name} ({st}, видов: {len(variants)}, активных: {v_active})")
    else:
        lines.append("- методов нет")
    lines.append("")
    lines.append("Выберите метод или добавьте новый:")
    return "\n".join(lines)


async def pay_admin_kb() -> InlineKeyboardMarkup:
    rows = []
    for m in await list_payment_methods():
        mark = "" if m.active else " (выкл)"
        rows.append([InlineKeyboardButton(text=f"{m.name}{mark}", callback_data=f"pm:open:{m.id}")])
    rows.append([InlineKeyboardButton(text="➕ Добавить метод", callback_data="pm:add:start")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("pay"))
@router.message(F.text.in_(["/pay", "💳 Оплата"]))
async def on_pay(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(await pay_admin_text(), reply_markup=await pay_admin_kb())


@router.callback_query(F.data == "pm:home")
async def pm_home(cb: CallbackQuery) -> None:
    await cb.answer()
    await cb.message.answer(await pay_admin_text(), reply_markup=await pay_admin_kb())


@router.callback_query(F.data.startswith("pm:open:"))
async def pm_open(cb: CallbackQuery) -> None:
    await cb.answer()
    try:
        pm_id = int(cb.data.split(":")[-1])
    except Exception:
        return
    m = await get_payment_method(pm_id)
    if not m:
        await cb.message.answer("Метод не найден.", reply_markup=await pay_admin_kb())
        return
    text, kb = await _method_card(m)
    await cb.message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("pm:toggle:"))
async def pm_toggle(cb: CallbackQuery) -> None:
    await cb.answer()
    try:
        pm_id = int(cb.data.split(":")[-1])
    except Exception:
        return
    async with SessionLocal() as session:
        m = await session.get(PaymentMethod, pm_id)
        if not m:
            await cb.message.answer("Метод не найден.", reply_markup=await pay_admin_kb())
            return
        m.active = not m.active
        new_state = m.active
        name = m.name
        await session.commit()
    await cb.message.answer(
        f"Метод «{name}»: {'включён' if new_state else 'выключен'}.",
        reply_markup=await pay_admin_kb(),
    )


@router.callback_query(F.data.startswith("pm:req:"))
async def pm_req_start(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    try:
        pm_id = int(cb.data.split(":")[-1])
    except Exception:
        return
    m = await get_payment_method(pm_id)
    if not m:
        await cb.message.answer("Метод не найден.", reply_markup=await pay_admin_kb())
        return
    await state.clear()
    await state.update_data(pm_id=pm_id)
    await state.set_state(PayAdminStates.waiting_requisites)
    cur = (m.requisites or "").strip() or "(не заданы)"
    await cb.message.answer(
        f"Метод «{m.name}». Введите реквизиты (номер карты/кошелька, инструкции).\n"
        f"Текущие реквизиты:\n{cur}\n\n"
        "Отмена — /cancel",
    )


@router.message(PayAdminStates.waiting_requisites)
async def pm_req_save(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Реквизиты не могут быть пустыми. Ещё раз или /cancel.")
        return
    data = await state.get_data()
    pm_id = data.get("pm_id")
    await state.clear()
    if not pm_id:
        await message.answer("Ввод сброшен. Начните заново — /pay.")
        return
    async with SessionLocal() as session:
        m = await session.get(PaymentMethod, int(pm_id))
        if not m:
            await message.answer("Метод не найден.")
            return
        m.requisites = text[:1024]
        name = m.name
        await session.commit()
    await message.answer(f"✅ Реквизиты метода «{name}» обновлены.")
    await message.answer(await pay_admin_text(), reply_markup=await pay_admin_kb())


@router.callback_query(F.data == "pm:add:start")
async def pm_add_start(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await state.clear()
    await state.set_state(PayAdminStates.waiting_method_name)
    await cb.message.answer(
        "Введите название нового метода оплаты (например: Каспи).\nОтмена — /cancel",
    )


@router.message(PayAdminStates.waiting_method_name)
async def pm_add_save(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name or len(name) > 64:
        await message.answer("Название не должно быть пустым или длиннее 64 символов. Ещё раз или /cancel.")
        return
    await state.clear()
    async with SessionLocal() as session:
        existing = (await session.execute(
            select(PaymentMethod).where(PaymentMethod.name == name)
        )).scalars().first()
        if existing:
            await message.answer(f"Метод «{name}» уже существует.")
            return
        session.add(PaymentMethod(name=name, requisites="", active=True))
        await session.commit()
    await message.answer(
        f"✅ Метод «{name}» добавлен. Не забудьте задать ему реквизиты.",
    )
    await message.answer(await pay_admin_text(), reply_markup=await pay_admin_kb())


@router.callback_query(F.data.startswith("pay:ok:"))
async def pay_confirm(cb: CallbackQuery, bot: Bot) -> None:
    await cb.answer()
    try:
        pay_id = int(cb.data.split(":")[-1])
    except Exception:
        return
    async with SessionLocal() as session:
        pay = await session.get(Payment, pay_id)
        if not pay:
            await cb.message.answer("Заявка на оплату не найдена.")
            return
        if pay.status != "pending":
            await cb.message.answer(
                f"Заявка уже обработана (статус: {pay.status}).",
            )
            return
        pay.status = "confirmed"
        dealer_code = pay.dealer_code
        method = pay.method
        variant = pay.variant
        amount = pay.amount
        await session.commit()
    method_full = f"{method} → {variant}" if variant else method
    new_balance = await apply_balance_change(dealer_code, -amount, "payment", f"Оплата: {method_full}")
    d = await get_dealer(dealer_code)
    if d and d.chat_id is not None:
        bal_txt = f"\nВаш долг: ${new_balance:g}" if new_balance is not None else ""
        try:
            await bot.send_message(
                d.chat_id,
                f"✅ Оплата подтверждена.\nМетод: {method_full}\nСумма: ${amount:g}{bal_txt}",
            )
        except Exception as e:

            await _notify_fail(bot, f"дилер {dealer_code}", e)

    bal_show = f"${new_balance:g}" if new_balance is not None else "?"
    await cb.message.answer(
        f"✅ Оплата подтверждена.\nДилер: {d.title if d else dealer_code}\n"
        f"Метод: {method_full}, сумма: ${amount:g}\nНовый долг: {bal_show}",
    )


@router.callback_query(F.data.startswith("pay:no:"))
async def pay_reject(cb: CallbackQuery, bot: Bot) -> None:
    await cb.answer()
    try:
        pay_id = int(cb.data.split(":")[-1])
    except Exception:
        return
    async with SessionLocal() as session:
        pay = await session.get(Payment, pay_id)
        if not pay:
            await cb.message.answer("Заявка на оплату не найдена.")
            return
        if pay.status != "pending":
            await cb.message.answer(
                f"Заявка уже обработана (статус: {pay.status}).",
            )
            return
        pay.status = "rejected"
        dealer_code = pay.dealer_code
        method = pay.method
        variant = pay.variant
        amount = pay.amount
        await session.commit()
    method_full = f"{method} → {variant}" if variant else method
    d = await get_dealer(dealer_code)
    if d and d.chat_id is not None:
        try:
            await bot.send_message(
                d.chat_id,
                f"❌ Оплата не подтверждена.\nМетод: {method_full}, сумма: ${amount:g}.\n"
                "Свяжитесь с администратором.",
            )
        except Exception as e:

            await _notify_fail(bot, f"дилер {dealer_code}", e)

    await cb.message.answer(
        f"Оплата отклонена.\nДилер: {d.title if d else dealer_code}\nМетод: {method_full}, сумма: ${amount:g}",
    )


# ====== Виды оплат и переименование (админ) ======

async def list_payment_variants(method_id: int, active_only: bool = False) -> list[PaymentVariant]:
    async with SessionLocal() as session:
        q = select(PaymentVariant).where(PaymentVariant.method_id == method_id).order_by(PaymentVariant.id.asc())
        if active_only:
            q = q.where(PaymentVariant.active.is_(True))
        return (await session.execute(q)).scalars().all()


async def get_payment_variant(var_id: int) -> PaymentVariant | None:
    async with SessionLocal() as session:
        return await session.get(PaymentVariant, var_id)


async def _method_card(m: PaymentMethod) -> tuple[str, InlineKeyboardMarkup]:
    variants = await list_payment_variants(m.id)
    lines = [
        f"💳 Метод: {m.name}",
        f"Статус: {'включён' if m.active else 'выключен'}",
        "",
        "Виды оплаты:",
    ]
    if variants:
        for v in variants:
            st = "вкл" if v.active else "выкл"
            rq = "реквизиты заданы" if (v.requisites or "").strip() else "реквизиты не заданы"
            lines.append(f"- {v.name} ({st}, {rq})")
    else:
        lines.append("- видов пока нет — добавьте первый")
    rows = []
    for v in variants:
        mark = "" if v.active else " (выкл)"
        rows.append([InlineKeyboardButton(text=f"➜ {v.name}{mark}", callback_data=f"pv:open:{v.id}")])
    rows.append([InlineKeyboardButton(text="➕ Добавить вид", callback_data=f"pm:vadd:{m.id}")])
    rows.append([
        InlineKeyboardButton(text="✏️ Переименовать", callback_data=f"pm:rename:{m.id}"),
        InlineKeyboardButton(
            text=("🔴 Выключить" if m.active else "🟢 Включить"),
            callback_data=f"pm:toggle:{m.id}",
        ),
    ])
    rows.append([InlineKeyboardButton(text="◀ К списку", callback_data="pm:home")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def _variant_card(v: PaymentVariant, m: PaymentMethod) -> tuple[str, InlineKeyboardMarkup]:
    req = (v.requisites or "").strip() or "(не заданы)"
    text = (
        f"💳 {m.name} → {v.name}\n"
        f"Статус: {'включён' if v.active else 'выключен'}\n\n"
        f"Реквизиты:\n{req}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить реквизиты", callback_data=f"pv:req:{v.id}")],
        [
            InlineKeyboardButton(text="✏️ Переименовать", callback_data=f"pv:rename:{v.id}"),
            InlineKeyboardButton(
                text=("🔴 Выключить" if v.active else "🟢 Включить"),
                callback_data=f"pv:toggle:{v.id}",
            ),
        ],
        [InlineKeyboardButton(text="◀ К методу", callback_data=f"pm:open:{m.id}")],
    ])
    return text, kb


# --- Переименование метода ---

@router.callback_query(F.data.startswith("pm:rename:"))
async def pm_rename_start(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    try:
        pm_id = int(cb.data.split(":")[-1])
    except Exception:
        return
    m = await get_payment_method(pm_id)
    if not m:
        await cb.message.answer("Метод не найден.", reply_markup=await pay_admin_kb())
        return
    await state.clear()
    await state.update_data(pm_id=pm_id)
    await state.set_state(PayAdminStates.waiting_method_rename)
    await cb.message.answer(
        f"Метод «{m.name}». Введите новое название (до 64 символов).\nОтмена — /cancel",
    )


@router.message(PayAdminStates.waiting_method_rename)
async def pm_rename_save(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name or len(name) > 64:
        await message.answer("Название не должно быть пустым или длиннее 64 символов. Ещё раз или /cancel.")
        return
    data = await state.get_data()
    pm_id = data.get("pm_id")
    if not pm_id:
        await state.clear()
        await message.answer("Ввод сброшен. Начните заново — /pay.")
        return
    async with SessionLocal() as session:
        existing = (await session.execute(
            select(PaymentMethod).where(PaymentMethod.name == name, PaymentMethod.id != int(pm_id))
        )).scalars().first()
        if existing:
            await message.answer(f"Метод «{name}» уже существует. Выберите другое название или /cancel.")
            return
        m = await session.get(PaymentMethod, int(pm_id))
        if not m:
            await state.clear()
            await message.answer("Метод не найден.")
            return
        m.name = name
        await session.commit()
    await state.clear()
    await message.answer(f"✅ Метод переименован: {name}")
    m2 = await get_payment_method(int(pm_id))
    if m2:
        text, kb = await _method_card(m2)
        await message.answer(text, reply_markup=kb)


# --- Добавление вида под методом ---

@router.callback_query(F.data.startswith("pm:vadd:"))
async def pm_vadd_start(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    try:
        pm_id = int(cb.data.split(":")[-1])
    except Exception:
        return
    m = await get_payment_method(pm_id)
    if not m:
        await cb.message.answer("Метод не найден.", reply_markup=await pay_admin_kb())
        return
    await state.clear()
    await state.update_data(pm_id=pm_id)
    await state.set_state(PayAdminStates.waiting_variant_name)
    await cb.message.answer(
        f"Метод «{m.name}». Введите название нового вида оплаты (например: TRC-20, Сбербанк).\nОтмена — /cancel",
    )


@router.message(PayAdminStates.waiting_variant_name)
async def pm_vadd_name_save(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name or len(name) > 64:
        await message.answer("Название не должно быть пустым или длиннее 64 символов. Ещё раз или /cancel.")
        return
    data = await state.get_data()
    pm_id = data.get("pm_id")
    if not pm_id:
        await state.clear()
        await message.answer("Ввод сброшен. Начните заново — /pay.")
        return
    await state.update_data(v_name=name)
    await state.set_state(PayAdminStates.waiting_variant_new_req)
    await message.answer(
        f"Вид «{name}». Введите реквизиты (номер карты/кошелька, инструкции).\nИли «-», чтобы пропустить.",
    )


@router.message(PayAdminStates.waiting_variant_new_req)
async def pm_vadd_req_save(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    req = "" if raw in ("-", "—") else raw[:1024]
    data = await state.get_data()
    pm_id = data.get("pm_id")
    name = data.get("v_name")
    await state.clear()
    if not pm_id or not name:
        await message.answer("Ввод сброшен. Начните заново — /pay.")
        return
    async with SessionLocal() as session:
        v = PaymentVariant(method_id=int(pm_id), name=name, requisites=req, active=True)
        session.add(v)
        await session.commit()
    await message.answer(f"✅ Вид «{name}» добавлен.")
    m2 = await get_payment_method(int(pm_id))
    if m2:
        text, kb = await _method_card(m2)
        await message.answer(text, reply_markup=kb)


# --- Карточка вида: открыть, вкл/выкл, реквизиты, переименовать ---

@router.callback_query(F.data.startswith("pv:open:"))
async def pv_open(cb: CallbackQuery) -> None:
    await cb.answer()
    try:
        v_id = int(cb.data.split(":")[-1])
    except Exception:
        return
    v = await get_payment_variant(v_id)
    if not v:
        await cb.message.answer("Вид не найден.", reply_markup=await pay_admin_kb())
        return
    m = await get_payment_method(v.method_id)
    if not m:
        await cb.message.answer("Метод не найден.", reply_markup=await pay_admin_kb())
        return
    text, kb = await _variant_card(v, m)
    await cb.message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("pv:toggle:"))
async def pv_toggle(cb: CallbackQuery) -> None:
    await cb.answer()
    try:
        v_id = int(cb.data.split(":")[-1])
    except Exception:
        return
    async with SessionLocal() as session:
        v = await session.get(PaymentVariant, v_id)
        if not v:
            await cb.message.answer("Вид не найден.", reply_markup=await pay_admin_kb())
            return
        v.active = not v.active
        method_id = v.method_id
        await session.commit()
    v2 = await get_payment_variant(v_id)
    m = await get_payment_method(method_id)
    if v2 and m:
        text, kb = await _variant_card(v2, m)
        await cb.message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("pv:req:"))
async def pv_req_start(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    try:
        v_id = int(cb.data.split(":")[-1])
    except Exception:
        return
    v = await get_payment_variant(v_id)
    if not v:
        await cb.message.answer("Вид не найден.", reply_markup=await pay_admin_kb())
        return
    m = await get_payment_method(v.method_id)
    await state.clear()
    await state.update_data(v_id=v_id)
    await state.set_state(PayAdminStates.waiting_variant_requisites)
    cur = (v.requisites or "").strip() or "(не заданы)"
    name_path = f"{m.name} → {v.name}" if m else v.name
    await cb.message.answer(
        f"{name_path}. Введите реквизиты (номер карты/кошелька, инструкции).\n"
        f"Текущие реквизиты:\n{cur}\n\nОтмена — /cancel",
    )


@router.message(PayAdminStates.waiting_variant_requisites)
async def pv_req_save(message: Message, state: FSMContext) -> None:
    text_in = (message.text or "").strip()
    if not text_in:
        await message.answer("Реквизиты не могут быть пустыми. Ещё раз или /cancel.")
        return
    data = await state.get_data()
    v_id = data.get("v_id")
    await state.clear()
    if not v_id:
        await message.answer("Ввод сброшен. Начните заново — /pay.")
        return
    async with SessionLocal() as session:
        v = await session.get(PaymentVariant, int(v_id))
        if not v:
            await message.answer("Вид не найден.")
            return
        v.requisites = text_in[:1024]
        method_id = v.method_id
        await session.commit()
    await message.answer("✅ Реквизиты обновлены.")
    v2 = await get_payment_variant(int(v_id))
    m = await get_payment_method(method_id)
    if v2 and m:
        text, kb = await _variant_card(v2, m)
        await message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("pv:rename:"))
async def pv_rename_start(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    try:
        v_id = int(cb.data.split(":")[-1])
    except Exception:
        return
    v = await get_payment_variant(v_id)
    if not v:
        await cb.message.answer("Вид не найден.", reply_markup=await pay_admin_kb())
        return
    await state.clear()
    await state.update_data(v_id=v_id)
    await state.set_state(PayAdminStates.waiting_variant_rename)
    await cb.message.answer(
        f"Вид «{v.name}». Введите новое название (до 64 символов).\nОтмена — /cancel",
    )


@router.message(PayAdminStates.waiting_variant_rename)
async def pv_rename_save(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name or len(name) > 64:
        await message.answer("Название не должно быть пустым или длиннее 64 символов. Ещё раз или /cancel.")
        return
    data = await state.get_data()
    v_id = data.get("v_id")
    await state.clear()
    if not v_id:
        await message.answer("Ввод сброшен. Начните заново — /pay.")
        return
    async with SessionLocal() as session:
        v = await session.get(PaymentVariant, int(v_id))
        if not v:
            await message.answer("Вид не найден.")
            return
        v.name = name
        method_id = v.method_id
        await session.commit()
    await message.answer(f"✅ Вид переименован: {name}")
    v2 = await get_payment_variant(int(v_id))
    m = await get_payment_method(method_id)
    if v2 and m:
        text, kb = await _variant_card(v2, m)
        await message.answer(text, reply_markup=kb)


# ====== Отправка ключа дилеру (админ) ======

@router.callback_query(F.data == "dkey:start")
async def dealer_key_start(cb: CallbackQuery) -> None:
    await cb.answer()
    dealers = await list_dealers()
    if not dealers:
        await cb.message.answer("Список дилеров пуст.", reply_markup=await dealers_menu_kb())
        return
    rows = []
    for d in dealers:
        mark = "" if d.chat_id is not None else "  (нет ID)"
        rows.append([InlineKeyboardButton(text=f"🔑 {d.title}{mark}", callback_data=f"dkey:pick:{d.code}")])
    await cb.message.answer("Кому отправить ключ?", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("dkey:pick:"))
async def dealer_key_pick(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    code = cb.data.split(":")[-1]
    d = await get_dealer(code)
    if not d:
        await cb.message.answer("Дилер не найден.", reply_markup=await dealers_menu_kb())
        return
    if d.chat_id is None:
        await cb.message.answer(
            f"У дилера «{d.title}» не задан Telegram ID. Задайте его через ➕ Добавить дилера.",
            reply_markup=await dealers_menu_kb(),
        )
        return
    await state.clear()
    await state.update_data(target_code=code)
    await state.set_state(AdminKeyToDealerStates.waiting_userid)
    await cb.message.answer(
        f"Дилер: {d.title}\nШаг 1/3. Введите USERID клиента (число).\nОтмена — /cancel",
    )


@router.message(AdminKeyToDealerStates.waiting_userid)
async def dealer_key_userid(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer("USERID должен быть числом. Ещё раз или /cancel.")
        return
    await state.update_data(key_uid=text)
    await state.set_state(AdminKeyToDealerStates.waiting_username)
    await message.answer("Шаг 2/3. Введите USERNAME клиента.")


@router.message(AdminKeyToDealerStates.waiting_username)
async def dealer_key_username(message: Message, state: FSMContext) -> None:
    username = (message.text or "").strip()
    if not username or len(username) > 128:
        await message.answer("USERNAME не должно быть пустым и длиннее 128 символов. Ещё раз или /cancel.")
        return
    await state.update_data(key_uname=username)
    await state.set_state(AdminKeyToDealerStates.waiting_keycode)
    await message.answer(
        "Шаг 3/3. Вставьте код ключа (любая длина).",
    )


@router.message(AdminKeyToDealerStates.waiting_keycode)
async def dealer_key_send(message: Message, state: FSMContext, bot: Bot) -> None:
    code_text = (message.text or "").strip()
    if not code_text:
        await message.answer("Код ключа не может быть пустым. Ещё раз или /cancel.")
        return
    data = await state.get_data()
    target_code = data.get("target_code")
    uid = data.get("key_uid")
    uname = data.get("key_uname")
    await state.clear()
    if not target_code or not uid or not uname:
        await message.answer("Ввод сброшен. Начните заново через /dealers.")
        return
    d = await get_dealer(target_code)
    if not d or d.chat_id is None:
        await message.answer("Дилер не найден или нет Telegram ID.")
        return
    safe_uid = html.escape(uid)
    safe_uname = html.escape(uname)
    safe_code = html.escape(code_text)
    body = (
        "🆕 Ваш новый ключ\n"
        f"USERID: {safe_uid}\n"
        f"USERNAME: {safe_uname}\n\n"
        f"<pre>{safe_code}</pre>"
    )
    try:
        await bot.send_message(d.chat_id, body, parse_mode="HTML")
    except (TelegramForbiddenError, TelegramBadRequest):
        await message.answer(
            f"❌ Не удалось отправить дилеру «{d.title}». "
            "Возможно, он не нажимал Start у этого бота.",
        )
        return
    except Exception as e:
        await message.answer(f"❌ Ошибка отправки: {e}")
        return
    await message.answer(
        f"✅ Ключ отправлен дилеру «{d.title}» (USERID {uid}, USERNAME {uname}).",
    )


