from __future__ import annotations

from datetime import datetime, timezone, timedelta
import csv, io, html, os, re, calendar, zipfile, shutil
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
from aiogram.fsm.context import FSMContext

from sqlalchemy import select, delete, update

from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest

from app.db import (
    SessionLocal, engine, Item, Dealer, BalanceTxn, PaymentMethod, PaymentVariant, Payment,
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

# Хранилище заказов дилеров (order_id → dict с данными)
_pending_orders: dict[str, dict] = {}
_order_counter = 0

def _next_order_id() -> str:
    global _order_counter
    _order_counter += 1
    return str(_order_counter)

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
            [KeyboardButton(text="➕ Добавить"), KeyboardButton(text="🔄 Продлить")],
            [KeyboardButton(text="🗑 Удалить"), KeyboardButton(text="📋 Список")],
            [KeyboardButton(text="⏰ Ближайшие"), KeyboardButton(text="⛔ Отключённые")],
            [KeyboardButton(text="👥 Дилеры"), KeyboardButton(text="💰 Баланс")],
            [KeyboardButton(text="💳 Оплата"), KeyboardButton(text="🌐 Часовой пояс")],
            [KeyboardButton(text="📊 Статус"), KeyboardButton(text="💾 Бэкап")],
            [KeyboardButton(text="ℹ️ Помощь"), KeyboardButton(text="❌ Отмена"), KeyboardButton(text="👁 Скрыть")],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выберите команду…",
        selective=True,
    )

def confirm_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="✅ Подтвердить"), KeyboardButton(text="❌ Отмена")]],
        resize_keyboard=True,
        selective=True,
    )

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
    has_notes = any(getattr(it, "note", None) for it in items)
    if has_notes:
        header = f"{'USERID'.rjust(UID_W)} | {'USERNAME'.ljust(UNAME_W)} | {'КЛИЕНТ'.ljust(NOTE_W)} | DUE DATE"
    else:
        header = f"{'USERID'.rjust(UID_W)} | {'USERNAME'.ljust(UNAME_W)} | DUE DATE"
    rows: list[str] = []
    for it in items:
        uid = str(it.user_id).rjust(UID_W)
        uname = _trunc(it.username, UNAME_W).ljust(UNAME_W)
        due = fmt_dt_human(it.due_date)
        if has_notes:
            note = _trunc(getattr(it, "note", "") or "", NOTE_W).ljust(NOTE_W)
            rows.append(f"{uid} | {uname} | {note} | {due}")
        else:
            rows.append(f"{uid} | {uname} | {due}")
    return header, rows

def send_pre_chunk(message: Message, text: str):
    return message.answer(f"<pre>{html.escape(text, quote=False)}</pre>", parse_mode="HTML")

def dealer_filter(query):
    if is_dealer_mode():
        return query.where(Item.dealer == settings.DEALER_NAME)
    return query

def ensure_allowed_user(message: Message) -> bool:
    if not is_dealer_mode():
        return True
    if settings.OWNER_CHAT_ID and str(message.from_user.id) != str(settings.OWNER_CHAT_ID):
        return False
    return True

def ensure_admin_only():
    return "Эта команда недоступна в вашем боте. Обратитесь к администратору."

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
    if not ensure_allowed_user(message):
        return
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
    if not ensure_allowed_user(message):
        return
    commands = BOT_COMMANDS_DEALER if is_dealer_mode() else BOT_COMMANDS_ADMIN
    text = "Доступные команды:\n" + "\n".join([f"/{c.command} — {c.description}" for c in commands])
    await message.answer(text, reply_markup=main_menu_kb())

@router.message(Command("menu"))
@router.message(F.text == "/menu")
async def show_menu(message: Message) -> None:
    if not ensure_allowed_user(message):
        return
    await message.answer("Клавиатура показана.", reply_markup=main_menu_kb())

@router.message(Command("hide"))
@router.message(F.text.in_(["/hide", "👁 Скрыть"]))
async def hide_menu(message: Message) -> None:
    if not ensure_allowed_user(message):
        return
    await message.answer("Клавиатура скрыта.", reply_markup=ReplyKeyboardRemove())

@router.message(Command("status"))
@router.message(F.text.in_(["/status", "📊 Статус"]))
async def on_status(message: Message) -> None:
    if not ensure_allowed_user(message):
        return
    async with SessionLocal() as session:
        q = dealer_filter(select(Item))
        total = (await session.execute(q)).scalars().unique().all()
    role = "dealer" if is_dealer_mode() else "admin"
    who = f" ({settings.DEALER_NAME})" if is_dealer_mode() else ""
    await message.answer(
        f"Бот работает ✅\nРежим: {role}{who}\nВ базе записей (в пределах вашей видимости): {len(total)}\n"
        f"ACTIVE_TZ: {get_active_timezone_name()} (UTC{tz_offset_str()})",
        reply_markup=main_menu_kb(),
    )

# ==== Таймзона ====

def tz_switch_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="GMT+5 • Ashgabat", callback_data="tz:set:Asia/Ashgabat"),
            InlineKeyboardButton(text="GMT+8 • Singapore", callback_data="tz:set:Asia/Singapore"),
        ]
    ])

@router.message(Command("timezone"))
@router.message(F.text.in_(["/timezone", "🌐 Часовой пояс"]))
async def show_timezone(message: Message) -> None:
    if not ensure_allowed_user(message):
        return
    if is_dealer_mode():
        await message.answer(ensure_admin_only(), reply_markup=main_menu_kb())
        return
    local_now = now_tz()
    utc_now = datetime.now(timezone.utc)
    text = (
        f"Активный часовой пояс: {get_active_timezone_name()} (UTC{tz_offset_str()})\n"
        f"Локальное время: {local_now.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
        f"UTC:            {utc_now.strftime('%Y-%m-%d %H:%M:%S UTC')}\n\n"
        "Переключить:"
    )
    await message.answer(text, reply_markup=tz_switch_kb())

@router.callback_query(F.data.startswith("tz:set:"))
async def tz_set(cb: CallbackQuery) -> None:
    if is_dealer_mode():
        await cb.answer("Недоступно", show_alert=False)
        return
    await cb.answer()
    tz_name = cb.data.split(":", 2)[-1]
    ok = set_active_timezone_name(tz_name)
    if ok:
        await cb.message.answer(f"✅ Часовой пояс установлен: {tz_name} (UTC{tz_offset_str()})")
    else:
        await cb.message.answer("❌ Не удалось установить часовой пояс. Проверьте логи.")

# ==== Добавление (только админ) ====

class AddStates(StatesGroup):
    waiting_user_id = State()
    waiting_username = State()
    waiting_duedatetime = State()
    waiting_note = State()

@router.message(Command("cancel"))
@router.message(F.text.in_(["/cancel", "❌ Отмена"]))
async def on_cancel(message: Message, state: FSMContext) -> None:
    if not ensure_allowed_user(message):
        return
    if is_dealer_mode():
        await message.answer(ensure_admin_only(), reply_markup=main_menu_kb())
        return
    await state.clear()
    await message.answer("Отменено.", reply_markup=main_menu_kb())

@router.message(Command("add"))
@router.message(F.text.in_(["/add", "➕ Добавить"]))
async def add_start(message: Message, state: FSMContext) -> None:
    if not ensure_allowed_user(message):
        return
    if is_dealer_mode():
        await message.answer(ensure_admin_only(), reply_markup=main_menu_kb())
        return
    await state.clear()
    await state.set_state(AddStates.waiting_user_id)
    await message.answer("Шаг 1/4. Введите USER ID (число):", reply_markup=main_menu_kb())

@router.message(AddStates.waiting_user_id)
async def add_user_id(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer("USER ID должен быть числом. Попробуйте ещё раз или /cancel.", reply_markup=main_menu_kb())
        return
    await state.update_data(user_id=int(text))
    await state.set_state(AddStates.waiting_username)
    await message.answer("Шаг 2/4. Введите USERNAME (например, XmADMIN):", reply_markup=main_menu_kb())

@router.message(AddStates.waiting_username)
async def add_username(message: Message, state: FSMContext) -> None:
    username = (message.text or "").strip()
    if not username:
        await message.answer("USERNAME не может быть пустым. Попробуйте ещё раз или /cancel.", reply_markup=main_menu_kb())
        return
    await state.update_data(username=username)
    await state.set_state(AddStates.waiting_duedatetime)
    await message.answer(
        "Шаг 3/4. Введите дату и время отключения строго в формате:\n"
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
        reply_markup=main_menu_kb(),
    )

# ==== Продление (/renew) — только админ ====

class RenewStates(StatesGroup):
    waiting_userid = State()
    waiting_new_due = State()
    waiting_confirm = State()

def add_months(dt: datetime, months: int = 1) -> datetime:
    y = dt.year + (dt.month - 1 + months) // 12
    m = (dt.month - 1 + months) % 12 + 1
    last_day = calendar.monthrange(y, m)[1]
    d = min(dt.day, last_day)
    return dt.replace(year=y, month=m, day=d)

def confirm_with_edit_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить вручную", callback_data="renew:edit")],
    ])

@router.message(Command("renew"))
@router.message(F.text.in_(["/renew", "🔄 Продлить"]))
async def renew_start(message: Message, state: FSMContext) -> None:
    if not ensure_allowed_user(message):
        return
    if is_dealer_mode():
        await message.answer(ensure_admin_only(), reply_markup=main_menu_kb())
        return
    await state.clear()
    await state.set_state(RenewStates.waiting_userid)
    await message.answer("Укажи USERID клиента, которого нужно продлить:", reply_markup=main_menu_kb())

@router.message(RenewStates.waiting_userid)
async def renew_find_by_userid(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer("USERID должен быть числом. Введите ещё раз или /cancel.", reply_markup=main_menu_kb())
        return
    uid = int(text)
    async with SessionLocal() as session:
        result = await session.execute(select(Item).where(Item.user_id == uid).order_by(Item.due_date.asc()))
        items = result.scalars().all()
    if not items:
        await message.answer("Записей с таким USERID не найдено. Проверьте число или /cancel.", reply_markup=main_menu_kb())
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
        await message.answer("Отправьте новую дату в формате:\nYYYY-MM-DD HH:MM:SS", reply_markup=main_menu_kb())
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
        await cb.message.answer("Запись не найдена. Попробуйте снова /renew.", reply_markup=main_menu_kb())
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
    await cb.message.answer("Отправьте новую дату в формате:\nYYYY-MM-DD HH:MM:SS", reply_markup=main_menu_kb())

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
        reply_markup=confirm_kb(),
    )
    # Кнопка для возврата к ручному вводу (если хотите поправить дату)
    await cb.message.answer("Хотите поправить дату вручную? Нажмите кнопку ниже и введите новую дату:", reply_markup=confirm_with_edit_kb())

@router.callback_query(F.data == "renew:edit")
async def renew_edit(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    data = await state.get_data()
    suggested = data.get("new_due")
    await state.set_state(RenewStates.waiting_new_due)
    if suggested:
        await cb.message.answer(
            "Отправьте новую дату в формате:\nYYYY-MM-DD HH:MM:SS\n"
            f"Подсказка: {suggested}",
            reply_markup=main_menu_kb(),
        )
    else:
        await cb.message.answer("Отправьте новую дату в формате:\nYYYY-MM-DD HH:MM:SS", reply_markup=main_menu_kb())

@router.message(RenewStates.waiting_new_due)
async def renew_get_new_due(message: Message, state: FSMContext) -> None:
    s = (message.text or "").strip()
    dt = parse_datetime_human(s)
    if not dt:
        await message.answer("Неверный формат даты. Используйте YYYY-MM-DD HH:MM:SS.\nПопробуйте ещё раз или /cancel.", reply_markup=main_menu_kb())
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
        reply_markup=confirm_kb(),
    )
    await message.answer("Если хотите скорректировать ещё раз, нажмите ниже:", reply_markup=confirm_with_edit_kb())

@router.message(RenewStates.waiting_confirm)
async def renew_confirm(message: Message, state: FSMContext, bot: Bot) -> None:
    text = (message.text or "").strip().lower()
    if text not in ("✅ подтвердить", "подтвердить", "да", "ok", "ок"):
        await state.clear()
        await message.answer("Отменено.", reply_markup=main_menu_kb())
        return
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
            await message.answer("Запись не найдена.", reply_markup=main_menu_kb())
            return
        dt = parse_datetime_human(new_due_str)
        if not dt:
            await state.clear()
            await message.answer("Ошибка при парсинге даты. Операция отменена.", reply_markup=main_menu_kb())
            return
        item.due_date = dt
        item.notified_count = 0
        item.last_notified_at = None
        dealer_code = item.dealer
        item_user_id = item.user_id
        item_username = item.username
        await session.commit()
    await state.clear()
    await message.answer(
        f"✅ Продлено: USERID={data['user_id']}, USERNAME={data['username']}\nНовая дата DUE={new_due_str}",
        reply_markup=main_menu_kb(),
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
            except Exception:
                pass

# ==== Удаление — только админ ====

class DeleteStates(StatesGroup):
    waiting_userid = State()
    waiting_confirm = State()

@router.message(Command("delete"))
@router.message(F.text.in_(["/delete", "🗑 Удалить"]))
async def delete_start(message: Message, state: FSMContext) -> None:
    if not ensure_allowed_user(message):
        return
    if is_dealer_mode():
        await message.answer(ensure_admin_only(), reply_markup=main_menu_kb())
        return
    await state.clear()
    await state.set_state(DeleteStates.waiting_userid)
    await message.answer(
        "Укажи USERID клиента, которого нужно удалить.\n"
        "Если по USERID несколько записей — предложу выбрать по дате или удалить все сразу.",
        reply_markup=main_menu_kb(),
    )

@router.message(DeleteStates.waiting_userid)
async def delete_by_userid(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer("USERID должен быть числом. Введите ещё раз или /cancel.", reply_markup=main_menu_kb())
        return
    uid = int(text)
    async with SessionLocal() as session:
        result = await session.execute(select(Item).where(Item.user_id == uid).order_by(Item.due_date.asc()))
        items = result.scalars().all()
    if not items:
        await message.answer("По этому USERID записей нет. Проверьте число или /cancel.", reply_markup=main_menu_kb())
        return
    if len(items) == 1:
        it = items[0]
        preview = f"USERID={it.user_id}, USERNAME={it.username}, DUE={fmt_dt_human(it.due_date)}"
        await state.update_data(action="one", item_id=it.id, user_id=it.user_id)
        await state.set_state(DeleteStates.waiting_confirm)
        await message.answer("Удалить запись?\n" + preview, reply_markup=confirm_kb())
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
        await cb.message.answer("Запись не найдена. Попробуйте снова /delete.", reply_markup=main_menu_kb())
        return
    preview = f"USERID={it.user_id}, USERNAME={it.username}, DUE={fmt_dt_human(it.due_date)}"
    await state.update_data(action="one", item_id=it.id, user_id=it.user_id)
    await state.set_state(DeleteStates.waiting_confirm)
    await cb.message.answer("Удалить запись?\n" + preview, reply_markup=confirm_kb())

@router.callback_query(F.data.startswith("delete:all:"))
async def delete_choose_all(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    try:
        uid = int(cb.data.split(":")[-1])
    except Exception:
        return
    await state.update_data(action="all", user_id=uid)
    await state.set_state(DeleteStates.waiting_confirm)
    await cb.message.answer(f"Удалить ВСЕ записи для USERID={uid}?", reply_markup=confirm_kb())

@router.message(DeleteStates.waiting_confirm)
async def delete_confirm(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip().lower()
    if text not in ("✅ подтвердить", "подтвердить", "да", "ok", "ок"):
        await state.clear()
        await message.answer("Отменено.", reply_markup=main_menu_kb())
        return
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
    await message.answer(msg, reply_markup=main_menu_kb())

@router.callback_query(F.data == "list:export_csv")
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

class EditStates(StatesGroup):
    waiting_search = State()
    waiting_pick = State()
    waiting_value = State()


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


@router.message(Command("edit"))
@router.message(F.text.in_(["/edit"]))
async def edit_start(message: Message, state: FSMContext) -> None:
    if not ensure_allowed_user(message):
        return
    if is_dealer_mode():
        await message.answer(ensure_admin_only(), reply_markup=main_menu_kb())
        return
    await state.clear()
    await state.set_state(EditStates.waiting_search)
    await message.answer(
        "\u270f\ufe0f \u0420\u0435\u0434\u0430\u043a\u0442\u043e\u0440 \u043a\u043b\u044e\u0447\u0435\u0439\n\n"
        "\u0412\u0432\u0435\u0434\u0438\u0442\u0435 USERID \u0438\u043b\u0438 \u0438\u043c\u044f \u043a\u043b\u0438\u0435\u043d\u0442\u0430 \u0434\u043b\u044f \u043f\u043e\u0438\u0441\u043a\u0430:",
        reply_markup=main_menu_kb(),
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
            q = select(Item).where(Item.note.ilike(f"%{text}%"))
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
        await cb.message.answer("\u0417\u0430\u043f\u0438\u0441\u044c \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d\u0430.", reply_markup=main_menu_kb())
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
            await message.answer("\u0417\u0430\u043f\u0438\u0441\u044c \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d\u0430.", reply_markup=main_menu_kb())
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
    await cb.message.answer("\u0413\u043e\u0442\u043e\u0432\u043e.", reply_markup=main_menu_kb())


# ==== Списки ====

@router.message(Command("list"))
@router.message(F.text.in_(["/list", "📋 Список"]))
async def on_list(message: Message) -> None:
    if not ensure_allowed_user(message):
        return
    async with SessionLocal() as session:
        q = dealer_filter(select(Item).order_by(Item.due_date.asc()))
        items = (await session.execute(q)).scalars().all()
    if not items:
        await message.answer("Список пуст.", reply_markup=main_menu_kb())
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
    if not ensure_allowed_user(message):
        return
    now = now_tz()
    async with SessionLocal() as session:
        q = dealer_filter(select(Item).order_by(Item.due_date.asc()))
        items = (await session.execute(q)).scalars().all()
    expired = [it for it in items if to_tz(it.due_date) <= now]
    if not expired:
        await message.answer("Отключённых (просроченных) нет.", reply_markup=main_menu_kb())
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
    if not ensure_allowed_user(message):
        return
    now = now_tz()
    end = now + timedelta(days=3)
    async with SessionLocal() as session:
        q = dealer_filter(select(Item).order_by(Item.due_date.asc()))
        all_items = (await session.execute(q)).scalars().all()
    window = [it for it in all_items if now < to_tz(it.due_date) <= end]
    if not window:
        await message.answer("Нет истечений в ближайшие 3 дня.", reply_markup=main_menu_kb())
        return
    header, lines = make_table_lines_without_id(window)
    header = "Ближайшие (до 3 дней):\n" + "-" * 40 + "\n" + header
    chunks = split_text_chunks(header, lines)
    for i, ch in enumerate(chunks, 1):
        suffix = f"\n(стр. {i}/{len(chunks)})" if len(chunks) > 1 else ""
        await send_pre_chunk(message, ch + suffix)

# ==== Раздел "Диллеры" (только админ) ====

# Спец-код и название для записей без дилера
MAIN_CODE = "main"
MAIN_TITLE = "Без дилера"

# Допустимый код дилера: латиница/цифры/подчёркивание, 2-32 символа
DEALER_CODE_RE = re.compile(r"^[a-z0-9_]{2,32}$")


async def list_dealers() -> list[Dealer]:
    """Все дилеры из БД (без main), отсортированы по названию."""
    async with SessionLocal() as session:
        return (
            await session.execute(select(Dealer).order_by(Dealer.title.asc()))
        ).scalars().all()


async def get_dealer(code: str) -> Dealer | None:
    """Дилер по коду; None для пустого или несуществующего кода."""
    code = (code or "").strip().lower()
    if not code:
        return None
    async with SessionLocal() as session:
        return (
            await session.execute(select(Dealer).where(Dealer.code == code))
        ).scalars().first()


async def dealers_menu_kb() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for d in await list_dealers():
        rows.append([
            InlineKeyboardButton(text=f"👁 {d.title}", callback_data=f"dealers:view:{d.code}"),
            InlineKeyboardButton(text=f"⬇️ CSV {d.title}", callback_data=f"dealers:export:{d.code}"),
        ])
    # Спец-раздел «Без дилера»
    rows.append([
        InlineKeyboardButton(text=f"👁 {MAIN_TITLE}", callback_data=f"dealers:view:{MAIN_CODE}"),
        InlineKeyboardButton(text=f"⬇️ CSV {MAIN_TITLE}", callback_data=f"dealers:export:{MAIN_CODE}"),
    ])
    # Действия с дилерами
    rows.append([
        InlineKeyboardButton(text="✉️ Написать дилеру", callback_data="dealers:msg:start"),
        InlineKeyboardButton(text="📢 Рассылка всем", callback_data="dealers:broadcast:start"),
    ])
    rows.append([
        InlineKeyboardButton(text="🔑 Отправить ключ дилеру", callback_data="dkey:start"),
    ])
    rows.append([
        InlineKeyboardButton(text="➕ Добавить дилера", callback_data="dealers:add:start"),
        InlineKeyboardButton(text="🗑 Удалить дилера", callback_data="dealers:del:start"),
    ])
    rows.append([
        InlineKeyboardButton(text="📝 Назначить по списку USERID → дилер", callback_data="dealers:assign:start"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def dealers_counts_text() -> str:
    dealers = await list_dealers()
    known = {d.code for d in dealers}
    async with SessionLocal() as session:
        rows = (await session.execute(select(Item.dealer))).all()
    counts: dict[str, int] = {d.code: 0 for d in dealers}
    main_count = 0
    for (d,) in rows:
        if d in known:
            counts[d] += 1
        else:
            # None, 'main' или код несуществующего дилера → «Без дилера»
            main_count += 1
    lines = ["Раздел диллеры:"]
    for d in dealers:
        lines.append(f"- {d.title}: {counts.get(d.code, 0)}")
    lines.append(f"- {MAIN_TITLE}: {main_count}")
    lines.append("")
    lines.append("Выберите действие:")
    return "\n".join(lines)

@router.message(Command("dealers"))
@router.message(F.text.in_(["/dealers", "👥 Дилеры"]))
async def dealers_home(message: Message, state: FSMContext) -> None:
    if not ensure_allowed_user(message):
        return
    if is_dealer_mode():
        await message.answer(ensure_admin_only(), reply_markup=main_menu_kb())
        return
    await state.clear()
    text = await dealers_counts_text()
    await message.answer(text, reply_markup=await dealers_menu_kb())

@router.callback_query(F.data.startswith("dealers:view:"))
async def dealers_view(cb: CallbackQuery) -> None:
    if is_dealer_mode():
        await cb.answer("Недоступно", show_alert=False)
        return
    await cb.answer()
    code = cb.data.split(":")[-1]
    if code == MAIN_CODE:
        title = MAIN_TITLE
    else:
        d = await get_dealer(code)
        if not d:
            await cb.message.answer("Неизвестный дилер (возможно, удалён).", reply_markup=await dealers_menu_kb())
            return
        title = d.title
    async with SessionLocal() as session:
        q = select(Item).where(Item.dealer == code).order_by(Item.due_date.asc())
        items = (await session.execute(q)).scalars().all()
    if not items:
        await cb.message.answer(f"{title}: записей нет.", reply_markup=await dealers_menu_kb())
        return
    header, lines = make_table_lines_without_id(items)
    header = f"{title}:\n" + "-" * 40 + "\n" + header
    chunks = split_text_chunks(header, lines)
    for i, ch in enumerate(chunks, 1):
        suffix = f"\n(стр. {i}/{len(chunks)})" if len(chunks) > 1 else ""
        await send_pre_chunk(cb.message, ch + suffix)
    await cb.message.answer(f"Всего записей ({title}): {len(items)}", reply_markup=await dealers_menu_kb())

@router.callback_query(F.data.startswith("dealers:export:"))
async def dealers_export(cb: CallbackQuery) -> None:
    if is_dealer_mode():
        await cb.answer("Недоступно", show_alert=False)
        return
    await cb.answer()
    code = cb.data.split(":")[-1]
    if code == MAIN_CODE:
        title = MAIN_TITLE
    else:
        d = await get_dealer(code)
        if not d:
            await cb.message.answer("Неизвестный дилер (возможно, удалён).")
            return
        title = d.title
    async with SessionLocal() as session:
        q = select(Item).where(Item.dealer == code).order_by(Item.due_date.asc())
        items = (await session.execute(q)).scalars().all()
    data = await build_items_csv_bytes(items)
    fname = f"export_{code}.csv"
    await cb.message.answer_document(
        BufferedInputFile(data, filename=fname),
        caption=f"Экспорт {title}: {len(items)} записей"
    )

# ===== Массовое назначение по списку USERID → дилер (только админ) =====

class DealerAssignStates(StatesGroup):
    waiting_ids = State()
    waiting_pick = State()

@router.callback_query(F.data == "dealers:assign:start")
async def dealers_assign_start(cb: CallbackQuery, state: FSMContext) -> None:
    if is_dealer_mode():
        await cb.answer("Недоступно", show_alert=False)
        return
    await cb.answer()
    await state.clear()
    await state.set_state(DealerAssignStates.waiting_ids)
    await cb.message.answer(
        "Отправьте список USERID через запятую/пробел/новую строку.\n"
        "Пример: 1323, 2005, 1383\n"
        "После этого предложу выбрать дилера.",
        reply_markup=main_menu_kb(),
    )

def parse_user_ids(text: str) -> List[int]:
    nums = re.findall(r"\d+", text or "")
    ids = [int(x) for x in nums]
    seen = set()
    out: List[int] = []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out

async def dealers_pick_kb() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for d in await list_dealers():
        rows.append([InlineKeyboardButton(text=f"Назначить → {d.title}", callback_data=f"dealers:assign:pick:{d.code}")])
    rows.append([InlineKeyboardButton(text=f"Назначить → {MAIN_TITLE}", callback_data=f"dealers:assign:pick:{MAIN_CODE}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

@router.message(DealerAssignStates.waiting_ids)
async def dealers_assign_ids(message: Message, state: FSMContext) -> None:
    if is_dealer_mode():
        await message.answer(ensure_admin_only(), reply_markup=main_menu_kb())
        return
    ids = parse_user_ids(message.text or "")
    if not ids:
        await message.answer("Не удалось распознать ни одного USERID. Пришлите числа через запятую/пробел/строки или /cancel.")
        return
    await state.update_data(assign_ids=ids)
    preview = ", ".join(str(x) for x in ids[:20]) + ("..." if len(ids) > 20 else "")
    await state.set_state(DealerAssignStates.waiting_pick)
    await message.answer(
        f"Найдено USERID: {len(ids)}\n"
        f"Пример: {preview}\n\n"
        "Выберите дилера, которому назначить:",
        reply_markup=await dealers_pick_kb(),
    )

@router.callback_query(F.data.startswith("dealers:assign:pick:"))
async def dealers_assign_pick(cb: CallbackQuery, state: FSMContext) -> None:
    if is_dealer_mode():
        await cb.answer("Недоступно", show_alert=False)
        return
    await cb.answer()
    code = cb.data.split(":")[-1]
    if code == MAIN_CODE:
        title = MAIN_TITLE
    else:
        d = await get_dealer(code)
        if not d:
            await cb.message.answer("Неизвестный дилер.")
            return
        title = d.title
    data = await state.get_data()
    ids: List[int] = data.get("assign_ids", [])
    if not ids:
        await cb.message.answer("Список USERID не найден в состоянии. Начните заново: /dealers → Назначить по списку.")
        return

    async with SessionLocal() as session:
        q = select(Item).where(Item.user_id.in_(ids))
        items = (await session.execute(q)).scalars().all()
        found = len(items)
        changed = 0
        for it in items:
            if it.dealer != code:
                it.dealer = code
                changed += 1
        await session.commit()

    await state.clear()
    await cb.message.answer(
        f"Готово. Передано дилеру: {title}\n"
        f"- USERID в запросе: {len(ids)}\n"
        f"- Найдено записей: {found}\n"
        f"- Обновлено (изменён dealer): {changed}\n",
        reply_markup=await dealers_menu_kb(),
    )


# ===== Добавление / изменение дилера (только админ) =====

class AddDealerStates(StatesGroup):
    waiting_code = State()
    waiting_title = State()
    waiting_chat_id = State()


@router.callback_query(F.data == "dealers:add:start")
async def dealer_add_start(cb: CallbackQuery, state: FSMContext) -> None:
    if is_dealer_mode():
        await cb.answer("Недоступно", show_alert=False)
        return
    await cb.answer()
    await state.clear()
    await state.set_state(AddDealerStates.waiting_code)
    await cb.message.answer(
        "Добавление дилера.\n"
        "Шаг 1/3. Введите КОД дилера — латиницей, без пробелов "
        "(буквы, цифры, _ ; 2–32 символа). Например: vasya\n"
        "Если такой код уже есть — данные дилера будут обновлены.\n\n"
        "Отмена — /cancel",
        reply_markup=main_menu_kb(),
    )


@router.message(AddDealerStates.waiting_code)
async def dealer_add_code(message: Message, state: FSMContext) -> None:
    code = (message.text or "").strip().lower()
    if code == MAIN_CODE:
        await message.answer("Код «main» зарезервирован системой. Введите другой код или /cancel.", reply_markup=main_menu_kb())
        return
    if not DEALER_CODE_RE.match(code):
        await message.answer(
            "Неверный код. Разрешены латинские буквы, цифры и _ (2–32 символа), без пробелов.\n"
            "Попробуйте ещё раз или /cancel.",
            reply_markup=main_menu_kb(),
        )
        return
    await state.update_data(code=code)
    await state.set_state(AddDealerStates.waiting_title)
    await message.answer(
        "Шаг 2/3. Введите НАЗВАНИЕ дилера — как показывать в меню (например: Вася):",
        reply_markup=main_menu_kb(),
    )


@router.message(AddDealerStates.waiting_title)
async def dealer_add_title(message: Message, state: FSMContext) -> None:
    title = (message.text or "").strip()
    if not title or len(title) > 64:
        await message.answer("Название не может быть пустым или длиннее 64 символов. Ещё раз или /cancel.", reply_markup=main_menu_kb())
        return
    await state.update_data(title=title)
    await state.set_state(AddDealerStates.waiting_chat_id)
    await message.answer(
        "Шаг 3/3. Введите Telegram ID дилера (число) — на него бот будет отправлять сообщения.\n"
        "Если ID пока неизвестен — отправьте «-» (можно задать позже, добавив дилера с тем же кодом).",
        reply_markup=main_menu_kb(),
    )


@router.message(AddDealerStates.waiting_chat_id)
async def dealer_add_chat_id(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    chat_id = None
    if raw not in ("-", "—"):
        digits = raw[1:] if raw.startswith("-") else raw
        if not digits.isdigit():
            await message.answer("Telegram ID должен быть числом, либо «-» чтобы пропустить. Ещё раз или /cancel.", reply_markup=main_menu_kb())
            return
        chat_id = int(raw)
    data = await state.get_data()
    code = data.get("code")
    title = data.get("title")
    if not code or not title:
        await state.clear()
        await message.answer("Ввод сброшен. Начните заново через /dealers.", reply_markup=main_menu_kb())
        return
    async with SessionLocal() as session:
        existing = (await session.execute(select(Dealer).where(Dealer.code == code))).scalars().first()
        if existing:
            existing.title = title
            existing.chat_id = chat_id
            action = "обновлён"
        else:
            session.add(Dealer(code=code, title=title, chat_id=chat_id))
            action = "добавлен"
        await session.commit()
    await state.clear()
    cid_txt = str(chat_id) if chat_id is not None else "не задан"
    await message.answer(
        f"✅ Дилер {action}: {title}\nКод: {code}\nTelegram ID: {cid_txt}",
        reply_markup=main_menu_kb(),
    )
    await message.answer(await dealers_counts_text(), reply_markup=await dealers_menu_kb())


# ===== Удаление дилера (только админ) =====

@router.callback_query(F.data == "dealers:del:start")
async def dealer_del_start(cb: CallbackQuery) -> None:
    if is_dealer_mode():
        await cb.answer("Недоступно", show_alert=False)
        return
    await cb.answer()
    dealers = await list_dealers()
    if not dealers:
        await cb.message.answer("Список дилеров пуст — удалять некого.", reply_markup=await dealers_menu_kb())
        return
    rows = [[InlineKeyboardButton(text=f"🗑 {d.title}", callback_data=f"dealers:del:pick:{d.code}")] for d in dealers]
    await cb.message.answer(
        "Выберите дилера для удаления.\nЕго клиенты будут перенесены в «Без дилера».",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("dealers:del:pick:"))
async def dealer_del_pick(cb: CallbackQuery) -> None:
    if is_dealer_mode():
        await cb.answer("Недоступно", show_alert=False)
        return
    await cb.answer()
    code = cb.data.split(":")[-1]
    d = await get_dealer(code)
    if not d:
        await cb.message.answer("Дилер не найден (возможно, уже удалён).", reply_markup=await dealers_menu_kb())
        return
    async with SessionLocal() as session:
        cnt = len((await session.execute(select(Item.id).where(Item.dealer == code))).all())
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Удалить", callback_data=f"dealers:del:confirm:{code}"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="dealers:del:cancel"),
    ]])
    await cb.message.answer(
        f"Удалить дилера «{d.title}» (код {code})?\n"
        f"Записей у дилера: {cnt} — они будут перенесены в «Без дилера».",
        reply_markup=kb,
    )


@router.callback_query(F.data == "dealers:del:cancel")
async def dealer_del_cancel(cb: CallbackQuery) -> None:
    await cb.answer("Отменено")
    await cb.message.answer("Удаление отменено.", reply_markup=await dealers_menu_kb())


@router.callback_query(F.data.startswith("dealers:del:confirm:"))
async def dealer_del_confirm(cb: CallbackQuery) -> None:
    if is_dealer_mode():
        await cb.answer("Недоступно", show_alert=False)
        return
    await cb.answer()
    code = cb.data.split(":")[-1]
    async with SessionLocal() as session:
        d = (await session.execute(select(Dealer).where(Dealer.code == code))).scalars().first()
        if not d:
            await cb.message.answer("Дилер не найден (возможно, уже удалён).", reply_markup=await dealers_menu_kb())
            return
        title = d.title
        res = await session.execute(
            update(Item).where(Item.dealer == code).values(dealer=MAIN_CODE)
        )
        moved = res.rowcount or 0
        await session.execute(delete(Dealer).where(Dealer.code == code))
        await session.commit()
    await cb.message.answer(
        f"🗑️ Дилер «{title}» удалён.\nПеренесено в «Без дилера»: {moved} записей.",
        reply_markup=await dealers_menu_kb(),
    )


# ===== Сообщение дилеру / рассылка (только админ) =====

class MsgDealerStates(StatesGroup):
    waiting_text = State()


class BroadcastStates(StatesGroup):
    waiting_text = State()


@router.callback_query(F.data == "dealers:msg:start")
async def dealer_msg_start(cb: CallbackQuery) -> None:
    if is_dealer_mode():
        await cb.answer("Недоступно", show_alert=False)
        return
    await cb.answer()
    dealers = await list_dealers()
    if not dealers:
        await cb.message.answer("Список дилеров пуст.", reply_markup=await dealers_menu_kb())
        return
    rows = []
    for d in dealers:
        mark = "" if d.chat_id is not None else "  (нет ID)"
        rows.append([InlineKeyboardButton(text=f"✉️ {d.title}{mark}", callback_data=f"dealers:msg:pick:{d.code}")])
    await cb.message.answer("Кому отправить сообщение?", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("dealers:msg:pick:"))
async def dealer_msg_pick(cb: CallbackQuery, state: FSMContext) -> None:
    if is_dealer_mode():
        await cb.answer("Недоступно", show_alert=False)
        return
    await cb.answer()
    code = cb.data.split(":")[-1]
    d = await get_dealer(code)
    if not d:
        await cb.message.answer("Дилер не найден.", reply_markup=await dealers_menu_kb())
        return
    if d.chat_id is None:
        await cb.message.answer(
            f"У дилера «{d.title}» не задан Telegram ID.\n"
            "Задайте его: «➕ Добавить дилера» → введите тот же код, данные обновятся.",
            reply_markup=await dealers_menu_kb(),
        )
        return
    await state.clear()
    await state.update_data(target_code=code)
    await state.set_state(MsgDealerStates.waiting_text)
    await cb.message.answer(
        f"Введите текст сообщения для дилера «{d.title}».\nОтмена — /cancel",
        reply_markup=main_menu_kb(),
    )


@router.message(MsgDealerStates.waiting_text)
async def dealer_msg_send(message: Message, state: FSMContext, bot: Bot) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Пустое сообщение. Введите текст или /cancel.", reply_markup=main_menu_kb())
        return
    data = await state.get_data()
    code = data.get("target_code")
    await state.clear()
    d = await get_dealer(code)
    if not d or d.chat_id is None:
        await message.answer("Дилер не найден или у него не задан Telegram ID.", reply_markup=await dealers_menu_kb())
        return
    body = f"📨 Сообщение от администратора:\n\n{text}"
    try:
        await bot.send_message(d.chat_id, body)
    except (TelegramForbiddenError, TelegramBadRequest):
        await message.answer(
            f"❌ Не удалось отправить дилеру «{d.title}».\n"
            "Скорее всего, дилер ещё не открыл этого бота и не нажал «Запустить» (Start). "
            "Попросите его это сделать и попробуйте снова.",
            reply_markup=await dealers_menu_kb(),
        )
        return
    except Exception as e:
        await message.answer(f"❌ Ошибка отправки дилеру «{d.title}»: {e}", reply_markup=await dealers_menu_kb())
        return
    await message.answer(f"✅ Сообщение отправлено дилеру «{d.title}».", reply_markup=await dealers_menu_kb())


@router.callback_query(F.data == "dealers:broadcast:start")
async def dealer_broadcast_start(cb: CallbackQuery, state: FSMContext) -> None:
    if is_dealer_mode():
        await cb.answer("Недоступно", show_alert=False)
        return
    await cb.answer()
    targets = [d for d in await list_dealers() if d.chat_id is not None]
    if not targets:
        await cb.message.answer(
            "Нет дилеров с заданным Telegram ID — рассылать некому.\n"
            "Задайте ID дилерам через «➕ Добавить дилера».",
            reply_markup=await dealers_menu_kb(),
        )
        return
    await state.clear()
    await state.set_state(BroadcastStates.waiting_text)
    names = ", ".join(d.title for d in targets)
    await cb.message.answer(
        f"Рассылка для {len(targets)} дилеров: {names}\n\n"
        "Введите текст сообщения. Отмена — /cancel",
        reply_markup=main_menu_kb(),
    )


@router.message(BroadcastStates.waiting_text)
async def dealer_broadcast_send(message: Message, state: FSMContext, bot: Bot) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Пустое сообщение. Введите текст или /cancel.", reply_markup=main_menu_kb())
        return
    await state.clear()
    targets = [d for d in await list_dealers() if d.chat_id is not None]
    if not targets:
        await message.answer("Нет дилеров с заданным Telegram ID.", reply_markup=await dealers_menu_kb())
        return
    body = f"📢 Сообщение от администратора:\n\n{text}"
    ok = 0
    failed: list[str] = []
    for d in targets:
        try:
            await bot.send_message(d.chat_id, body)
            ok += 1
        except Exception:
            failed.append(d.title)
    report = f"📢 Рассылка завершена.\n✅ Доставлено: {ok} из {len(targets)}"
    if failed:
        report += (
            f"\n❌ Не доставлено ({len(failed)}): {', '.join(failed)}\n"
            "Эти дилеры, вероятно, не нажимали «Запустить» (Start) у бота-админа."
        )
    await message.answer(report, reply_markup=await dealers_menu_kb())

# ==== Заглушки для dealer-режима ====

if is_dealer_mode():
    @router.message(Command("add"))
    @router.message(Command("renew"))
    @router.message(Command("delete"))
    @router.message(Command("dealers"))
    @router.message(Command("timezone"))
    @router.message(F.text.in_(["/add","/renew","/delete","/dealers","/timezone","/cancel"]))
    async def dealer_stub(message: Message) -> None:
        if not ensure_allowed_user(message):
            return
        await message.answer(ensure_admin_only(), reply_markup=main_menu_kb())


# ====== Кабинет дилера (единый бот, роль 'dealer') ======

def dealer_user_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Добавить"), KeyboardButton(text="🔄 Продлить")],
            [KeyboardButton(text="📋 Список"), KeyboardButton(text="⛔ Отключённые")],
            [KeyboardButton(text="⏰ Ближайшие"), KeyboardButton(text="📊 Статус")],
            [KeyboardButton(text="💰 Баланс"), KeyboardButton(text="💳 Оплата")],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выберите команду…",
        selective=True,
    )


@dealer_router.message(CommandStart())
@dealer_router.message(F.text == "/start")
async def dealer_on_start(message: Message) -> None:
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
async def dealer_on_help(message: Message) -> None:
    text = "Доступные команды:\n" + "\n".join(f"/{c.command} — {c.description}" for c in BOT_COMMANDS_DEALER)
    await message.answer(text, reply_markup=dealer_user_menu_kb())


@dealer_router.message(Command("list"))
@dealer_router.message(F.text.in_(["/list", "📋 Список"]))
async def dealer_on_list(message: Message) -> None:
    d = await dealer_by_chat(message.from_user.id)
    if not d:
        return
    async with SessionLocal() as session:
        q = select(Item).where(Item.dealer == d.code).order_by(Item.due_date.asc())
        items = (await session.execute(q)).scalars().all()
    if not items:
        await message.answer("Список пуст.", reply_markup=dealer_user_menu_kb())
        return
    header, lines = make_table_lines_without_id(items)
    chunks = split_text_chunks(header, lines)
    for i, ch in enumerate(chunks, 1):
        suffix = f"\n(стр. {i}/{len(chunks)})" if len(chunks) > 1 else ""
        await send_pre_chunk(message, ch + suffix)
    await message.answer(f"Всего записей: {len(items)}", reply_markup=dealer_user_menu_kb())


@dealer_router.message(Command("disabled"))
@dealer_router.message(F.text.in_(["/disabled", "⛔ Отключённые"]))
async def dealer_on_disabled(message: Message) -> None:
    d = await dealer_by_chat(message.from_user.id)
    if not d:
        return
    now = now_tz()
    async with SessionLocal() as session:
        q = select(Item).where(Item.dealer == d.code).order_by(Item.due_date.asc())
        items = (await session.execute(q)).scalars().all()
    expired = [it for it in items if to_tz(it.due_date) <= now]
    if not expired:
        await message.answer("Отключённых (просроченных) нет.", reply_markup=dealer_user_menu_kb())
        return
    header, lines = make_table_lines_without_id(expired)
    header = "Disabled (просроченные):\n" + "-" * 40 + "\n" + header
    chunks = split_text_chunks(header, lines)
    for i, ch in enumerate(chunks, 1):
        suffix = f"\n(стр. {i}/{len(chunks)})" if len(chunks) > 1 else ""
        await send_pre_chunk(message, ch + suffix)


@dealer_router.message(Command("next"))
@dealer_router.message(F.text.in_(["/next", "⏰ Ближайшие"]))
async def dealer_on_next(message: Message) -> None:
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
        await message.answer("Нет истечений в ближайшие 3 дня.", reply_markup=dealer_user_menu_kb())
        return
    header, lines = make_table_lines_without_id(window)
    header = "Ближайшие (до 3 дней):\n" + "-" * 40 + "\n" + header
    chunks = split_text_chunks(header, lines)
    for i, ch in enumerate(chunks, 1):
        suffix = f"\n(стр. {i}/{len(chunks)})" if len(chunks) > 1 else ""
        await send_pre_chunk(message, ch + suffix)


@dealer_router.message(Command("status"))
@dealer_router.message(F.text.in_(["/status", "📊 Статус"]))
async def dealer_on_status(message: Message) -> None:
    d = await dealer_by_chat(message.from_user.id)
    if not d:
        return
    async with SessionLocal() as session:
        cnt = len((await session.execute(select(Item.id).where(Item.dealer == d.code))).all())
    await message.answer(
        f"Бот работает ✅\nДилер: {d.title}\nВаших записей: {cnt}",
        reply_markup=dealer_user_menu_kb(),
    )




# ===== Редактирование имени клиента (дилер) =====

class DealerEditStates(StatesGroup):
    waiting_search = State()
    waiting_value = State()


@dealer_router.message(Command("edit"))
@dealer_router.message(F.text.in_(["/edit"]))
async def dealer_edit_start(message: Message, state: FSMContext) -> None:
    d = await dealer_by_chat(message.from_user.id)
    if not d:
        return
    await state.clear()
    await state.set_state(DealerEditStates.waiting_search)
    await message.answer(
        "✏️ Редактор\n\nВведите USERID для поиска:",
        reply_markup=dealer_user_menu_kb(),
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
            q = select(Item).where(Item.note.ilike(f"%{text}%"), Item.dealer == d.code)
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
        await cb.message.answer("Запись не найдена.", reply_markup=dealer_user_menu_kb())
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
            await message.answer("Запись не найдена.", reply_markup=dealer_user_menu_kb())
            return
        it.note = text
        await session.commit()
    await state.clear()
    await message.answer(
        f"✅ Имя клиента обновлено: {text}",
        reply_markup=dealer_user_menu_kb(),
    )


# ===== Заказ новых ключей (дилер) =====

class DealerOrderStates(StatesGroup):
    waiting_names = State()

@dealer_router.message(Command("order"))
@dealer_router.message(F.text.in_(["/order", "➕ Добавить"]))
async def dealer_order_start(message: Message) -> None:
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
        oid = _next_order_id()
        _pending_orders[oid] = {
            "dealer_code": d.code,
            "dealer_title": d.title,
            "dealer_chat_id": d.chat_id,
            "names": list(names),
            "fulfilled": 0,
        }
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
        except Exception:
            pass
    await message.answer(
        f"✅ Запрос на {total} ключей отправлен администратору.\n"
        f"Клиенты:\n{names_list}\n\nОжидайте.",
        reply_markup=dealer_user_menu_kb(),
    )


@dealer_router.callback_query(F.data == "dorder:no")
async def dealer_order_cancel(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await state.clear()
    await cb.message.answer("Отменено.", reply_markup=dealer_user_menu_kb())


# ===== Запрос дилера на продление клиента =====

class DealerRenewStates(StatesGroup):
    waiting_userid = State()
    waiting_comment = State()


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
        reply_markup=dealer_user_menu_kb(),
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
        await message.answer("USERID должен быть числом. Введите ещё раз или /cancel.", reply_markup=dealer_user_menu_kb())
        return
    uid = int(text)
    async with SessionLocal() as session:
        q = select(Item).where(Item.dealer == d.code, Item.user_id == uid).order_by(Item.due_date.asc())
        items = (await session.execute(q)).scalars().all()
    if not items:
        await message.answer("Клиент с таким USERID не найден среди ваших. Введите ещё раз или /cancel.", reply_markup=dealer_user_menu_kb())
        return
    if len(items) == 1:
        it = items[0]
        await state.update_data(item_id=it.id)
        await state.set_state(DealerRenewStates.waiting_comment)
        await message.answer(_dealer_renew_comment_prompt(it), reply_markup=dealer_user_menu_kb())
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
        await cb.message.answer("Запись не найдена среди ваших клиентов.", reply_markup=dealer_user_menu_kb())
        return
    await state.update_data(item_id=it.id)
    await state.set_state(DealerRenewStates.waiting_comment)
    await cb.message.answer(_dealer_renew_comment_prompt(it), reply_markup=dealer_user_menu_kb())


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
        await message.answer("Что-то пошло не так. Начните заново — /renew.", reply_markup=dealer_user_menu_kb())
        return
    async with SessionLocal() as session:
        it = await session.get(Item, int(item_id))
    if not it or it.dealer != d.code:
        await message.answer("Запись не найдена среди ваших клиентов.", reply_markup=dealer_user_menu_kb())
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
        except Exception:
            pass
    await message.answer(
        "✅ Запрос на продление отправлен администратору. Ожидайте подтверждения.",
        reply_markup=dealer_user_menu_kb(),
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
async def dealer_on_balance(message: Message) -> None:
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
    await message.answer("\n".join(lines), reply_markup=dealer_user_menu_kb())


# ===== Оплата (дилер) =====

class DealerPayStates(StatesGroup):
    waiting_amount = State()


async def _dealer_show_methods(target, d: Dealer) -> None:
    methods = await list_payment_methods(active_only=True)
    bal = d.balance or 0.0
    if not methods:
        await target.answer(
            f"💳 Оплата\nВаш долг: ${bal:g}\n\n"
            "Методы оплаты пока не настроены. Обратитесь к администратору.",
            reply_markup=dealer_user_menu_kb(),
        )
        return
    rows = [[InlineKeyboardButton(text=m.name, callback_data=f"dpay:m:{m.id}")] for m in methods]
    await target.answer(
        f"💳 Оплата\nВаш долг: ${bal:g}\n\nВыберите метод оплаты:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@dealer_router.message(Command("pay"))
@dealer_router.message(F.text.in_(["/pay", "💳 Оплата"]))
async def dealer_on_pay(message: Message) -> None:
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
        await cb.message.answer("Метод недоступен.", reply_markup=dealer_user_menu_kb())
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
        await cb.message.answer("Вид оплаты недоступен.", reply_markup=dealer_user_menu_kb())
        return
    m = await get_payment_method(v.method_id)
    if not m or not m.active:
        await cb.message.answer("Метод недоступен.", reply_markup=dealer_user_menu_kb())
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
        await cb.message.answer("Вид оплаты недоступен.", reply_markup=dealer_user_menu_kb())
        return
    m = await get_payment_method(v.method_id)
    if not m or not m.active:
        await cb.message.answer("Метод недоступен.", reply_markup=dealer_user_menu_kb())
        return
    await state.clear()
    await state.update_data(pay_method=m.name, pay_variant=v.name)
    await state.set_state(DealerPayStates.waiting_amount)
    await cb.message.answer(
        f"Метод: {m.name} → {v.name}\nВведите сумму в $, которую вы перечислили. Отмена — /cancel",
        reply_markup=dealer_user_menu_kb(),
    )


@dealer_router.message(DealerPayStates.waiting_amount)
async def dealer_pay_amount(message: Message, state: FSMContext, bot: Bot) -> None:
    d = await dealer_by_chat(message.from_user.id)
    if not d:
        await state.clear()
        return
    amount = _parse_amount(message.text or "")
    if amount is None:
        await message.answer("Введите положительное число (например 5). Ещё раз или /cancel.", reply_markup=dealer_user_menu_kb())
        return
    data = await state.get_data()
    method = data.get("pay_method")
    variant = data.get("pay_variant")
    await state.clear()
    if not method:
        await message.answer("Что-то пошло не так. Начните заново — /pay.", reply_markup=dealer_user_menu_kb())
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
        except Exception:
            pass
    await message.answer(
        f"✅ Заявка на оплату отправлена администратору.\n"
        f"Метод: {method} → {variant or '—'}\nСумма: ${amount:g}\n"
        "Долг уменьшится после подтверждения.",
        reply_markup=dealer_user_menu_kb(),
    )


@dealer_router.message()
async def dealer_fallback(message: Message) -> None:
    cmds = ", ".join(f"/{c.command}" for c in BOT_COMMANDS_DEALER if c.command not in ("start", "help"))
    await message.answer(
        f"Доступные команды: {cmds}.",
        reply_markup=dealer_user_menu_kb(),
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
        await cb.message.answer("Запись не найдена (возможно, удалена).", reply_markup=main_menu_kb())
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
    await cb.message.answer("Отправьте новую дату в формате:\nYYYY-MM-DD HH:MM:SS", reply_markup=main_menu_kb())


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
        await cb.message.answer("Запись не найдена (возможно, удалена).", reply_markup=main_menu_kb())
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
        except Exception:
            pass
    await cb.message.answer(
        f"Запрос отклонён. USERID={user_id}, USERNAME={username}.",
        reply_markup=main_menu_kb(),
    )


# ====== Баланс и долги дилеров (админ) ======

class BalanceStates(StatesGroup):
    waiting_amount = State()
    waiting_comment = State()
    waiting_price = State()


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
    if not ensure_allowed_user(message):
        return
    if is_dealer_mode():
        await message.answer(ensure_admin_only(), reply_markup=main_menu_kb())
        return
    await state.clear()
    await message.answer(await balance_overview_text(), reply_markup=balance_menu_kb())


@router.callback_query(F.data == "bal:add:start")
async def bal_add_start(cb: CallbackQuery) -> None:
    if is_dealer_mode():
        await cb.answer("Недоступно", show_alert=False)
        return
    await cb.answer()
    if not await list_dealers():
        await cb.message.answer("Список дилеров пуст.", reply_markup=balance_menu_kb())
        return
    await cb.message.answer("Кому добавить долг?", reply_markup=await _balance_pick_dealer_kb("add"))


@router.callback_query(F.data == "bal:sub:start")
async def bal_sub_start(cb: CallbackQuery) -> None:
    if is_dealer_mode():
        await cb.answer("Недоступно", show_alert=False)
        return
    await cb.answer()
    if not await list_dealers():
        await cb.message.answer("Список дилеров пуст.", reply_markup=balance_menu_kb())
        return
    await cb.message.answer("У кого снять долг?", reply_markup=await _balance_pick_dealer_kb("sub"))


@router.callback_query(F.data.startswith("bal:pick:"))
async def bal_pick(cb: CallbackQuery, state: FSMContext) -> None:
    if is_dealer_mode():
        await cb.answer("Недоступно", show_alert=False)
        return
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
        reply_markup=main_menu_kb(),
    )


@router.message(BalanceStates.waiting_amount)
async def bal_amount(message: Message, state: FSMContext) -> None:
    amount = _parse_amount(message.text or "")
    if amount is None:
        await message.answer("Введите положительное число (например 5). Ещё раз или /cancel.", reply_markup=main_menu_kb())
        return
    await state.update_data(bal_amount=amount)
    await state.set_state(BalanceStates.waiting_comment)
    await message.answer(
        "Введите комментарий к операции (например «корректировка») или «-», чтобы пропустить.",
        reply_markup=main_menu_kb(),
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
        await message.answer("Ввод сброшен. Начните заново — /balance.", reply_markup=main_menu_kb())
        return
    d = await get_dealer(code)
    if not d:
        await message.answer("Дилер не найден.", reply_markup=main_menu_kb())
        return
    signed = amount if direction == "add" else -amount
    kind = "admin_add" if direction == "add" else "admin_sub"
    new_balance = await apply_balance_change(code, signed, kind, comment)
    if new_balance is None:
        await message.answer("Не удалось изменить баланс.", reply_markup=main_menu_kb())
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
        except Exception:
            pass
    await message.answer(
        f"✅ Готово. Дилеру «{d.title}» {word} ${amount:g}.\nНовый долг: ${new_balance:g}",
        reply_markup=main_menu_kb(),
    )
    await message.answer(await balance_overview_text(), reply_markup=balance_menu_kb())


@router.callback_query(F.data == "bal:price:start")
async def bal_price_start(cb: CallbackQuery, state: FSMContext) -> None:
    if is_dealer_mode():
        await cb.answer("Недоступно", show_alert=False)
        return
    await cb.answer()
    price = await get_price()
    await state.clear()
    await state.set_state(BalanceStates.waiting_price)
    await cb.message.answer(
        f"Текущая цена за продление: ${price:g}\n"
        "Введите новую цену в $ (например 5). Отмена — /cancel",
        reply_markup=main_menu_kb(),
    )


@router.message(BalanceStates.waiting_price)
async def bal_price_set(message: Message, state: FSMContext) -> None:
    amount = _parse_amount(message.text or "")
    if amount is None:
        await message.answer("Введите положительное число (например 5). Ещё раз или /cancel.", reply_markup=main_menu_kb())
        return
    await state.clear()
    await set_price(amount)
    await message.answer(f"✅ Цена за продление установлена: ${amount:g}", reply_markup=main_menu_kb())
    await message.answer(await balance_overview_text(), reply_markup=balance_menu_kb())


# ====== Методы оплаты и подтверждение оплат (админ) ======

class PayAdminStates(StatesGroup):
    waiting_requisites = State()  # legacy (старые реквизиты метода)
    waiting_method_name = State()
    waiting_method_rename = State()
    waiting_variant_name = State()
    waiting_variant_new_req = State()
    waiting_variant_requisites = State()
    waiting_variant_rename = State()


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
    if not ensure_allowed_user(message):
        return
    if is_dealer_mode():
        await message.answer(ensure_admin_only(), reply_markup=main_menu_kb())
        return
    await state.clear()
    await message.answer(await pay_admin_text(), reply_markup=await pay_admin_kb())


@router.callback_query(F.data == "pm:home")
async def pm_home(cb: CallbackQuery) -> None:
    if is_dealer_mode():
        await cb.answer("Недоступно", show_alert=False)
        return
    await cb.answer()
    await cb.message.answer(await pay_admin_text(), reply_markup=await pay_admin_kb())


@router.callback_query(F.data.startswith("pm:open:"))
async def pm_open(cb: CallbackQuery) -> None:
    if is_dealer_mode():
        await cb.answer("Недоступно", show_alert=False)
        return
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
    if is_dealer_mode():
        await cb.answer("Недоступно", show_alert=False)
        return
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
    if is_dealer_mode():
        await cb.answer("Недоступно", show_alert=False)
        return
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
        reply_markup=main_menu_kb(),
    )


@router.message(PayAdminStates.waiting_requisites)
async def pm_req_save(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Реквизиты не могут быть пустыми. Ещё раз или /cancel.", reply_markup=main_menu_kb())
        return
    data = await state.get_data()
    pm_id = data.get("pm_id")
    await state.clear()
    if not pm_id:
        await message.answer("Ввод сброшен. Начните заново — /pay.", reply_markup=main_menu_kb())
        return
    async with SessionLocal() as session:
        m = await session.get(PaymentMethod, int(pm_id))
        if not m:
            await message.answer("Метод не найден.", reply_markup=main_menu_kb())
            return
        m.requisites = text[:1024]
        name = m.name
        await session.commit()
    await message.answer(f"✅ Реквизиты метода «{name}» обновлены.", reply_markup=main_menu_kb())
    await message.answer(await pay_admin_text(), reply_markup=await pay_admin_kb())


@router.callback_query(F.data == "pm:add:start")
async def pm_add_start(cb: CallbackQuery, state: FSMContext) -> None:
    if is_dealer_mode():
        await cb.answer("Недоступно", show_alert=False)
        return
    await cb.answer()
    await state.clear()
    await state.set_state(PayAdminStates.waiting_method_name)
    await cb.message.answer(
        "Введите название нового метода оплаты (например: Каспи).\nОтмена — /cancel",
        reply_markup=main_menu_kb(),
    )


@router.message(PayAdminStates.waiting_method_name)
async def pm_add_save(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name or len(name) > 64:
        await message.answer("Название не должно быть пустым или длиннее 64 символов. Ещё раз или /cancel.", reply_markup=main_menu_kb())
        return
    await state.clear()
    async with SessionLocal() as session:
        existing = (await session.execute(
            select(PaymentMethod).where(PaymentMethod.name == name)
        )).scalars().first()
        if existing:
            await message.answer(f"Метод «{name}» уже существует.", reply_markup=main_menu_kb())
            return
        session.add(PaymentMethod(name=name, requisites="", active=True))
        await session.commit()
    await message.answer(
        f"✅ Метод «{name}» добавлен. Не забудьте задать ему реквизиты.",
        reply_markup=main_menu_kb(),
    )
    await message.answer(await pay_admin_text(), reply_markup=await pay_admin_kb())


@router.callback_query(F.data.startswith("pay:ok:"))
async def pay_confirm(cb: CallbackQuery, bot: Bot) -> None:
    if is_dealer_mode():
        await cb.answer("Недоступно", show_alert=False)
        return
    await cb.answer()
    try:
        pay_id = int(cb.data.split(":")[-1])
    except Exception:
        return
    async with SessionLocal() as session:
        pay = await session.get(Payment, pay_id)
        if not pay:
            await cb.message.answer("Заявка на оплату не найдена.", reply_markup=main_menu_kb())
            return
        if pay.status != "pending":
            await cb.message.answer(
                f"Заявка уже обработана (статус: {pay.status}).",
                reply_markup=main_menu_kb(),
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
        except Exception:
            pass
    bal_show = f"${new_balance:g}" if new_balance is not None else "?"
    await cb.message.answer(
        f"✅ Оплата подтверждена.\nДилер: {d.title if d else dealer_code}\n"
        f"Метод: {method_full}, сумма: ${amount:g}\nНовый долг: {bal_show}",
        reply_markup=main_menu_kb(),
    )


@router.callback_query(F.data.startswith("pay:no:"))
async def pay_reject(cb: CallbackQuery, bot: Bot) -> None:
    if is_dealer_mode():
        await cb.answer("Недоступно", show_alert=False)
        return
    await cb.answer()
    try:
        pay_id = int(cb.data.split(":")[-1])
    except Exception:
        return
    async with SessionLocal() as session:
        pay = await session.get(Payment, pay_id)
        if not pay:
            await cb.message.answer("Заявка на оплату не найдена.", reply_markup=main_menu_kb())
            return
        if pay.status != "pending":
            await cb.message.answer(
                f"Заявка уже обработана (статус: {pay.status}).",
                reply_markup=main_menu_kb(),
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
        except Exception:
            pass
    await cb.message.answer(
        f"Оплата отклонена.\nДилер: {d.title if d else dealer_code}\nМетод: {method_full}, сумма: ${amount:g}",
        reply_markup=main_menu_kb(),
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
    if is_dealer_mode():
        await cb.answer("Недоступно", show_alert=False)
        return
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
        reply_markup=main_menu_kb(),
    )


@router.message(PayAdminStates.waiting_method_rename)
async def pm_rename_save(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name or len(name) > 64:
        await message.answer("Название не должно быть пустым или длиннее 64 символов. Ещё раз или /cancel.", reply_markup=main_menu_kb())
        return
    data = await state.get_data()
    pm_id = data.get("pm_id")
    if not pm_id:
        await state.clear()
        await message.answer("Ввод сброшен. Начните заново — /pay.", reply_markup=main_menu_kb())
        return
    async with SessionLocal() as session:
        existing = (await session.execute(
            select(PaymentMethod).where(PaymentMethod.name == name, PaymentMethod.id != int(pm_id))
        )).scalars().first()
        if existing:
            await message.answer(f"Метод «{name}» уже существует. Выберите другое название или /cancel.", reply_markup=main_menu_kb())
            return
        m = await session.get(PaymentMethod, int(pm_id))
        if not m:
            await state.clear()
            await message.answer("Метод не найден.", reply_markup=main_menu_kb())
            return
        m.name = name
        await session.commit()
    await state.clear()
    await message.answer(f"✅ Метод переименован: {name}", reply_markup=main_menu_kb())
    m2 = await get_payment_method(int(pm_id))
    if m2:
        text, kb = await _method_card(m2)
        await message.answer(text, reply_markup=kb)


# --- Добавление вида под методом ---

@router.callback_query(F.data.startswith("pm:vadd:"))
async def pm_vadd_start(cb: CallbackQuery, state: FSMContext) -> None:
    if is_dealer_mode():
        await cb.answer("Недоступно", show_alert=False)
        return
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
        reply_markup=main_menu_kb(),
    )


@router.message(PayAdminStates.waiting_variant_name)
async def pm_vadd_name_save(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name or len(name) > 64:
        await message.answer("Название не должно быть пустым или длиннее 64 символов. Ещё раз или /cancel.", reply_markup=main_menu_kb())
        return
    data = await state.get_data()
    pm_id = data.get("pm_id")
    if not pm_id:
        await state.clear()
        await message.answer("Ввод сброшен. Начните заново — /pay.", reply_markup=main_menu_kb())
        return
    await state.update_data(v_name=name)
    await state.set_state(PayAdminStates.waiting_variant_new_req)
    await message.answer(
        f"Вид «{name}». Введите реквизиты (номер карты/кошелька, инструкции).\nИли «-», чтобы пропустить.",
        reply_markup=main_menu_kb(),
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
        await message.answer("Ввод сброшен. Начните заново — /pay.", reply_markup=main_menu_kb())
        return
    async with SessionLocal() as session:
        v = PaymentVariant(method_id=int(pm_id), name=name, requisites=req, active=True)
        session.add(v)
        await session.commit()
    await message.answer(f"✅ Вид «{name}» добавлен.", reply_markup=main_menu_kb())
    m2 = await get_payment_method(int(pm_id))
    if m2:
        text, kb = await _method_card(m2)
        await message.answer(text, reply_markup=kb)


# --- Карточка вида: открыть, вкл/выкл, реквизиты, переименовать ---

@router.callback_query(F.data.startswith("pv:open:"))
async def pv_open(cb: CallbackQuery) -> None:
    if is_dealer_mode():
        await cb.answer("Недоступно", show_alert=False)
        return
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
    if is_dealer_mode():
        await cb.answer("Недоступно", show_alert=False)
        return
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
    if is_dealer_mode():
        await cb.answer("Недоступно", show_alert=False)
        return
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
        reply_markup=main_menu_kb(),
    )


@router.message(PayAdminStates.waiting_variant_requisites)
async def pv_req_save(message: Message, state: FSMContext) -> None:
    text_in = (message.text or "").strip()
    if not text_in:
        await message.answer("Реквизиты не могут быть пустыми. Ещё раз или /cancel.", reply_markup=main_menu_kb())
        return
    data = await state.get_data()
    v_id = data.get("v_id")
    await state.clear()
    if not v_id:
        await message.answer("Ввод сброшен. Начните заново — /pay.", reply_markup=main_menu_kb())
        return
    async with SessionLocal() as session:
        v = await session.get(PaymentVariant, int(v_id))
        if not v:
            await message.answer("Вид не найден.", reply_markup=main_menu_kb())
            return
        v.requisites = text_in[:1024]
        method_id = v.method_id
        await session.commit()
    await message.answer("✅ Реквизиты обновлены.", reply_markup=main_menu_kb())
    v2 = await get_payment_variant(int(v_id))
    m = await get_payment_method(method_id)
    if v2 and m:
        text, kb = await _variant_card(v2, m)
        await message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("pv:rename:"))
async def pv_rename_start(cb: CallbackQuery, state: FSMContext) -> None:
    if is_dealer_mode():
        await cb.answer("Недоступно", show_alert=False)
        return
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
        reply_markup=main_menu_kb(),
    )


@router.message(PayAdminStates.waiting_variant_rename)
async def pv_rename_save(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name or len(name) > 64:
        await message.answer("Название не должно быть пустым или длиннее 64 символов. Ещё раз или /cancel.", reply_markup=main_menu_kb())
        return
    data = await state.get_data()
    v_id = data.get("v_id")
    await state.clear()
    if not v_id:
        await message.answer("Ввод сброшен. Начните заново — /pay.", reply_markup=main_menu_kb())
        return
    async with SessionLocal() as session:
        v = await session.get(PaymentVariant, int(v_id))
        if not v:
            await message.answer("Вид не найден.", reply_markup=main_menu_kb())
            return
        v.name = name
        method_id = v.method_id
        await session.commit()
    await message.answer(f"✅ Вид переименован: {name}", reply_markup=main_menu_kb())
    v2 = await get_payment_variant(int(v_id))
    m = await get_payment_method(method_id)
    if v2 and m:
        text, kb = await _variant_card(v2, m)
        await message.answer(text, reply_markup=kb)


# ====== Отправка ключа дилеру (админ) ======

class AdminKeyToDealerStates(StatesGroup):
    waiting_userid = State()
    waiting_username = State()
    waiting_keycode = State()


@router.callback_query(F.data == "dkey:start")
async def dealer_key_start(cb: CallbackQuery) -> None:
    if is_dealer_mode():
        await cb.answer("Недоступно", show_alert=False)
        return
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
    if is_dealer_mode():
        await cb.answer("Недоступно", show_alert=False)
        return
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
        reply_markup=main_menu_kb(),
    )


@router.message(AdminKeyToDealerStates.waiting_userid)
async def dealer_key_userid(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer("USERID должен быть числом. Ещё раз или /cancel.", reply_markup=main_menu_kb())
        return
    await state.update_data(key_uid=text)
    await state.set_state(AdminKeyToDealerStates.waiting_username)
    await message.answer("Шаг 2/3. Введите USERNAME клиента.", reply_markup=main_menu_kb())


@router.message(AdminKeyToDealerStates.waiting_username)
async def dealer_key_username(message: Message, state: FSMContext) -> None:
    username = (message.text or "").strip()
    if not username or len(username) > 128:
        await message.answer("USERNAME не должно быть пустым и длиннее 128 символов. Ещё раз или /cancel.", reply_markup=main_menu_kb())
        return
    await state.update_data(key_uname=username)
    await state.set_state(AdminKeyToDealerStates.waiting_keycode)
    await message.answer(
        "Шаг 3/3. Вставьте код ключа (любая длина).",
        reply_markup=main_menu_kb(),
    )


@router.message(AdminKeyToDealerStates.waiting_keycode)
async def dealer_key_send(message: Message, state: FSMContext, bot: Bot) -> None:
    code_text = (message.text or "").strip()
    if not code_text:
        await message.answer("Код ключа не может быть пустым. Ещё раз или /cancel.", reply_markup=main_menu_kb())
        return
    data = await state.get_data()
    target_code = data.get("target_code")
    uid = data.get("key_uid")
    uname = data.get("key_uname")
    await state.clear()
    if not target_code or not uid or not uname:
        await message.answer("Ввод сброшен. Начните заново через /dealers.", reply_markup=main_menu_kb())
        return
    d = await get_dealer(target_code)
    if not d or d.chat_id is None:
        await message.answer("Дилер не найден или нет Telegram ID.", reply_markup=main_menu_kb())
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
            reply_markup=main_menu_kb(),
        )
        return
    except Exception as e:
        await message.answer(f"❌ Ошибка отправки: {e}", reply_markup=main_menu_kb())
        return
    await message.answer(
        f"✅ Ключ отправлен дилеру «{d.title}» (USERID {uid}, USERNAME {uname}).",
        reply_markup=main_menu_kb(),
    )


# ====== Выполнение заказа дилера (админ) ======

class OrderFulfillStates(StatesGroup):
    waiting_user_id = State()
    waiting_username = State()
    waiting_due = State()
    waiting_key_code = State()
    waiting_confirm = State()


@router.callback_query(F.data.startswith("oful:") & ~F.data.in_(["oful:ok", "oful:cancel"]))
async def order_fulfill_start(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    oid = cb.data.split(":", 1)[1]
    order = _pending_orders.get(oid)
    if not order:
        await cb.message.answer("Заказ не найден или уже выполнен.", reply_markup=main_menu_kb())
        return
    idx = order["fulfilled"]
    names = order["names"]
    if idx >= len(names):
        await cb.message.answer("Все ключи из этого заказа уже выполнены.", reply_markup=main_menu_kb())
        return
    client_name = names[idx]
    total = len(names)
    await state.clear()
    await state.update_data(order_id=oid, key_index=idx)
    await state.set_state(OrderFulfillStates.waiting_user_id)
    await cb.message.answer(
        f"🔑 Ключ {idx + 1}/{total} — клиент: {client_name}\n\n"
        f"Введите USERID:",
        reply_markup=main_menu_kb(),
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
    order = _pending_orders.get(oid)
    if not order:
        await state.clear()
        await message.answer("Заказ не найден.", reply_markup=main_menu_kb())
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
    order = _pending_orders.get(oid)
    if not order:
        await state.clear()
        await cb.message.answer("Заказ не найден.", reply_markup=main_menu_kb())
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
    order["fulfilled"] = idx + 1
    await state.clear()

    bal_str = f"${new_bal:.2f}" if new_bal is not None else "?"
    await cb.message.answer(
        f"✅ Ключ {idx + 1}/{len(order['names'])} выполнен!\n"
        f"Клиент: {client_name}, USERID={uid}\n"
        f"Баланс дилера: {bal_str}",
        reply_markup=main_menu_kb(),
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
        del _pending_orders[oid]
        await cb.message.answer("🎉 Все ключи из заказа выполнены!", reply_markup=main_menu_kb())


@router.callback_query(F.data == "oful:cancel", OrderFulfillStates.waiting_confirm)
async def oful_cancel(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await state.clear()
    await cb.message.answer("Отменено. Заказ остаётся в очереди.", reply_markup=main_menu_kb())


# ====== Бэкап базы данных (только админ) ======

BACKUP_DIR = Path("./data/backups")


class BackupStates(StatesGroup):
    waiting_restore_file = State()
    waiting_restore_confirm = State()


def backup_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 Создать бэкап", callback_data="backup:create")],
        [InlineKeyboardButton(text="📥 Восстановить из бэкапа", callback_data="backup:restore")],
        [InlineKeyboardButton(text="📋 Список бэкапов", callback_data="backup:list")],
    ])


@router.message(Command("backup"))
@router.message(F.text.in_(["/backup", "💾 Бэкап"]))
async def backup_home(message: Message, state: FSMContext) -> None:
    if not ensure_allowed_user(message):
        return
    if is_dealer_mode():
        await message.answer(ensure_admin_only(), reply_markup=main_menu_kb())
        return
    await state.clear()
    await message.answer("💾 Бэкап базы данных\n\nВыберите действие:", reply_markup=backup_menu_kb())


@router.callback_query(F.data == "backup:home")
async def backup_home_cb(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await state.clear()
    await cb.message.answer("💾 Бэкап базы данных\n\nВыберите действие:", reply_markup=backup_menu_kb())


def _find_db_path() -> "Path | None":
    """Найти файл БД: пробуем несколько вариантов пути."""
    candidates = [
        Path("/app/data/data.db"),       # абсолютный путь в Docker
        Path("./data/data.db"),          # относительный (WORKDIR /app)
        Path("data/data.db"),            # без ./
    ]
    # Также пробуем извлечь путь из DATABASE_URL
    url = os.environ.get("DATABASE_URL", "")
    if ":///" in url:
        raw = url.split("///", 1)[1].split("?")[0]
        candidates.insert(0, Path(raw))
    for p in candidates:
        if p.exists():
            return p
    return None


@router.callback_query(F.data == "backup:create")
async def backup_create(cb: CallbackQuery) -> None:
    await cb.answer("Создаю бэкап…")
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    ts = now_tz().strftime("%Y%m%d_%H%M%S")
    zip_name = f"xmplus_backup_{ts}.zip"
    zip_path = BACKUP_DIR / zip_name

    db_path = _find_db_path()
    tz_path = Path("/app/.tz_override")

    if not db_path:
        await cb.message.answer(
            "❌ Файл базы данных не найден!\n"
            f"DATABASE_URL: {os.environ.get('DATABASE_URL', '(не задан)')}\n"
            f"CWD: {Path.cwd()}",
            reply_markup=backup_menu_kb(),
        )
        return

    try:
        # Сохраняем переменные окружения (.env)
        env_keys = (
            "BOT_TOKEN", "OWNER_CHAT_ID", "BOT_MODE", "DEALER_NAME",
            "TIMEZONE", "CHECK_INTERVAL_MINUTES", "PRE_NOTIFY_HOURS",
            "NOTIFY_EVERY_MINUTES", "MAX_NOTIFICATIONS", "DATABASE_URL",
        )
        env_lines = [f"{k}={os.environ[k]}" for k in env_keys if k in os.environ]
        env_content = "\n".join(env_lines) + "\n"

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(db_path, "data/data.db")
            zf.writestr(".env", env_content)
            if tz_path.exists():
                zf.write(tz_path, ".tz_override")

        # Проверяем что data.db действительно попало в архив
        with zipfile.ZipFile(zip_path, "r") as zf:
            names = zf.namelist()
        if "data/data.db" not in names:
            await cb.message.answer(
                f"❌ Ошибка: data/data.db не в архиве.\nСодержимое: {names}",
                reply_markup=backup_menu_kb(),
            )
            return

        size_kb = zip_path.stat().st_size / 1024
        with open(zip_path, "rb") as f:
            doc = BufferedInputFile(f.read(), filename=zip_name)
        await cb.message.answer_document(
            doc,
            caption=(
                f"📦 Бэкап создан: {zip_name}\n"
                f"Размер: {size_kb:.1f} KB\n"
                f"Содержимое: {', '.join(names)}"
            ),
        )
        await cb.message.answer(
            "Бэкап сохранён на сервере и отправлен вам.",
            reply_markup=backup_menu_kb(),
        )
    except Exception as e:
        await cb.message.answer(f"❌ Ошибка создания бэкапа: {e}", reply_markup=backup_menu_kb())


@router.callback_query(F.data == "backup:restore")
async def backup_restore_start(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await state.clear()
    await state.set_state(BackupStates.waiting_restore_file)
    await cb.message.answer(
        "📥 Восстановление из бэкапа\n\n"
        "Отправьте ZIP-архив бэкапа.\n"
        "⚠️ Текущая база данных будет заменена!\n\n"
        "Отмена — /cancel",
        reply_markup=main_menu_kb(),
    )


@router.message(BackupStates.waiting_restore_file, F.document)
async def backup_restore_got_file(message: Message, state: FSMContext, bot: Bot) -> None:
    doc = message.document
    if not doc.file_name or not doc.file_name.lower().endswith(".zip"):
        await message.answer(
            "Отправьте ZIP-архив (.zip). Попробуйте ещё раз или /cancel.",
            reply_markup=main_menu_kb(),
        )
        return

    await message.answer("⏳ Загружаю архив…")

    try:
        file_obj = await bot.get_file(doc.file_id)
        bio = await bot.download_file(file_obj.file_path)

        tmp_zip = Path("./data/_restore_tmp.zip")
        tmp_zip.write_bytes(bio.read())

        with zipfile.ZipFile(tmp_zip, "r") as zf:
            names = zf.namelist()

        if "data/data.db" not in names:
            tmp_zip.unlink(missing_ok=True)
            await state.clear()
            await message.answer(
                "❌ Архив не содержит data/data.db — это не бэкап XMPLUS.",
                reply_markup=backup_menu_kb(),
            )
            return

        contents = ", ".join(names)
        await state.update_data(restore_zip=str(tmp_zip))
        await state.set_state(BackupStates.waiting_restore_confirm)
        await message.answer(
            f"Архив: {doc.file_name}\n"
            f"Содержимое: {contents}\n\n"
            "⚠️ Текущая база данных будет заменена!\n"
            "Подтвердите восстановление:",
            reply_markup=confirm_kb(),
        )
    except zipfile.BadZipFile:
        Path("./data/_restore_tmp.zip").unlink(missing_ok=True)
        await state.clear()
        await message.answer("❌ Файл повреждён или не является ZIP.", reply_markup=backup_menu_kb())
    except Exception as e:
        Path("./data/_restore_tmp.zip").unlink(missing_ok=True)
        await state.clear()
        await message.answer(f"❌ Ошибка: {e}", reply_markup=backup_menu_kb())


@router.message(BackupStates.waiting_restore_file)
async def backup_restore_not_file(message: Message) -> None:
    await message.answer(
        "Отправьте файл (ZIP-архив). Или /cancel для отмены.",
        reply_markup=main_menu_kb(),
    )


@router.message(BackupStates.waiting_restore_confirm)
async def backup_restore_confirm(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip().lower()
    data = await state.get_data()
    tmp_zip_str = data.get("restore_zip", "")
    tmp_zip = Path(tmp_zip_str) if tmp_zip_str else None

    if text not in ("✅ подтвердить", "подтвердить", "да", "ok", "ок"):
        if tmp_zip:
            tmp_zip.unlink(missing_ok=True)
        await state.clear()
        await message.answer("Восстановление отменено.", reply_markup=main_menu_kb())
        return

    if not tmp_zip or not tmp_zip.exists():
        await state.clear()
        await message.answer(
            "❌ Временный файл не найден. Начните заново.",
            reply_markup=backup_menu_kb(),
        )
        return

    try:
        db_path = Path("./data/data.db")

        # Бэкап текущей базы перед заменой
        if db_path.exists():
            safe_ts = now_tz().strftime("%Y%m%d_%H%M%S")
            shutil.copy2(db_path, Path(f"./data/data.db.pre_restore_{safe_ts}"))

        # Закрываем соединения с БД
        await engine.dispose()

        # Распаковка
        with zipfile.ZipFile(tmp_zip, "r") as zf:
            if "data/data.db" in zf.namelist():
                zf.extract("data/data.db", ".")
            if ".tz_override" in zf.namelist():
                tz_data = zf.read(".tz_override")
                Path("/app/.tz_override").write_bytes(tz_data)

        tmp_zip.unlink(missing_ok=True)
        await state.clear()
        await message.answer(
            "✅ База данных восстановлена!\n"
            "Старая база сохранена как резерв.\n\n"
            "⚠️ Перезапустите бота для полного применения:\n"
            "<code>cd /opt/xmplus && docker compose restart</code>",
            parse_mode="HTML",
            reply_markup=main_menu_kb(),
        )
    except Exception as e:
        if tmp_zip:
            tmp_zip.unlink(missing_ok=True)
        await state.clear()
        await message.answer(f"❌ Ошибка восстановления: {e}", reply_markup=main_menu_kb())


# --- Список бэкапов ---

@router.callback_query(F.data == "backup:list")
async def backup_list_show(cb: CallbackQuery) -> None:
    await cb.answer()
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    files = sorted(BACKUP_DIR.glob("xmplus_backup_*.zip"), reverse=True)
    if not files:
        await cb.message.answer(
            "📋 Список бэкапов пуст.\nСоздайте первый бэкап.",
            reply_markup=backup_menu_kb(),
        )
        return

    rows: list[list[InlineKeyboardButton]] = []
    for f in files:
        size_kb = f.stat().st_size / 1024
        ts = f.stem.replace("xmplus_backup_", "")
        label = f"{ts} ({size_kb:.0f} KB)"
        rows.append([
            InlineKeyboardButton(text=f"📦 {label}", callback_data=f"backup:dl:{ts}"),
            InlineKeyboardButton(text="🗑", callback_data=f"backup:rm:{ts}"),
        ])
    rows.append([InlineKeyboardButton(text="◀ Назад", callback_data="backup:home")])

    await cb.message.answer(
        f"📋 Бэкапов на сервере: {len(files)}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("backup:dl:"))
async def backup_download(cb: CallbackQuery) -> None:
    await cb.answer()
    ts = cb.data.split(":", 2)[-1]
    zip_name = f"xmplus_backup_{ts}.zip"
    zip_path = BACKUP_DIR / zip_name

    if not zip_path.exists():
        await cb.message.answer("Файл не найден.", reply_markup=backup_menu_kb())
        return

    try:
        with open(zip_path, "rb") as f:
            doc = BufferedInputFile(f.read(), filename=zip_name)
        size_kb = zip_path.stat().st_size / 1024
        await cb.message.answer_document(doc, caption=f"📦 {zip_name} ({size_kb:.1f} KB)")
    except Exception as e:
        await cb.message.answer(f"❌ Ошибка: {e}", reply_markup=backup_menu_kb())


@router.callback_query(F.data.startswith("backup:rm:"))
async def backup_delete_ask(cb: CallbackQuery) -> None:
    await cb.answer()
    ts = cb.data.split(":", 2)[-1]
    zip_name = f"xmplus_backup_{ts}.zip"
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Удалить", callback_data=f"backup:rmok:{ts}"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="backup:list"),
    ]])
    await cb.message.answer(f"Удалить бэкап {zip_name}?", reply_markup=kb)


@router.callback_query(F.data.startswith("backup:rmok:"))
async def backup_delete_exec(cb: CallbackQuery) -> None:
    await cb.answer()
    ts = cb.data.split(":", 2)[-1]
    zip_name = f"xmplus_backup_{ts}.zip"
    zip_path = BACKUP_DIR / zip_name

    if zip_path.exists():
        zip_path.unlink()
        await cb.message.answer(f"🗑 {zip_name} удалён.")
    else:
        await cb.message.answer("Файл уже удалён.")

    # Обновляем список
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(BACKUP_DIR.glob("xmplus_backup_*.zip"), reverse=True)
    if files:
        rows: list[list[InlineKeyboardButton]] = []
        for f in files:
            size_kb = f.stat().st_size / 1024
            ts2 = f.stem.replace("xmplus_backup_", "")
            label = f"{ts2} ({size_kb:.0f} KB)"
            rows.append([
                InlineKeyboardButton(text=f"📦 {label}", callback_data=f"backup:dl:{ts2}"),
                InlineKeyboardButton(text="🗑", callback_data=f"backup:rm:{ts2}"),
            ])
        rows.append([InlineKeyboardButton(text="◀ Назад", callback_data="backup:home")])
        await cb.message.answer(
            f"📋 Бэкапов на сервере: {len(files)}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
    else:
        await cb.message.answer("📋 Список бэкапов пуст.", reply_markup=backup_menu_kb())
