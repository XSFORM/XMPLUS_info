from __future__ import annotations

import html
import logging
from collections import defaultdict

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select

from app.db import SessionLocal, Service
from app.keyboards import main_menu_kb
from app.states import ServiceAddStates, ServiceEditStates
from app.utils import now_tz
from app.bot import _trunc, split_text_chunks, send_pre_chunk
from app.services_util import (
    period_label, period_short, shift_months, fmt_amount, parse_amount_currency,
    parse_date_simple, days_left_text, svc_card,
)

log = logging.getLogger(__name__)

router = Router()

# =====================================================================
#   🧾 СЕРВИСЫ — VPS, VDS, домены и т.п. (только администратор)
# =====================================================================

SKIP = ("-", "—")
NAME_W = 16


def services_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="➕ Добавить", callback_data="svc:add"),
            InlineKeyboardButton(text="📋 Список", callback_data="svc:list"),
        ],
        [
            InlineKeyboardButton(text="⏰ Ближайшие", callback_data="svc:next"),
            InlineKeyboardButton(text="✅ Оплачено", callback_data="svc:paidpick"),
        ],
        [
            InlineKeyboardButton(text="✏️ Изменить", callback_data="svc:editpick"),
            InlineKeyboardButton(text="🗑 Удалить", callback_data="svc:delpick"),
        ],
        [InlineKeyboardButton(text="◀ Главное меню", callback_data="svc:back")],
    ])


def period_kb(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Ежемесячно", callback_data=f"{prefix}:1"),
            InlineKeyboardButton(text="Ежегодно", callback_data=f"{prefix}:12"),
        ],
        [
            InlineKeyboardButton(text="3 мес", callback_data=f"{prefix}:3"),
            InlineKeyboardButton(text="6 мес", callback_data=f"{prefix}:6"),
            InlineKeyboardButton(text="Другое", callback_data=f"{prefix}:custom"),
        ],
    ])


async def _all_services() -> list[Service]:
    async with SessionLocal() as session:
        return list((await session.execute(
            select(Service).order_by(Service.due_date.asc())
        )).scalars().all())


async def _get(svc_id: int) -> Service | None:
    async with SessionLocal() as session:
        return await session.get(Service, svc_id)


def _table(items: list[Service]) -> tuple[str, list[str]]:
    today = now_tz().date()
    header = f"{'СЕРВИС'.ljust(NAME_W)} | ПЕРИОД | ОПЛАТА     | ДНЕЙ | СУММА"
    rows = []
    for s in items:
        days = (s.due_date - today).days
        rows.append(
            f"{_trunc(s.name, NAME_W).ljust(NAME_W)} | {period_short(s.period_months).ljust(6)} | "
            f"{s.due_date.isoformat()} | {str(days).rjust(4)} | {fmt_amount(s.amount, s.currency)}"
        )
    return header, rows


def _totals(items: list[Service]) -> str:
    monthly: dict[str, float] = defaultdict(float)
    for s in items:
        if s.amount:
            monthly[s.currency] += s.amount / max(1, s.period_months)
    if not monthly:
        return ""
    lines = ["💵 Расходы в среднем:"]
    for cur, m in sorted(monthly.items()):
        lines.append(f"  {cur}: {m:.2f} в месяц • {m * 12:.2f} в год")
    return "\n".join(lines)


def _pick_kb(items: list[Service], prefix: str) -> InlineKeyboardMarkup:
    today = now_tz().date()
    rows = []
    for s in items[:40]:
        days = (s.due_date - today).days
        rows.append([InlineKeyboardButton(
            text=f"{s.name} • {s.due_date.isoformat()} ({days_left_text(days)})",
            callback_data=f"{prefix}:{s.id}",
        )])
    rows.append([InlineKeyboardButton(text="◀ Назад", callback_data="svc:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---- Вход в раздел ----

async def _show_menu(message: Message) -> None:
    items = await _all_services()
    today = now_tz().date()
    soon = sum(1 for s in items if (s.due_date - today).days <= 7)
    extra = f"\n⚠️ Оплата в ближайшие 7 дней / просрочено: {soon}" if soon else ""
    await message.answer(
        f"🧾 Сервисы ({len(items)} шт.){extra}\nВыберите действие:",
        reply_markup=services_menu_kb(),
    )


@router.message(Command("services"))
@router.message(F.text.in_(["🧾 Сервисы", "/services"]))
async def svc_section(message: Message, state: FSMContext) -> None:
    await state.clear()
    await _show_menu(message)


@router.callback_query(F.data == "svc:menu")
async def svc_menu_cb(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await state.clear()
    await _show_menu(cb.message)


@router.callback_query(F.data == "svc:back")
async def svc_back(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await state.clear()
    await cb.message.answer("Главное меню.", reply_markup=main_menu_kb())


# ---- Список / ближайшие ----

@router.callback_query(F.data == "svc:list")
async def svc_list(cb: CallbackQuery) -> None:
    await cb.answer()
    items = await _all_services()
    if not items:
        await cb.message.answer("🧾 Список сервисов пуст.", reply_markup=services_menu_kb())
        return
    header, rows = _table(items)
    for ch in split_text_chunks(f"🧾 Сервисы ({len(items)}):\n{header}\n{'─' * len(header)}", rows):
        await send_pre_chunk(cb.message, ch)
    totals = _totals(items)
    await cb.message.answer(totals or f"Всего: {len(items)}", reply_markup=services_menu_kb())


@router.callback_query(F.data == "svc:next")
async def svc_next(cb: CallbackQuery) -> None:
    await cb.answer()
    today = now_tz().date()
    items = [s for s in await _all_services() if (s.due_date - today).days <= 30]
    if not items:
        await cb.message.answer("🧾 Нет оплат в ближайшие 30 дней.", reply_markup=services_menu_kb())
        return
    header, rows = _table(items)
    for ch in split_text_chunks(f"⏰ Оплаты в ближайшие 30 дней ({len(items)}):\n{header}\n{'─' * len(header)}", rows):
        await send_pre_chunk(cb.message, ch)
    await cb.message.answer(f"Ближайших: {len(items)}", reply_markup=services_menu_kb())


# ---- Добавить ----

@router.callback_query(F.data == "svc:add")
async def svc_add_start(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await state.clear()
    await state.set_state(ServiceAddStates.waiting_name)
    await cb.message.answer(
        "🧾 Новый сервис\n\nШаг 1/6. Название (например «VPS Hetzner», «домен xmplus.com»):\n"
        "Отмена — /cancel"
    )


@router.message(ServiceAddStates.waiting_name)
async def svc_add_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name or name.startswith("/"):
        await message.answer("Введите название сервиса:")
        return
    await state.update_data(name=name[:128])
    await message.answer("Шаг 2/6. Как часто платить?", reply_markup=period_kb("svcp"))


@router.callback_query(F.data.startswith("svcp:"))
async def svc_add_period(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    if "name" not in (await state.get_data()):
        return
    val = cb.data.split(":")[1]
    if val == "custom":
        await state.set_state(ServiceAddStates.waiting_period_custom)
        await cb.message.answer("Введите период в месяцах (например 2 или 24):")
        return
    await _ask_due(cb.message, state, int(val))


@router.message(ServiceAddStates.waiting_period_custom)
async def svc_add_period_custom(message: Message, state: FSMContext) -> None:
    t = (message.text or "").strip()
    if not t.isdigit() or not (1 <= int(t) <= 120):
        await message.answer("Нужно число месяцев от 1 до 120:")
        return
    await _ask_due(message, state, int(t))


async def _ask_due(message: Message, state: FSMContext, months: int) -> None:
    await state.update_data(period=months)
    await state.set_state(ServiceAddStates.waiting_due)
    await message.answer(
        f"Период: {period_label(months)}\n\n"
        "Шаг 3/6. Дата следующей оплаты:\n"
        "Формат: 2026-12-05, 05.12.2026 или просто 05.12"
    )


@router.message(ServiceAddStates.waiting_due)
async def svc_add_due(message: Message, state: FSMContext) -> None:
    d = parse_date_simple(message.text or "", now_tz().date())
    if not d:
        await message.answer("Не понял дату. Пример: 2026-12-05 или 05.12")
        return
    await state.update_data(due=d.isoformat())
    await state.set_state(ServiceAddStates.waiting_amount)
    await message.answer(
        "Шаг 4/6. Сумма и валюта (например «5.5 $», «12 eur», «500 руб»).\n"
        "Без валюты — считаю в $. «-» — пропустить."
    )


@router.message(ServiceAddStates.waiting_amount)
async def svc_add_amount(message: Message, state: FSMContext) -> None:
    t = (message.text or "").strip()
    if t in SKIP:
        amount, cur = 0.0, "$"
    else:
        parsed = parse_amount_currency(t)
        if not parsed:
            await message.answer("Не понял сумму. Пример: 5.5 $ или 500 руб. «-» — пропустить.")
            return
        amount, cur = parsed
    await state.update_data(amount=amount, currency=cur)
    await state.set_state(ServiceAddStates.waiting_url)
    await message.answer("Шаг 5/6. Ссылка на панель оплаты (https://…) или «-»:")


@router.message(ServiceAddStates.waiting_url)
async def svc_add_url(message: Message, state: FSMContext) -> None:
    t = (message.text or "").strip()
    if t not in SKIP and not t.startswith(("http://", "https://")):
        t = "https://" + t
    await state.update_data(url="" if t in SKIP else t[:512])
    await state.set_state(ServiceAddStates.waiting_note)
    await message.answer("Шаг 6/6. Заметка (аккаунт, логин, IP…) или «-»:")


@router.message(ServiceAddStates.waiting_note)
async def svc_add_note(message: Message, state: FSMContext) -> None:
    t = (message.text or "").strip()
    data = await state.get_data()
    await state.clear()
    from datetime import date as _date
    due = _date.fromisoformat(data["due"])
    async with SessionLocal() as session:
        s = Service(
            name=data["name"],
            period_months=int(data["period"]),
            due_date=due,
            billing_day=due.day,
            amount=float(data.get("amount") or 0),
            currency=data.get("currency") or "$",
            url=data.get("url") or "",
            note="" if t in SKIP else t[:255],
        )
        session.add(s)
        await session.commit()
        await session.refresh(s)
    days = (s.due_date - now_tz().date()).days
    await message.answer(
        f"✅ Сервис добавлен\n\n{svc_card(s)}\n\n"
        f"До оплаты: {days_left_text(days)}. Напомню за 7, 3, 1 день и в день оплаты.",
        reply_markup=services_menu_kb(),
    )


# ---- Оплачено / отложить (из уведомления и из меню) ----

@router.callback_query(F.data == "svc:paidpick")
async def svc_paid_pick(cb: CallbackQuery) -> None:
    await cb.answer()
    items = await _all_services()
    if not items:
        await cb.message.answer("Список сервисов пуст.", reply_markup=services_menu_kb())
        return
    await cb.message.answer("✅ Какой сервис оплачен?", reply_markup=_pick_kb(items, "svc:paid"))


@router.callback_query(F.data.startswith("svc:paid:"))
async def svc_paid(cb: CallbackQuery) -> None:
    svc_id = int(cb.data.split(":")[2])
    async with SessionLocal() as session:
        s = await session.get(Service, svc_id)
        if not s:
            await cb.answer("Сервис не найден", show_alert=True)
            return
        old = s.due_date
        s.due_date = shift_months(old, s.period_months, s.billing_day)
        s.last_paid_on = now_tz().date()
        s.snooze_date = None
        await session.commit()
        await session.refresh(s)
    await cb.answer("Отмечено как оплачено")
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="↩️ Отменить", callback_data=f"svc:undo:{svc_id}:{old.isoformat()}"),
    ]])
    await cb.message.answer(
        f"✅ {s.name}: оплачено.\nСледующая оплата: {s.due_date.isoformat()} "
        f"({days_left_text((s.due_date - now_tz().date()).days)})",
        reply_markup=kb,
    )


@router.callback_query(F.data.startswith("svc:undo:"))
async def svc_undo(cb: CallbackQuery) -> None:
    from datetime import date as _date
    _, _, sid, old = cb.data.split(":")
    async with SessionLocal() as session:
        s = await session.get(Service, int(sid))
        if not s:
            await cb.answer("Сервис не найден", show_alert=True)
            return
        s.due_date = _date.fromisoformat(old)
        s.last_paid_on = None
        await session.commit()
    await cb.answer("Отменено")
    await cb.message.answer(f"↩️ {s.name}: дата оплаты возвращена на {old}.")


@router.callback_query(F.data.startswith("svc:snz:"))
async def svc_snooze(cb: CallbackQuery) -> None:
    from datetime import timedelta
    svc_id = int(cb.data.split(":")[2])
    tomorrow = (now_tz().date() + timedelta(days=1)).isoformat()
    async with SessionLocal() as session:
        s = await session.get(Service, svc_id)
        if not s:
            await cb.answer("Сервис не найден", show_alert=True)
            return
        s.snooze_date = tomorrow
        await session.commit()
    await cb.answer("Напомню завтра")
    await cb.message.answer(f"⏰ {s.name}: напомню завтра.")


# ---- Изменить ----

FIELD_LABELS = {
    "name": "Название",
    "period": "Период",
    "due": "Дата оплаты",
    "amount": "Сумма",
    "url": "Ссылка",
    "note": "Заметка",
}


def _edit_kb(svc_id: int) -> InlineKeyboardMarkup:
    b = lambda f: InlineKeyboardButton(text=f"✏️ {FIELD_LABELS[f]}", callback_data=f"svce:f:{svc_id}:{f}")
    return InlineKeyboardMarkup(inline_keyboard=[
        [b("name"), b("period")],
        [b("due"), b("amount")],
        [b("url"), b("note")],
        [InlineKeyboardButton(text="◀ Назад", callback_data="svc:menu")],
    ])


@router.callback_query(F.data == "svc:editpick")
async def svc_edit_pick(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await state.clear()
    items = await _all_services()
    if not items:
        await cb.message.answer("Список сервисов пуст.", reply_markup=services_menu_kb())
        return
    await cb.message.answer("✏️ Какой сервис изменить?", reply_markup=_pick_kb(items, "svce:pick"))


@router.callback_query(F.data.startswith("svce:pick:"))
async def svc_edit_card(cb: CallbackQuery) -> None:
    await cb.answer()
    s = await _get(int(cb.data.split(":")[2]))
    if not s:
        await cb.message.answer("Сервис не найден.", reply_markup=services_menu_kb())
        return
    await cb.message.answer(svc_card(s), reply_markup=_edit_kb(s.id))


@router.callback_query(F.data.startswith("svce:f:"))
async def svc_edit_field(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    _, _, sid, field = cb.data.split(":")
    if field == "period":
        await cb.message.answer("Новый период:", reply_markup=period_kb(f"svcep:{sid}"))
        return
    await state.set_state(ServiceEditStates.waiting_value)
    await state.update_data(svc_id=int(sid), field=field)
    hints = {
        "due": "Новая дата оплаты (2026-12-05, 05.12.2026 или 05.12):",
        "amount": "Новая сумма и валюта (например «5.5 $»), «-» — убрать:",
        "url": "Новая ссылка (https://…), «-» — убрать:",
        "note": "Новая заметка, «-» — убрать:",
        "name": "Новое название:",
    }
    await cb.message.answer(hints[field])


@router.callback_query(F.data.startswith("svcep:"))
async def svc_edit_period(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    parts = cb.data.split(":")
    sid, val = int(parts[1]), parts[2]
    if val == "custom":
        await state.set_state(ServiceEditStates.waiting_value)
        await state.update_data(svc_id=sid, field="period")
        await cb.message.answer("Введите период в месяцах (например 2 или 24):")
        return
    await _save_field(cb.message, state, sid, "period", int(val))


@router.message(ServiceEditStates.waiting_value)
async def svc_edit_value(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    sid, field = int(data["svc_id"]), data["field"]
    t = (message.text or "").strip()
    value: object
    if field == "name":
        if not t:
            await message.answer("Название не может быть пустым:")
            return
        value = t[:128]
    elif field == "period":
        if not t.isdigit() or not (1 <= int(t) <= 120):
            await message.answer("Нужно число месяцев от 1 до 120:")
            return
        value = int(t)
    elif field == "due":
        value = parse_date_simple(t, now_tz().date())
        if not value:
            await message.answer("Не понял дату. Пример: 2026-12-05 или 05.12")
            return
    elif field == "amount":
        if t in SKIP:
            value = (0.0, None)
        else:
            parsed = parse_amount_currency(t)
            if not parsed:
                await message.answer("Не понял сумму. Пример: 5.5 $")
                return
            value = parsed
    elif field == "url":
        if t in SKIP:
            value = ""
        else:
            value = (t if t.startswith(("http://", "https://")) else "https://" + t)[:512]
    else:  # note
        value = "" if t in SKIP else t[:255]
    await _save_field(message, state, sid, field, value)


async def _save_field(message: Message, state: FSMContext, sid: int, field: str, value) -> None:
    await state.clear()
    async with SessionLocal() as session:
        s = await session.get(Service, sid)
        if not s:
            await message.answer("Сервис не найден.", reply_markup=services_menu_kb())
            return
        if field == "name":
            s.name = value
        elif field == "period":
            s.period_months = value
        elif field == "due":
            s.due_date = value
            s.billing_day = value.day
            s.last_notified_on = None
            s.snooze_date = None
        elif field == "amount":
            s.amount = value[0]
            if value[1]:
                s.currency = value[1]
        elif field == "url":
            s.url = value
        elif field == "note":
            s.note = value
        await session.commit()
        await session.refresh(s)
    await message.answer(f"✅ Сохранено\n\n{svc_card(s)}", reply_markup=_edit_kb(s.id))


# ---- Удалить ----

@router.callback_query(F.data == "svc:delpick")
async def svc_del_pick(cb: CallbackQuery) -> None:
    await cb.answer()
    items = await _all_services()
    if not items:
        await cb.message.answer("Список сервисов пуст.", reply_markup=services_menu_kb())
        return
    await cb.message.answer("🗑 Какой сервис удалить?", reply_markup=_pick_kb(items, "svc:del"))


@router.callback_query(F.data.startswith("svc:del:"))
async def svc_del_ask(cb: CallbackQuery) -> None:
    await cb.answer()
    s = await _get(int(cb.data.split(":")[2]))
    if not s:
        await cb.message.answer("Сервис не найден.", reply_markup=services_menu_kb())
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Удалить", callback_data=f"svc:delok:{s.id}"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="svc:menu"),
    ]])
    await cb.message.answer(f"Удалить сервис?\n\n{svc_card(s)}", reply_markup=kb)


@router.callback_query(F.data.startswith("svc:delok:"))
async def svc_del_exec(cb: CallbackQuery) -> None:
    await cb.answer()
    async with SessionLocal() as session:
        s = await session.get(Service, int(cb.data.split(":")[2]))
        if not s:
            await cb.message.answer("Сервис уже удалён.", reply_markup=services_menu_kb())
            return
        name = s.name
        await session.delete(s)
        await session.commit()
    await cb.message.answer(f"🗑 Сервис «{name}» удалён.", reply_markup=services_menu_kb())
