from __future__ import annotations

import logging

from datetime import datetime, timezone, timedelta
import csv, io, html, json, os, re, calendar, zipfile, shutil
from pathlib import Path
from typing import List

from aiogram import Router, Bot, F
from aiogram.filters import CommandStart, Command, BaseFilter
from aiogram.types import (
    Message,
    CallbackQuery,
    BotCommand,
    BotCommandScopeDefault,
    BotCommandScopeChat,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
    MenuButtonCommands,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BufferedInputFile,
)
from aiogram.fsm.state import StatesGroup, State

from app.states import (
    EditStates, DealerEditStates, DealerOrderStates,
    DealerRenewStates, DealerPayStates, OrderFulfillStates, RenewStates,
)
from app.keyboards import main_menu_kb, choose_by_due_kb, dealer_user_menu_kb
from aiogram.fsm.context import FSMContext

from sqlalchemy import select, delete, update

from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest

from app.db import (
    SessionLocal, engine, Item, RouterItem, Dealer, DealerOrder, BalanceTxn, PaymentMethod, PaymentVariant, Payment,
    get_price, set_price, apply_balance_change,
)
from app.config import settings
from app.utils import (
    parse_datetime_human,
    fmt_dt_human,
    now_tz,
    to_tz,
    tz_offset_str,
    get_active_timezone_name,
    set_active_timezone_name,
)

router = Router()

log = logging.getLogger(__name__)


async def _get_order(oid: str):
    """Fetch DealerOrder by id, return dict-like or None."""
    try:
        order_id = int(oid)
    except (ValueError, TypeError):
        return None
    async with SessionLocal() as session:
        row = (await session.execute(
            select(DealerOrder).where(DealerOrder.id == order_id)
        )).scalars().first()
        if not row:
            return None
        return {
            "id": row.id,
            "dealer_code": row.dealer_code,
            "dealer_title": row.dealer_title,
            "dealer_chat_id": row.dealer_chat_id,
            "names": json.loads(row.names_json),
            "fulfilled": row.fulfilled,
        }


async def _update_order_fulfilled(oid: str, fulfilled: int) -> None:
    """Update fulfilled counter in DB."""
    async with SessionLocal() as session:
        await session.execute(
            update(DealerOrder).where(DealerOrder.id == int(oid)).values(fulfilled=fulfilled)
        )
        await session.commit()


async def _delete_order(oid: str) -> None:
    """Delete completed order from DB."""
    async with SessionLocal() as session:
        await session.execute(
            delete(DealerOrder).where(DealerOrder.id == int(oid))
        )
        await session.commit()


async def _notify_fail(bot: Bot, dest_name: str, err: Exception) -> None:
    """Логировать ошибку отправки и уведомить админа."""
    log.warning("send_message to %s failed: %s", dest_name, err)
    owner = int(settings.OWNER_CHAT_ID) if settings.OWNER_CHAT_ID else None
    if owner:
        try:
            await bot.send_message(
                owner,
                f"\u26a0\ufe0f \u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u043e\u0442\u043f\u0440\u0430\u0432\u0438\u0442\u044c \u0441\u043e\u043e\u0431\u0449\u0435\u043d\u0438\u0435: {dest_name}\n\u041f\u0440\u0438\u0447\u0438\u043d\u0430: {err}",
            )
        except Exception:
            pass


# Команды меню в зависимости от режима
BOT_COMMANDS_ADMIN = [
    BotCommand(command="start", description="Запуск бота"),
    BotCommand(command="help", description="Справка по командам"),
    BotCommand(command="add", description="Добавить (мастер: USERID → USERNAME → дата/время)"),
    BotCommand(command="renew", description="Продлить по USERID"),
    BotCommand(command="delete", description="Удалить по USERID (с подтверждением)"),
    BotCommand(command="list", description="Список (отсортировано по дате)"),
    BotCommand(command="disabled", description="Список отключённых (просроченных)"),
    BotCommand(command="next", description="Ближайшие истечения"),
    BotCommand(command="dealers", description="Раздел диллеры"),
    BotCommand(command="balance", description="Балансы и долги дилеров"),
    BotCommand(command="pay", description="Методы оплаты"),
    BotCommand(command="edit", description="Редактировать ключ"),
    BotCommand(command="status", description="Статус бота"),
    BotCommand(command="backup", description="Бэкап базы данных"),
    BotCommand(command="timezone", description="Показать/сменить локальное время (TZ)"),
    BotCommand(command="cancel", description="Отменить текущий ввод"),
    BotCommand(command="menu", description="Показать клавиатуру"),
    BotCommand(command="hide", description="Скрыть клавиатуру"),
]
BOT_COMMANDS_DEALER = [
    BotCommand(command="start", description="Запуск бота"),
    BotCommand(command="help", description="Справка по командам"),
    BotCommand(command="list", description="Список (только ваши записи)"),
    BotCommand(command="disabled", description="Список отключённых (только ваши)"),
    BotCommand(command="next", description="Ближайшие 3 дня (только ваши)"),
    BotCommand(command="renew", description="Запрос на продление клиента"),
    BotCommand(command="order", description="Заказать новые ключи"),
    BotCommand(command="edit", description="Изменить имя клиента"),
    BotCommand(command="balance", description="Ваш баланс (долг)"),
    BotCommand(command="pay", description="Оплата и реквизиты"),
    BotCommand(command="status", description="Статус"),
]

def is_dealer_mode() -> bool:
    return settings.BOT_MODE == "dealer"


# ====== Роли пользователей и контроль доступа (единый бот) ======

async def resolve_role(user_id: int) -> str:
    """
    Роль: 'owner' (администратор), 'dealer' (дилер из БД) или 'none' (нет доступа).
    В legacy dealer-контейнере владелец = сам дилер, остальные — 'none'.
    """
    if settings.OWNER_CHAT_ID and str(user_id) == str(settings.OWNER_CHAT_ID):
        return "owner"
    if is_dealer_mode():
        return "none"
    async with SessionLocal() as session:
        found = (await session.execute(select(Dealer.id).where(Dealer.chat_id == user_id))).first()
    return "dealer" if found else "none"


async def dealer_by_chat(user_id: int) -> Dealer | None:
    """Дилер по его Telegram chat_id."""
    async with SessionLocal() as session:
        return (await session.execute(select(Dealer).where(Dealer.chat_id == user_id))).scalars().first()


class IsOwner(BaseFilter):
    """Пропускает только администратора."""
    async def __call__(self, event) -> bool:
        u = getattr(event, "from_user", None)
        return u is not None and (await resolve_role(u.id)) == "owner"


class IsDealer(BaseFilter):
    """Пропускает только дилеров."""
    async def __call__(self, event) -> bool:
        u = getattr(event, "from_user", None)
        return u is not None and (await resolve_role(u.id)) == "dealer"


class IsGuest(BaseFilter):
    """Пропускает тех, у кого нет доступа."""
    async def __call__(self, event) -> bool:
        u = getattr(event, "from_user", None)
        return u is not None and (await resolve_role(u.id)) == "none"


# Основной router — только администратор. Дилеры и гости — отдельные роутеры.
dealer_router = Router()
guest_router = Router()

router.message.filter(IsOwner())
router.callback_query.filter(IsOwner())
dealer_router.message.filter(IsDealer())
dealer_router.callback_query.filter(IsDealer())
guest_router.message.filter(IsGuest())
guest_router.callback_query.filter(IsGuest())

# Sub-router: admin-mode-only handlers (not registered in dealer mode)
_admin = Router()
if not is_dealer_mode():
    router.include_router(_admin)
else:
    # In dealer mode, answer any stray admin-only callback
    @router.callback_query()
    async def _dealer_cb_fallback(cb: CallbackQuery) -> None:
        await cb.answer()


# ---- helpers: длинный текст, CSV-экспорт и аккуратные таблицы без ID ----

MESSAGE_LIMIT = 3900  # запас к лимиту 4096

def split_text_chunks(header: str, lines: list[str]) -> list[str]:
    chunks = []
    current = header + "\n"
    for ln in lines:
        add = ln + "\n"
        if len(current) + len(add) > MESSAGE_LIMIT:
            chunks.append(current.rstrip())
            current = "(продолжение)\n" + add
        else:
            current += add
    if current.strip():
        chunks.append(current.rstrip())
    return chunks

async def build_items_csv_bytes(items) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["user_id", "username", "note", "due_date"])
    for it in items:
        w.writerow([it.user_id, it.username, getattr(it, "note", "") or "", fmt_dt_human(it.due_date)])
    data = buf.getvalue().encode("utf-8")
    buf.close()
    return data

# Вариант A: фиксированные ширины колонок
UID_W = 5
UNAME_W = 8

def _trunc(s: str, width: int) -> str:
    return s if len(s) <= width else (s[: max(0, width - 1)] + "…")

NOTE_W = 10

def make_table_lines_without_id(items) -> tuple[str, list[str]]:
    header = f"{'USERID'.rjust(UID_W)} | {'USERNAME'.ljust(UNAME_W)} | {'КЛИЕНТ'.ljust(NOTE_W)} | DUE DATE"
    rows: list[str] = []
    for it in items:
        uid = str(it.user_id).rjust(UID_W)
        uname = _trunc(it.username, UNAME_W).ljust(UNAME_W)
        note = _trunc(getattr(it, "note", "") or "", NOTE_W).ljust(NOTE_W)
        due = fmt_dt_human(it.due_date)
        rows.append(f"{uid} | {uname} | {note} | {due}")
    return header, rows

def send_pre_chunk(message: Message, text: str):
    return message.answer(f"<pre>{html.escape(text, quote=False)}</pre>", parse_mode="HTML")

def dealer_filter(query):
    if is_dealer_mode():
        return query.where(Item.dealer == settings.DEALER_NAME)
    return query

async def set_bot_commands(bot: Bot) -> None:
    commands = BOT_COMMANDS_DEALER if is_dealer_mode() else BOT_COMMANDS_ADMIN
    await bot.set_my_commands(commands=commands, scope=BotCommandScopeDefault())
    # В admin-боте каждому дилеру показываем только его набор команд
    if not is_dealer_mode():
        try:
            for d in await list_dealers():
                if d.chat_id is not None:
                    try:
                        await bot.set_my_commands(
                            commands=BOT_COMMANDS_DEALER,
                            scope=BotCommandScopeChat(chat_id=d.chat_id),
                        )
                    except Exception:
                        pass
        except Exception:
            pass
    try:
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    except Exception:
        pass

@router.message(CommandStart())
@router.message(F.text == "/start")
async def on_start(message: Message) -> None:
    role = "dealer" if is_dealer_mode() else "admin"
    who = f" ({settings.DEALER_NAME})" if is_dealer_mode() else ""
    await message.answer(
        f"✅ XMPLUS запущен [{role}{who}].\n"
        "Команды — в меню (кнопка с квадратами) и на клавиатуре ниже.",
        reply_markup=main_menu_kb(),
    )

@router.message(Command("help"))
@router.message(F.text.in_(["/help", "ℹ️ Помощь"]))
async def on_help(message: Message) -> None:
    commands = BOT_COMMANDS_DEALER if is_dealer_mode() else BOT_COMMANDS_ADMIN
    text = "Доступные команды:\n" + "\n".join([f"/{c.command} — {c.description}" for c in commands])
    await message.answer(text)

@router.message(Command("menu"))
@router.message(F.text == "/menu")
async def show_menu(message: Message) -> None:
    await message.answer("Клавиатура показана.", reply_markup=main_menu_kb())

@router.message(Command("hide"))
@router.message(F.text.in_(["/hide", "👁 Скрыть"]))
async def hide_menu(message: Message) -> None:
    await message.answer("Клавиатура скрыта.", reply_markup=ReplyKeyboardRemove())

@router.message(Command("status"))
@router.message(F.text.in_(["/status", "📊 Статус"]))
async def on_status(message: Message) -> None:
    async with SessionLocal() as session:
        q = dealer_filter(select(Item))
        total = (await session.execute(q)).scalars().unique().all()
    role = "dealer" if is_dealer_mode() else "admin"
    who = f" ({settings.DEALER_NAME})" if is_dealer_mode() else ""
    await message.answer(
        f"Бот работает ✅\nРежим: {role}{who}\nВ базе записей (в пределах вашей видимости): {len(total)}\n"
        f"ACTIVE_TZ: {get_active_timezone_name()} (UTC{tz_offset_str()})",
    )

# ==== Таймзона ====

def tz_switch_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="GMT+5 • Ashgabat", callback_data="tz:set:Asia/Ashgabat"),
            InlineKeyboardButton(text="GMT+8 • Singapore", callback_data="tz:set:Asia/Singapore"),
        ]
    ])

@_admin.message(Command("timezone"))
@_admin.message(F.text.in_(["/timezone", "🌐 Часовой пояс"]))
async def show_timezone(message: Message) -> None:
    local_now = now_tz()
    utc_now = datetime.now(timezone.utc)
    text = (
        f"Активный часовой пояс: {get_active_timezone_name()} (UTC{tz_offset_str()})\n"
        f"Локальное время: {local_now.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
        f"UTC:            {utc_now.strftime('%Y-%m-%d %H:%M:%S UTC')}\n\n"
        "Переключить:"
    )
    await message.answer(text, reply_markup=tz_switch_kb())

@_admin.callback_query(F.data.startswith("tz:set:"))
async def tz_set(cb: CallbackQuery) -> None:
    await cb.answer()
    tz_name = cb.data.split(":", 2)[-1]
    ok = set_active_timezone_name(tz_name)
    if ok:
        await cb.message.answer(f"✅ Часовой пояс установлен: {tz_name} (UTC{tz_offset_str()})")
    else:
        await cb.message.answer("❌ Не удалось установить часовой пояс. Проверьте логи.")

@_admin.message(Command("cancel"))
@_admin.message(F.text.in_(["/cancel", "❌ Отмена"]))
async def on_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.", reply_markup=main_menu_kb())
async def list_export_csv(cb: CallbackQuery) -> None:
    async with SessionLocal() as session:
        q = dealer_filter(select(Item).order_by(Item.due_date.asc()))
        items = (await session.execute(q)).scalars().all()
    data = await build_items_csv_bytes(items)
    await cb.message.answer_document(
        BufferedInputFile(data, filename="clients_export.csv"),
        caption=f"Экспорт: {len(items)} записей"
    )


# ==== Редактирование ключей (админ) ====


def _item_card(it) -> str:
    note = getattr(it, "note", "") or ""
    note_line = f"Клиент: {note}\n" if note else ""
    return (
        f"USERID: {it.user_id}\n"
        f"USERNAME: {it.username}\n"
        f"{note_line}"
        f"DUE: {fmt_dt_human(it.due_date)}\n"
        f"Дилер: {it.dealer}"
    )


def _edit_kb(item_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="\u270f\ufe0f USERID", callback_data=f"edit:f:{item_id}:user_id"),
            InlineKeyboardButton(text="\u270f\ufe0f USERNAME", callback_data=f"edit:f:{item_id}:username"),
        ],
        [
            InlineKeyboardButton(text="\u270f\ufe0f \u041a\u043b\u0438\u0435\u043d\u0442", callback_data=f"edit:f:{item_id}:note"),
            InlineKeyboardButton(text="\u270f\ufe0f \u0414\u0430\u0442\u0430", callback_data=f"edit:f:{item_id}:due_date"),
        ],
        [InlineKeyboardButton(text="\u25c0 \u041d\u0430\u0437\u0430\u0434", callback_data="edit:back")],
    ])


FIELD_LABELS = {
    "user_id": "USERID",
    "username": "USERNAME",
    "note": "\u0417\u0430\u043c\u0435\u0442\u043a\u0430 (\u0438\u043c\u044f \u043a\u043b\u0438\u0435\u043d\u0442\u0430)",
    "due_date": "\u0414\u0430\u0442\u0430 \u043e\u0442\u043a\u043b\u044e\u0447\u0435\u043d\u0438\u044f",
}


@_admin.message(Command("edit"))
@_admin.message(F.text.in_(["/edit", "✏️ Редактор"]))
async def edit_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(EditStates.waiting_search)
    await message.answer(
        "\u270f\ufe0f \u0420\u0435\u0434\u0430\u043a\u0442\u043e\u0440 \u043a\u043b\u044e\u0447\u0435\u0439\n\n"
        "\u0412\u0432\u0435\u0434\u0438\u0442\u0435 USERID \u0438\u043b\u0438 \u0438\u043c\u044f \u043a\u043b\u0438\u0435\u043d\u0442\u0430 \u0434\u043b\u044f \u043f\u043e\u0438\u0441\u043a\u0430:",
    )


@router.message(EditStates.waiting_search)
async def edit_search(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("\u0412\u0432\u0435\u0434\u0438\u0442\u0435 USERID \u0438\u043b\u0438 \u0438\u043c\u044f \u043a\u043b\u0438\u0435\u043d\u0442\u0430:")
        return
    async with SessionLocal() as session:
        if text.isdigit():
            q = select(Item).where(Item.user_id == int(text))
        else:
            from sqlalchemy import or_
            q = select(Item).where(or_(
                Item.username.ilike(f"%{text}%"),
                Item.note.ilike(f"%{text}%"),
            ))
        items = (await session.execute(q)).scalars().all()
    if not items:
        await message.answer("\u041d\u0438\u0447\u0435\u0433\u043e \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d\u043e. \u041f\u043e\u043f\u0440\u043e\u0431\u0443\u0439\u0442\u0435 \u0435\u0449\u0451 \u0440\u0430\u0437 \u0438\u043b\u0438 /cancel.")
        return
    if len(items) == 1:
        it = items[0]
        await state.clear()
        await message.answer(
            f"\U0001f4cb {_item_card(it)}",
            reply_markup=_edit_kb(it.id),
        )
        return
    # Multiple results - show list with buttons
    rows = []
    for it in items[:10]:
        note = getattr(it, "note", "") or ""
        label = f"{it.user_id} | {it.username}"
        if note:
            label += f" | {note}"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"edit:pick:{it.id}")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await state.clear()
    await message.answer(f"\u041d\u0430\u0439\u0434\u0435\u043d\u043e {len(items)} \u0437\u0430\u043f\u0438\u0441\u0435\u0439. \u0412\u044b\u0431\u0435\u0440\u0438\u0442\u0435:", reply_markup=kb)


@router.callback_query(F.data.startswith("edit:pick:"))
async def edit_pick(cb: CallbackQuery) -> None:
    await cb.answer()
    item_id = int(cb.data.split(":")[-1])
    async with SessionLocal() as session:
        it = (await session.execute(select(Item).where(Item.id == item_id))).scalars().first()
    if not it:
        await cb.message.answer("\u0417\u0430\u043f\u0438\u0441\u044c \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d\u0430.")
        return
    await cb.message.answer(
        f"\U0001f4cb {_item_card(it)}",
        reply_markup=_edit_kb(it.id),
    )


@router.callback_query(F.data.startswith("edit:f:"))
async def edit_field_start(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    parts = cb.data.split(":")
    item_id = int(parts[2])
    field = parts[3]
    label = FIELD_LABELS.get(field, field)
    await state.set_state(EditStates.waiting_value)
    await state.update_data(edit_item_id=item_id, edit_field=field)
    if field == "due_date":
        await cb.message.answer(
            f"\u0412\u0432\u0435\u0434\u0438\u0442\u0435 \u043d\u043e\u0432\u0443\u044e \u0434\u0430\u0442\u0443 (YYYY-MM-DD HH:MM:SS):"
        )
    elif field == "user_id":
        await cb.message.answer(f"\u0412\u0432\u0435\u0434\u0438\u0442\u0435 \u043d\u043e\u0432\u044b\u0439 USERID (\u0447\u0438\u0441\u043b\u043e):")
    else:
        await cb.message.answer(f"\u0412\u0432\u0435\u0434\u0438\u0442\u0435 \u043d\u043e\u0432\u043e\u0435 \u0437\u043d\u0430\u0447\u0435\u043d\u0438\u0435 \u0434\u043b\u044f {label}:")


@router.message(EditStates.waiting_value)
async def edit_field_save(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("\u0417\u043d\u0430\u0447\u0435\u043d\u0438\u0435 \u043d\u0435 \u043c\u043e\u0436\u0435\u0442 \u0431\u044b\u0442\u044c \u043f\u0443\u0441\u0442\u044b\u043c.")
        return
    data = await state.get_data()
    item_id = data["edit_item_id"]
    field = data["edit_field"]
    async with SessionLocal() as session:
        it = (await session.execute(select(Item).where(Item.id == item_id))).scalars().first()
        if not it:
            await state.clear()
            await message.answer("\u0417\u0430\u043f\u0438\u0441\u044c \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d\u0430.")
            return
        if field == "user_id":
            if not text.isdigit():
                await message.answer("USERID \u0434\u043e\u043b\u0436\u0435\u043d \u0431\u044b\u0442\u044c \u0447\u0438\u0441\u043b\u043e\u043c. \u0415\u0449\u0451 \u0440\u0430\u0437:")
                return
            it.user_id = int(text)
        elif field == "username":
            it.username = text
        elif field == "note":
            it.note = text
        elif field == "due_date":
            dt = parse_datetime_human(text)
            if not dt:
                await message.answer("\u041d\u0435\u0432\u0435\u0440\u043d\u044b\u0439 \u0444\u043e\u0440\u043c\u0430\u0442. YYYY-MM-DD HH:MM:SS. \u0415\u0449\u0451 \u0440\u0430\u0437:")
                return
            it.due_date = dt
        await session.commit()
        await session.refresh(it)
    await state.clear()
    await message.answer(
        f"\u2705 \u0421\u043e\u0445\u0440\u0430\u043d\u0435\u043d\u043e!\n\n\U0001f4cb {_item_card(it)}",
        reply_markup=_edit_kb(it.id),
    )


@router.callback_query(F.data == "edit:back")
async def edit_back(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await state.clear()
    await cb.message.answer("\u0413\u043e\u0442\u043e\u0432\u043e.")


# ==== Списки ====

@router.message(Command("list"))
@router.message(F.text.in_(["/list", "📋 Список"]))
async def on_list(message: Message) -> None:
    async with SessionLocal() as session:
        q = dealer_filter(select(Item).order_by(Item.due_date.asc()))
        items = (await session.execute(q)).scalars().all()
    if not items:
        await message.answer("Список пуст.")
        return
    header, lines = make_table_lines_without_id(items)
    chunks = split_text_chunks(header, lines)
    for i, ch in enumerate(chunks, 1):
        suffix = f"\n(стр. {i}/{len(chunks)})" if len(chunks) > 1 else ""
        await send_pre_chunk(message, ch + suffix)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬇️ Экспорт CSV", callback_data="list:export_csv")]
    ])
    await message.answer(f"Всего записей: {len(items)}", reply_markup=kb)

@router.message(Command("disabled"))
@router.message(F.text.in_(["/disabled", "⛔ Отключённые"]))
async def on_disabled(message: Message) -> None:
    now = now_tz()
    async with SessionLocal() as session:
        q = dealer_filter(select(Item).order_by(Item.due_date.asc()))
        items = (await session.execute(q)).scalars().all()
    expired = [it for it in items if to_tz(it.due_date) <= now]
    if not expired:
        await message.answer("Отключённых (просроченных) нет.")
        return
    header, lines = make_table_lines_without_id(expired)
    header = "Disabled (просроченные):\n" + "-" * 40 + "\n" + header
    chunks = split_text_chunks(header, lines)
    for i, ch in enumerate(chunks, 1):
        suffix = f"\n(стр. {i}/{len(chunks)})" if len(chunks) > 1 else ""
        await send_pre_chunk(message, ch + suffix)

@router.message(Command("next"))
@router.message(F.text.in_(["/next", "⏰ Ближайшие"]))
async def on_next(message: Message) -> None:
    now = now_tz()
    end = now + timedelta(days=3)
    async with SessionLocal() as session:
        q = dealer_filter(select(Item).order_by(Item.due_date.asc()))
        all_items = (await session.execute(q)).scalars().all()
    window = [it for it in all_items if now < to_tz(it.due_date) <= end]
    if not window:
        await message.answer("Нет истечений в ближайшие 3 дня.")
        return
    header, lines = make_table_lines_without_id(window)
    header = "Ближайшие (до 3 дней):\n" + "-" * 40 + "\n" + header
    chunks = split_text_chunks(header, lines)
    for i, ch in enumerate(chunks, 1):
        suffix = f"\n(стр. {i}/{len(chunks)})" if len(chunks) > 1 else ""
        await send_pre_chunk(message, ch + suffix)

# ====== Кабинет дилера (единый бот, роль 'dealer') ======


@dealer_router.message(CommandStart())
@dealer_router.message(F.text == "/start")
async def dealer_on_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    d = await dealer_by_chat(message.from_user.id)
    name = d.title if d else "дилер"
    cmds = ", ".join(f"/{c.command}" for c in BOT_COMMANDS_DEALER if c.command not in ("start", "help"))
    await message.answer(
        f"✅ XMPLUS — кабинет дилера: {name}.\n"
        f"Вам доступны команды: {cmds}.",
        reply_markup=dealer_user_menu_kb(),
    )


@dealer_router.message(Command("help"))
@dealer_router.message(F.text == "/help")
async def dealer_on_help(message: Message, state: FSMContext) -> None:
    await state.clear()
    text = "Доступные команды:\n" + "\n".join(f"/{c.command} — {c.description}" for c in BOT_COMMANDS_DEALER)
    await message.answer(text)


@dealer_router.message(Command("list"))
@dealer_router.message(F.text.in_(["/list", "📋 Список"]))
async def dealer_on_list(message: Message, state: FSMContext) -> None:
    await state.clear()
    d = await dealer_by_chat(message.from_user.id)
    if not d:
        return
    async with SessionLocal() as session:
        q = select(Item).where(Item.dealer == d.code).order_by(Item.due_date.asc())
        items = (await session.execute(q)).scalars().all()
    if not items:
        await message.answer("Список пуст.")
        return
    header, lines = make_table_lines_without_id(items)
    chunks = split_text_chunks(header, lines)
    for i, ch in enumerate(chunks, 1):
        suffix = f"\n(стр. {i}/{len(chunks)})" if len(chunks) > 1 else ""
        await send_pre_chunk(message, ch + suffix)
    await message.answer(f"Всего записей: {len(items)}")


@dealer_router.message(Command("disabled"))
@dealer_router.message(F.text.in_(["/disabled", "⛔ Отключённые"]))
async def dealer_on_disabled(message: Message, state: FSMContext) -> None:
    await state.clear()
    d = await dealer_by_chat(message.from_user.id)
    if not d:
        return
    now = now_tz()
    async with SessionLocal() as session:
        q = select(Item).where(Item.dealer == d.code).order_by(Item.due_date.asc())
        items = (await session.execute(q)).scalars().all()
    expired = [it for it in items if to_tz(it.due_date) <= now]
    if not expired:
        await message.answer("Отключённых (просроченных) нет.")
        return
    header, lines = make_table_lines_without_id(expired)
    header = "Disabled (просроченные):\n" + "-" * 40 + "\n" + header
    chunks = split_text_chunks(header, lines)
    for i, ch in enumerate(chunks, 1):
        suffix = f"\n(стр. {i}/{len(chunks)})" if len(chunks) > 1 else ""
        await send_pre_chunk(message, ch + suffix)


@dealer_router.message(Command("next"))
@dealer_router.message(F.text.in_(["/next", "⏰ Ближайшие"]))
async def dealer_on_next(message: Message, state: FSMContext) -> None:
    await state.clear()
    d = await dealer_by_chat(message.from_user.id)
    if not d:
        return
    now = now_tz()
    end = now + timedelta(days=3)
    async with SessionLocal() as session:
        q = select(Item).where(Item.dealer == d.code).order_by(Item.due_date.asc())
        items = (await session.execute(q)).scalars().all()
    window = [it for it in items if now < to_tz(it.due_date) <= end]
    if not window:
        await message.answer("Нет истечений в ближайшие 3 дня.")
        return
    header, lines = make_table_lines_without_id(window)
    header = "Ближайшие (до 3 дней):\n" + "-" * 40 + "\n" + header
    chunks = split_text_chunks(header, lines)
    for i, ch in enumerate(chunks, 1):
        suffix = f"\n(стр. {i}/{len(chunks)})" if len(chunks) > 1 else ""
        await send_pre_chunk(message, ch + suffix)


@dealer_router.message(Command("status"))
@dealer_router.message(F.text.in_(["/status", "📊 Статус"]))
async def dealer_on_status(message: Message, state: FSMContext) -> None:
    await state.clear()
    d = await dealer_by_chat(message.from_user.id)
    if not d:
        return
    async with SessionLocal() as session:
        cnt = len((await session.execute(select(Item.id).where(Item.dealer == d.code))).all())
    await message.answer(
        f"Бот работает ✅\nДилер: {d.title}\nВаших записей: {cnt}",
    )


# ===== Редактирование имени клиента (дилер) =====


@dealer_router.message(Command("edit"))
@dealer_router.message(F.text.in_(["/edit", "✏️ Редактор"]))
async def dealer_edit_start(message: Message, state: FSMContext) -> None:
    d = await dealer_by_chat(message.from_user.id)
    if not d:
        return
    await state.clear()
    await state.set_state(DealerEditStates.waiting_search)
    await message.answer(
        "✏️ Редактор\n\nВведите USERID для поиска:",
    )


@dealer_router.message(DealerEditStates.waiting_search)
async def dealer_edit_search(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    d = await dealer_by_chat(message.from_user.id)
    if not d:
        return
    if not text:
        await message.answer("Введите USERID:")
        return
    async with SessionLocal() as session:
        if text.isdigit():
            q = select(Item).where(Item.user_id == int(text), Item.dealer == d.code)
        else:
            from sqlalchemy import or_
            q = select(Item).where(
                Item.dealer == d.code,
                or_(Item.username.ilike(f"%{text}%"), Item.note.ilike(f"%{text}%")),
            )
        items = (await session.execute(q)).scalars().all()
    if not items:
        await message.answer("Ничего не найдено. Попробуйте ещё раз или /cancel.")
        return
    if len(items) == 1:
        it = items[0]
        note = getattr(it, "note", "") or ""
        dash = "—"
        await state.update_data(dedit_item_id=it.id)
        await state.set_state(DealerEditStates.waiting_value)
        await message.answer(
            f"USERID: {it.user_id}\nUSERNAME: {it.username}\n"
            f"Клиент: {note or dash}\n\n"
            "Введите новое имя клиента:",
        )
        return
    rows = []
    for it in items[:10]:
        note = getattr(it, "note", "") or ""
        label = f"{it.user_id} | {it.username}"
        if note:
            label += f" | {note}"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"dedit:pick:{it.id}")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await state.clear()
    await message.answer(f"Найдено {len(items)}. Выберите:", reply_markup=kb)


@dealer_router.callback_query(F.data.startswith("dedit:pick:"))
async def dealer_edit_pick(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    item_id = int(cb.data.split(":")[-1])
    d = await dealer_by_chat(cb.from_user.id)
    if not d:
        return
    async with SessionLocal() as session:
        it = (await session.execute(select(Item).where(Item.id == item_id, Item.dealer == d.code))).scalars().first()
    if not it:
        await cb.message.answer("Запись не найдена.")
        return
    note = getattr(it, "note", "") or ""
    dash = "—"
    await state.set_state(DealerEditStates.waiting_value)
    await state.update_data(dedit_item_id=it.id)
    await cb.message.answer(
        f"USERID: {it.user_id}\nUSERNAME: {it.username}\n"
        f"Клиент: {note or dash}\n\n"
        "Введите новое имя клиента:",
    )


@dealer_router.message(DealerEditStates.waiting_value)
async def dealer_edit_save(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Имя не может быть пустым.")
        return
    data = await state.get_data()
    item_id = data["dedit_item_id"]
    d = await dealer_by_chat(message.from_user.id)
    if not d:
        return
    async with SessionLocal() as session:
        it = (await session.execute(select(Item).where(Item.id == item_id, Item.dealer == d.code))).scalars().first()
        if not it:
            await state.clear()
            await message.answer("Запись не найдена.")
            return
        it.note = text
        await session.commit()
    await state.clear()
    await message.answer(
        f"✅ Имя клиента обновлено: {text}",
    )


# ===== Заказ новых ключей (дилер) =====


@dealer_router.message(Command("order"))
@dealer_router.message(F.text.in_(["/order", "➕ Добавить"]))
async def dealer_order_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    d = await dealer_by_chat(message.from_user.id)
    if not d:
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="1", callback_data="dorder:n:1"),
        InlineKeyboardButton(text="2", callback_data="dorder:n:2"),
        InlineKeyboardButton(text="3", callback_data="dorder:n:3"),
    ]])
    await message.answer(
        "📦 Заказ новых ключей\n\nСколько ключей вы хотите заказать?",
        reply_markup=kb,
    )


@dealer_router.callback_query(F.data.startswith("dorder:n:"))
async def dealer_order_pick(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    try:
        n = int(cb.data.split(":")[-1])
    except Exception:
        return
    if n < 1 or n > 3:
        return
    await state.update_data(order_count=n, order_names=[], order_collected=0)
    await state.set_state(DealerOrderStates.waiting_names)
    await cb.message.answer(
        f"Введите имя клиента 1/{n}:",
    )


@dealer_router.message(DealerOrderStates.waiting_names)
async def dealer_order_collect_name(message: Message, state: FSMContext, bot: Bot) -> None:
    name = (message.text or "").strip()
    if not name:
        await message.answer("Имя не может быть пустым. Введите ещё раз:")
        return
    data = await state.get_data()
    names = data.get("order_names", [])
    names.append(name)
    collected = len(names)
    total = data["order_count"]

    if collected < total:
        await state.update_data(order_names=names)
        await message.answer(f"Введите имя клиента {collected + 1}/{total}:")
        return

    # Все имена собраны — отправляем заявку админу
    await state.clear()
    d = await dealer_by_chat(message.from_user.id)
    if not d:
        return
    names_list = "\n".join(f"  {i+1}) {n}" for i, n in enumerate(names))
    owner_chat = int(settings.OWNER_CHAT_ID) if settings.OWNER_CHAT_ID else None
    if owner_chat:
        async with SessionLocal() as _s:
            new_order = DealerOrder(
                dealer_code=d.code,
                dealer_title=d.title,
                dealer_chat_id=d.chat_id,
                names_json=json.dumps(names, ensure_ascii=False),
                fulfilled=0,
            )
            _s.add(new_order)
            await _s.commit()
            oid = str(new_order.id)
        admin_text = (
            f"📦 Заявка на новые ключи\n\n"
            f"Дилер: {d.title}\n"
            f"Количество: {total}\n"
            f"Клиенты:\n{names_list}\n"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="▶️ Выполнить", callback_data=f"oful:{oid}")],
        ])
        try:
            await bot.send_message(owner_chat, admin_text, reply_markup=kb)
        except Exception as e:
            log.warning("Failed to send order to admin: %s", e)
    await message.answer(
        f"✅ Запрос на {total} ключей отправлен администратору.\n"
        f"Клиенты:\n{names_list}\n\nОжидайте.",
    )


@dealer_router.callback_query(F.data == "dorder:no")
async def dealer_order_cancel(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await state.clear()
    await cb.message.answer("Отменено.")


# ===== Запрос дилера на продление клиента =====


@dealer_router.message(Command("cancel"))
@dealer_router.message(F.text == "/cancel")
async def dealer_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.", reply_markup=dealer_user_menu_kb())


@dealer_router.message(Command("renew"))
@dealer_router.message(F.text.in_(["/renew", "🔄 Продлить"]))
async def dealer_renew_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(DealerRenewStates.waiting_userid)
    await message.answer(
        "Продление клиента.\n"
        "Введите USERID клиента, которого хотите продлить.\n"
        "Отмена — /cancel",
    )


def _dealer_renew_comment_prompt(it: Item) -> str:
    return (
        "Клиент:\n"
        f"USERID: {it.user_id}\n"
        f"USERNAME: {it.username}\n"
        f"Текущая дата отключения: {fmt_dt_human(it.due_date)}\n\n"
        "Добавьте комментарий для администратора (например «оплатил на месяц») "
        "или отправьте «-», чтобы пропустить."
    )


@dealer_router.message(DealerRenewStates.waiting_userid)
async def dealer_renew_userid(message: Message, state: FSMContext) -> None:
    d = await dealer_by_chat(message.from_user.id)
    if not d:
        await state.clear()
        return
    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer("USERID должен быть числом. Введите ещё раз или /cancel.")
        return
    uid = int(text)
    async with SessionLocal() as session:
        q = select(Item).where(Item.dealer == d.code, Item.user_id == uid).order_by(Item.due_date.asc())
        items = (await session.execute(q)).scalars().all()
    if not items:
        await message.answer("Клиент с таким USERID не найден среди ваших. Введите ещё раз или /cancel.")
        return
    if len(items) == 1:
        it = items[0]
        await state.update_data(item_id=it.id)
        await state.set_state(DealerRenewStates.waiting_comment)
        await message.answer(_dealer_renew_comment_prompt(it))
        return
    kb = choose_by_due_kb("drenew", items)
    await message.answer("Найдено несколько записей с этим USERID. Выберите нужную:", reply_markup=kb)


@dealer_router.callback_query(F.data.startswith("drenew:choose:"))
async def dealer_renew_choose(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    try:
        item_id = int(cb.data.split(":")[-1])
    except Exception:
        return
    d = await dealer_by_chat(cb.from_user.id)
    if not d:
        return
    async with SessionLocal() as session:
        it = await session.get(Item, item_id)
    if not it or it.dealer != d.code:
        await cb.message.answer("Запись не найдена среди ваших клиентов.")
        return
    await state.update_data(item_id=it.id)
    await state.set_state(DealerRenewStates.waiting_comment)
    await cb.message.answer(_dealer_renew_comment_prompt(it))


@dealer_router.message(DealerRenewStates.waiting_comment)
async def dealer_renew_comment(message: Message, state: FSMContext, bot: Bot) -> None:
    d = await dealer_by_chat(message.from_user.id)
    if not d:
        await state.clear()
        return
    raw = (message.text or "").strip()
    comment = "" if raw in ("-", "—") else raw
    data = await state.get_data()
    item_id = data.get("item_id")
    await state.clear()
    if not item_id:
        await message.answer("Что-то пошло не так. Начните заново — /renew.")
        return
    async with SessionLocal() as session:
        it = await session.get(Item, int(item_id))
    if not it or it.dealer != d.code:
        await message.answer("Запись не найдена среди ваших клиентов.")
        return
    owner_chat = int(settings.OWNER_CHAT_ID) if settings.OWNER_CHAT_ID else None
    if owner_chat:
        admin_text = (
            "📩 Запрос на продление\n\n"
            f"Дилер: {d.title}\n"
            f"USERID: {it.user_id}\n"
            f"USERNAME: {it.username}\n"
            f"Текущая дата отключения: {fmt_dt_human(it.due_date)}\n"
            f"Комментарий: {comment if comment else '—'}"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"rreq:ok:{it.id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"rreq:no:{it.id}"),
        ]])
        try:
            await bot.send_message(owner_chat, admin_text, reply_markup=kb)
        except Exception as e:

            log.warning("Failed to send renew request to admin: %s", e)

    await message.answer(
        "✅ Запрос на продление отправлен администратору. Ожидайте подтверждения.",
    )


def _fmt_txn(t) -> str:
    kind_ru = {
        "renewal": "продление",
        "admin_add": "начисление (админ)",
        "admin_sub": "списание (админ)",
        "payment": "оплата",
    }.get(t.kind, t.kind)
    sign = "+" if t.amount >= 0 else "−"
    when = fmt_dt_human(t.created_at)
    line = f"{sign}${abs(t.amount):g} — {kind_ru} — {when}"
    if t.comment:
        line += f" — {t.comment}"
    return line


@dealer_router.message(Command("balance"))
@dealer_router.message(F.text.in_(["/balance", "💰 Баланс"]))
async def dealer_on_balance(message: Message, state: FSMContext) -> None:
    await state.clear()
    d = await dealer_by_chat(message.from_user.id)
    if not d:
        return
    async with SessionLocal() as session:
        txns = (await session.execute(
            select(BalanceTxn).where(BalanceTxn.dealer_code == d.code)
            .order_by(BalanceTxn.id.desc()).limit(10)
        )).scalars().all()
    bal = d.balance or 0.0
    lines = [f"💰 Ваш баланс (долг): ${bal:g}", ""]
    if txns:
        lines.append("Последние операции:")
        for t in txns:
            lines.append(_fmt_txn(t))
    else:
        lines.append("Операций пока нет.")
    await message.answer("\n".join(lines))


# ===== Оплата (дилер) =====


async def _dealer_show_methods(target, d: Dealer) -> None:
    methods = await list_payment_methods(active_only=True)
    bal = d.balance or 0.0
    if not methods:
        await target.answer(
            f"💳 Оплата\nВаш долг: ${bal:g}\n\n"
            "Методы оплаты пока не настроены. Обратитесь к администратору.",
        )
        return
    rows = [[InlineKeyboardButton(text=m.name, callback_data=f"dpay:m:{m.id}")] for m in methods]
    await target.answer(
        f"💳 Оплата\nВаш долг: ${bal:g}\n\nВыберите метод оплаты:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@dealer_router.message(Command("pay"))
@dealer_router.message(F.text.in_(["/pay", "💳 Оплата"]))
async def dealer_on_pay(message: Message, state: FSMContext) -> None:
    await state.clear()
    d = await dealer_by_chat(message.from_user.id)
    if not d:
        return
    await _dealer_show_methods(message, d)


@dealer_router.callback_query(F.data == "dpay:home")
async def dealer_pay_home(cb: CallbackQuery) -> None:
    await cb.answer()
    d = await dealer_by_chat(cb.from_user.id)
    if not d:
        return
    await _dealer_show_methods(cb.message, d)


@dealer_router.callback_query(F.data.startswith("dpay:m:"))
async def dealer_pay_method(cb: CallbackQuery) -> None:
    await cb.answer()
    try:
        pm_id = int(cb.data.split(":")[-1])
    except Exception:
        return
    m = await get_payment_method(pm_id)
    if not m or not m.active:
        await cb.message.answer("Метод недоступен.")
        return
    variants = await list_payment_variants(pm_id, active_only=True)
    if not variants:
        await cb.message.answer(
            f"💳 {m.name}\n\nДля этого метода пока не настроены виды оплаты. Обратитесь к администратору.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="◀ К методам", callback_data="dpay:home"),
            ]]),
        )
        return
    rows = [[InlineKeyboardButton(text=v.name, callback_data=f"dpay:v:{v.id}")] for v in variants]
    rows.append([InlineKeyboardButton(text="◀ К методам", callback_data="dpay:home")])
    await cb.message.answer(
        f"💳 {m.name}\n\nВыберите вид оплаты (сеть/способ):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@dealer_router.callback_query(F.data.startswith("dpay:v:"))
async def dealer_pay_variant(cb: CallbackQuery) -> None:
    await cb.answer()
    try:
        v_id = int(cb.data.split(":")[-1])
    except Exception:
        return
    v = await get_payment_variant(v_id)
    if not v or not v.active:
        await cb.message.answer("Вид оплаты недоступен.")
        return
    m = await get_payment_method(v.method_id)
    if not m or not m.active:
        await cb.message.answer("Метод недоступен.")
        return
    req = (v.requisites or "").strip() or "(реквизиты не указаны, обратитесь к администратору)"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Я оплатил этим способом", callback_data=f"dpay:pay:{v.id}")],
        [InlineKeyboardButton(text="◀ К видам", callback_data=f"dpay:m:{m.id}")],
    ])
    await cb.message.answer(
        f"💳 {m.name} → {v.name}\n\nРеквизиты для оплаты:\n{req}",
        reply_markup=kb,
    )


@dealer_router.callback_query(F.data.startswith("dpay:pay:"))
async def dealer_pay_start(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    try:
        v_id = int(cb.data.split(":")[-1])
    except Exception:
        return
    v = await get_payment_variant(v_id)
    if not v or not v.active:
        await cb.message.answer("Вид оплаты недоступен.")
        return
    m = await get_payment_method(v.method_id)
    if not m or not m.active:
        await cb.message.answer("Метод недоступен.")
        return
    await state.clear()
    await state.update_data(pay_method=m.name, pay_variant=v.name)
    await state.set_state(DealerPayStates.waiting_amount)
    await cb.message.answer(
        f"Метод: {m.name} → {v.name}\nВведите сумму в $, которую вы перечислили. Отмена — /cancel",
    )


@dealer_router.message(DealerPayStates.waiting_amount)
async def dealer_pay_amount(message: Message, state: FSMContext, bot: Bot) -> None:
    d = await dealer_by_chat(message.from_user.id)
    if not d:
        await state.clear()
        return
    amount = _parse_amount(message.text or "")
    if amount is None:
        await message.answer("Введите положительное число (например 5). Ещё раз или /cancel.")
        return
    data = await state.get_data()
    method = data.get("pay_method")
    variant = data.get("pay_variant")
    await state.clear()
    if not method:
        await message.answer("Что-то пошло не так. Начните заново — /pay.")
        return
    async with SessionLocal() as session:
        pay = Payment(dealer_code=d.code, method=method, variant=variant, amount=amount, status="pending")
        session.add(pay)
        await session.commit()
        pay_id = pay.id
    owner_chat = int(settings.OWNER_CHAT_ID) if settings.OWNER_CHAT_ID else None
    if owner_chat:
        admin_text = (
            "💵 Заявка на оплату\n\n"
            f"Дилер: {d.title}\n"
            f"Метод: {method}\n"
            f"Вид: {variant or '—'}\n"
            f"Сумма: ${amount:g}\n"
            f"Текущий долг дилера: ${(d.balance or 0.0):g}"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"pay:ok:{pay_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"pay:no:{pay_id}"),
        ]])
        try:
            await bot.send_message(owner_chat, admin_text, reply_markup=kb)
        except Exception as e:

            log.warning("Failed to send payment request to admin: %s", e)

    await message.answer(
        f"✅ Заявка на оплату отправлена администратору.\n"
        f"Метод: {method} → {variant or '—'}\nСумма: ${amount:g}\n"
        "Долг уменьшится после подтверждения.",
    )


@dealer_router.message()
async def dealer_fallback(message: Message) -> None:
    cmds = ", ".join(f"/{c.command}" for c in BOT_COMMANDS_DEALER if c.command not in ("start", "help"))
    await message.answer(
        f"Доступные команды: {cmds}.",
    )


# ====== Гости (нет доступа) ======

@guest_router.message()
async def guest_message(message: Message) -> None:
    await message.answer("⛔ Нет доступа. Бот доступен только администратору и дилерам.")


@guest_router.callback_query()
async def guest_callback(cb: CallbackQuery) -> None:
    await cb.answer("Нет доступа", show_alert=True)


# ====== Обработка заявок дилеров на продление (админ) ======

@router.callback_query(F.data.startswith("rreq:ok:"))
async def renew_request_approve(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    try:
        item_id = int(cb.data.split(":")[-1])
    except Exception:
        return
    async with SessionLocal() as session:
        it = await session.get(Item, item_id)
    if not it:
        await cb.message.answer("Запись не найдена (возможно, удалена).")
        return
    await state.clear()
    await state.update_data(item_id=it.id, user_id=it.user_id, username=it.username, old_due=fmt_dt_human(it.due_date))
    await state.set_state(RenewStates.waiting_new_due)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Подставить текущую", callback_data=f"renew:prefill:current:{it.id}")],
        [InlineKeyboardButton(text="Подставить +1 месяц", callback_data=f"renew:prefill:plus1m:{it.id}")],
    ])
    await cb.message.answer(
        "✅ Запрос одобрен. Продление:\n"
        f"USERID: {it.user_id}\n"
        f"USERNAME: {it.username}\n"
        f"Текущая дата отключения: {fmt_dt_human(it.due_date)}",
        reply_markup=kb,
    )
    await cb.message.answer("Отправьте новую дату в формате:\nYYYY-MM-DD HH:MM:SS")


@router.callback_query(F.data.startswith("rreq:no:"))
async def renew_request_reject(cb: CallbackQuery, bot: Bot) -> None:
    await cb.answer()
    try:
        item_id = int(cb.data.split(":")[-1])
    except Exception:
        return
    async with SessionLocal() as session:
        it = await session.get(Item, item_id)
    if not it:
        await cb.message.answer("Запись не найдена (возможно, удалена).")
        return
    dealer_code = it.dealer
    user_id = it.user_id
    username = it.username
    d = await get_dealer(dealer_code) if dealer_code and dealer_code != MAIN_CODE else None
    if d and d.chat_id is not None:
        try:
            await bot.send_message(
                d.chat_id,
                "❌ Запрос на продление отклонён администратором.\n"
                f"Клиент: USERID={user_id}, USERNAME={username}",
            )
        except Exception as e:

            await _notify_fail(bot, f"дилер {d.title}", e)

    await cb.message.answer(
        f"Запрос отклонён. USERID={user_id}, USERNAME={username}.",
    )


# ====== Выполнение заказа дилера (админ) ======


@router.callback_query(F.data.startswith("oful:") & ~F.data.in_(["oful:ok", "oful:cancel"]))
async def order_fulfill_start(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    oid = cb.data.split(":", 1)[1]
    order = await _get_order(oid)
    if not order:
        await cb.message.answer("Заказ не найден или уже выполнен.")
        return
    idx = order["fulfilled"]
    names = order["names"]
    if idx >= len(names):
        await cb.message.answer("Все ключи из этого заказа уже выполнены.")
        return
    client_name = names[idx]
    total = len(names)
    await state.clear()
    await state.update_data(order_id=oid, key_index=idx)
    await state.set_state(OrderFulfillStates.waiting_user_id)
    await cb.message.answer(
        f"🔑 Ключ {idx + 1}/{total} — клиент: {client_name}\n\n"
        f"Введите USERID:",
    )


@router.message(OrderFulfillStates.waiting_user_id)
async def oful_user_id(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer("USERID должен быть числом. Попробуйте ещё раз или /cancel.")
        return
    await state.update_data(uid=int(text))
    await state.set_state(OrderFulfillStates.waiting_username)
    await message.answer("Введите USERNAME:")


@router.message(OrderFulfillStates.waiting_username)
async def oful_username(message: Message, state: FSMContext) -> None:
    uname = (message.text or "").strip()
    if not uname:
        await message.answer("USERNAME не может быть пустым. Попробуйте ещё раз:")
        return
    await state.update_data(uname=uname)
    await state.set_state(OrderFulfillStates.waiting_due)
    await message.answer(
        "Введите дату отключения (YYYY-MM-DD HH:MM:SS):\n"
        "Пример: 2025-10-20 15:35:43",
    )


@router.message(OrderFulfillStates.waiting_due)
async def oful_due(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    dt = parse_datetime_human(text)
    if not dt:
        await message.answer("Неверный формат. Используйте YYYY-MM-DD HH:MM:SS. Ещё раз:")
        return
    await state.update_data(due=dt.isoformat())
    await state.set_state(OrderFulfillStates.waiting_key_code)
    await message.answer("Введите код ключа (текст для отправки дилеру):")


@router.message(OrderFulfillStates.waiting_key_code)
async def oful_key_code(message: Message, state: FSMContext) -> None:
    key_code = (message.text or "").strip()
    if not key_code:
        await message.answer("Код ключа не может быть пустым. Ещё раз:")
        return
    await state.update_data(key_code=key_code)
    data = await state.get_data()
    oid = data["order_id"]
    order = await _get_order(oid)
    if not order:
        await state.clear()
        await message.answer("Заказ не найден.")
        return
    idx = data["key_index"]
    client_name = order["names"][idx]
    dt = datetime.fromisoformat(data["due"])
    price = await get_price()
    await state.set_state(OrderFulfillStates.waiting_confirm)
    await message.answer(
        f"📋 Подтверждение:\n\n"
        f"Дилер: {order['dealer_title']}\n"
        f"Клиент: {client_name}\n"
        f"USERID: {data['uid']}\n"
        f"USERNAME: {data['uname']}\n"
        f"DUE: {fmt_dt_human(dt)}\n"
        f"Код ключа: <code>{html.escape(data['key_code'])}</code>\n"
        f"Долг: +${price:.2f}\n",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Подтвердить", callback_data="oful:ok"),
                InlineKeyboardButton(text="❌ Отмена", callback_data="oful:cancel"),
            ],
        ]),
    )


@router.callback_query(F.data == "oful:ok", OrderFulfillStates.waiting_confirm)
async def oful_confirm(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await cb.answer()
    data = await state.get_data()
    oid = data["order_id"]
    order = await _get_order(oid)
    if not order:
        await state.clear()
        await cb.message.answer("Заказ не найден.")
        return

    idx = data["key_index"]
    client_name = order["names"][idx]
    uid = data["uid"]
    uname = data["uname"]
    dt = datetime.fromisoformat(data["due"])
    key_code = data["key_code"]
    dealer_code = order["dealer_code"]
    dealer_chat_id = order["dealer_chat_id"]
    price = await get_price()

    # 1) Добавляем клиента в базу (на имя дилера, с заметкой)
    async with SessionLocal() as session:
        item = Item(
            user_id=uid,
            username=uname,
            due_date=dt,
            dealer=dealer_code,
            note=client_name,
            chat_id=cb.message.chat.id,
        )
        session.add(item)
        await session.commit()

    # 2) Добавляем долг дилеру
    new_bal = await apply_balance_change(
        dealer_code, price, "order", f"Ключ для {client_name} (USERID={uid})"
    )

    # 3) Отправляем дилеру ключ
    if dealer_chat_id:
        bal_abs = f"${abs(new_bal):.2f}" if new_bal is not None else "?"
        dealer_text = (
            f"🔑 Ваш ключ готов!\n\n"
            f"Клиент: {client_name}\n"
            f"USERID: {uid}\n"
            f"USERNAME: {uname}\n"
            f"Действует до: {fmt_dt_human(dt)}\n\n"
            f"Код ключа (нажмите чтобы скопировать):\n"
            f"<code>{html.escape(key_code)}</code>\n\n"
            f"Начислено: ${price:.2f}\n"
            f"Ваш долг: {bal_abs}"
        )
        try:
            await bot.send_message(dealer_chat_id, dealer_text, parse_mode="HTML")
        except Exception:
            await cb.message.answer(f"⚠️ Не удалось отправить ключ дилеру (chat_id={dealer_chat_id}).")

    # 4) Обновляем счётчик
    await _update_order_fulfilled(oid, idx + 1)
    order["fulfilled"] = idx + 1  # update local copy
    await state.clear()

    bal_str = f"${new_bal:.2f}" if new_bal is not None else "?"
    await cb.message.answer(
        f"✅ Ключ {idx + 1}/{len(order['names'])} выполнен!\n"
        f"Клиент: {client_name}, USERID={uid}\n"
        f"Баланс дилера: {bal_str}",
    )

    # Если есть ещё ключи — предлагаем продолжить
    if order["fulfilled"] < len(order["names"]):
        next_name = order["names"][order["fulfilled"]]
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"▶️ Следующий: {next_name}", callback_data=f"oful:{oid}")],
        ])
        await cb.message.answer(
            f"Осталось ключей: {len(order['names']) - order['fulfilled']}",
            reply_markup=kb,
        )
    else:
        await _delete_order(oid)
        await cb.message.answer("🎉 Все ключи из заказа выполнены!")


@router.callback_query(F.data == "oful:cancel", OrderFulfillStates.waiting_confirm)
async def oful_cancel(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await state.clear()
    await cb.message.answer("Отменено. Заказ остаётся в очереди.")


