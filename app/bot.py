from __future__ import annotations

from datetime import datetime, timezone, timedelta
import csv, io, html, re, calendar
from typing import List

from aiogram import Router, Bot, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    CallbackQuery,
    BotCommand,
    BotCommandScopeDefault,
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

from sqlalchemy import select, delete

from app.db import SessionLocal, Item
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
    BotCommand(command="status", description="Статус бота"),
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
    BotCommand(command="status", description="Статус"),
]

def is_dealer_mode() -> bool:
    return settings.BOT_MODE == "dealer"

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
            [KeyboardButton(text="/add"), KeyboardButton(text="/renew")],
            [KeyboardButton(text="/list"), KeyboardButton(text="/disabled")],
            [KeyboardButton(text="/next"), KeyboardButton(text="/status")],
            [KeyboardButton(text="/delete"), KeyboardButton(text="/help")],
            [KeyboardButton(text="/dealers"), KeyboardButton(text="/timezone")],
            [KeyboardButton(text="/cancel"), KeyboardButton(text="/hide")],
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
    w.writerow(["user_id", "username", "due_date"])
    for it in items:
        w.writerow([it.user_id, it.username, fmt_dt_human(it.due_date)])
    data = buf.getvalue().encode("utf-8")
    buf.close()
    return data

# Вариант A: фиксированные ширины колонок
UID_W = 5
UNAME_W = 8

def _trunc(s: str, width: int) -> str:
    return s if len(s) <= width else (s[: max(0, width - 1)] + "…")

def make_table_lines_without_id(items) -> tuple[str, list[str]]:
    header = f"{'USERID'.rjust(UID_W)} | {'USERNAME'.ljust(UNAME_W)} | DUE DATE"
    rows: list[str] = []
    for it in items:
        uid = str(it.user_id).rjust(UID_W)
        uname = _trunc(it.username, UNAME_W).ljust(UNAME_W)
        due = fmt_dt_human(it.due_date)
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
@router.message(F.text == "/help")
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
@router.message(F.text == "/hide")
async def hide_menu(message: Message) -> None:
    if not ensure_allowed_user(message):
        return
    await message.answer("Клавиатура скрыта.", reply_markup=ReplyKeyboardRemove())

@router.message(Command("status"))
@router.message(F.text == "/status")
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
@router.message(F.text == "/timezone")
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

@router.message(Command("cancel"))
@router.message(F.text == "/cancel")
async def on_cancel(message: Message, state: FSMContext) -> None:
    if not ensure_allowed_user(message):
        return
    if is_dealer_mode():
        await message.answer(ensure_admin_only(), reply_markup=main_menu_kb())
        return
    await state.clear()
    await message.answer("Отменено.", reply_markup=main_menu_kb())

@router.message(Command("add"))
@router.message(F.text == "/add")
async def add_start(message: Message, state: FSMContext) -> None:
    if not ensure_allowed_user(message):
        return
    if is_dealer_mode():
        await message.answer(ensure_admin_only(), reply_markup=main_menu_kb())
        return
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
        item = Item(user_id=user_id, username=username, due_date=dt, chat_id=message.chat.id)
        session.add(item)
        await session.commit()
        await session.refresh(item)
    await state.clear()
    await message.answer(
        f"Добавлено: [{item.id}] USERID={user_id}, USERNAME={username}, DUE={fmt_dt_human(dt)}",
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
@router.message(F.text == "/renew")
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
async def renew_confirm(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip().lower()
    if text not in ("✅ подтвердить", "подтвердить", "да", "ok", "ок"):
        await state.clear()
        await message.answer("Отменено.", reply_markup=main_menu_kb())
        return
    data = await state.get_data()
    item_id = int(data["item_id"])
    new_due_str = data["new_due"]
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
        await session.commit()
    await state.clear()
    await message.answer(
        f"✅ Продлено: USERID={data['user_id']}, USERNAME={data['username']}\nНовая дата DUE={new_due_str}",
        reply_markup=main_menu_kb(),
    )

# ==== Удаление — только админ ====

class DeleteStates(StatesGroup):
    waiting_userid = State()
    waiting_confirm = State()

@router.message(Command("delete"))
@router.message(F.text == "/delete")
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

# ==== Списки ====

@router.message(Command("list"))
@router.message(F.text == "/list")
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
@router.message(F.text == "/disabled")
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
@router.message(F.text == "/next")
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

DEALER_CODES = ["serdar", "ilya", "main"]
DEALER_TITLES = {"serdar": "Сердар", "ilya": "Иля", "main": "Без дилера"}

def dealers_menu_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="👁 Сердар", callback_data="dealers:view:serdar"),
         InlineKeyboardButton(text="⬇️ CSV Сердар", callback_data="dealers:export:serdar")],
        [InlineKeyboardButton(text="👁 Иля", callback_data="dealers:view:ilya"),
         InlineKeyboardButton(text="⬇️ CSV Иля", callback_data="dealers:export:ilya")],
        [InlineKeyboardButton(text="👁 Без дилера", callback_data="dealers:view:main"),
         InlineKeyboardButton(text="⬇️ CSV Без дилера", callback_data="dealers:export:main")],
        [InlineKeyboardButton(text="📝 Назначить по списку USERID → дилер", callback_data="dealers:assign:start")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def dealers_counts_text() -> str:
    async with SessionLocal() as session:
        rows = (await session.execute(select(Item.dealer))).all()
        counts = {"serdar": 0, "ilya": 0, "main": 0}
        for (d,) in rows:
            if d not in counts:
                counts["main"] += 1 if d is None else 0
            else:
                counts[d] += 1
    return (
        "Раздел диллеры:\n"
        f"- Сердар: {counts.get('serdar', 0)}\n"
        f"- Иля: {counts.get('ilya', 0)}\n"
        f"- Без дилера: {counts.get('main', 0)}\n\n"
        "Выберите действие:"
    )

@router.message(Command("dealers"))
@router.message(F.text == "/dealers")
async def dealers_home(message: Message, state: FSMContext) -> None:
    if not ensure_allowed_user(message):
        return
    if is_dealer_mode():
        await message.answer(ensure_admin_only(), reply_markup=main_menu_kb())
        return
    await state.clear()
    text = await dealers_counts_text()
    await message.answer(text, reply_markup=dealers_menu_kb())

@router.callback_query(F.data.startswith("dealers:view:"))
async def dealers_view(cb: CallbackQuery) -> None:
    if is_dealer_mode():
        await cb.answer("Недоступно", show_alert=False)
        return
    await cb.answer()
    dealer = cb.data.split(":")[-1]
    if dealer not in DEALER_TITLES:
        await cb.message.answer("Неизвестный дилер.")
        return
    async with SessionLocal() as session:
        q = select(Item).where(Item.dealer == dealer).order_by(Item.due_date.asc())
        items = (await session.execute(q)).scalars().all()
    title = DEALER_TITLES[dealer]
    if not items:
        await cb.message.answer(f"{title}: записей нет.", reply_markup=dealers_menu_kb())
        return
    header, lines = make_table_lines_without_id(items)
    header = f"{title}:\n" + "-" * 40 + "\n" + header
    chunks = split_text_chunks(header, lines)
    for i, ch in enumerate(chunks, 1):
        suffix = f"\n(стр. {i}/{len(chunks)})" if len(chunks) > 1 else ""
        await send_pre_chunk(cb.message, ch + suffix)
    await cb.message.answer(f"Всего записей ({title}): {len(items)}", reply_markup=dealers_menu_kb())

@router.callback_query(F.data.startswith("dealers:export:"))
async def dealers_export(cb: CallbackQuery) -> None:
    if is_dealer_mode():
        await cb.answer("Недоступно", show_alert=False)
        return
    await cb.answer()
    dealer = cb.data.split(":")[-1]
    if dealer not in DEALER_TITLES:
        await cb.message.answer("Неизвестный дилер.")
        return
    async with SessionLocal() as session:
        q = select(Item).where(Item.dealer == dealer).order_by(Item.due_date.asc())
        items = (await session.execute(q)).scalars().all()
    data = await build_items_csv_bytes(items)
    title = DEALER_TITLES[dealer]
    fname = f"export_{dealer}.csv"
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

def dealers_pick_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Назначить → Сердар", callback_data="dealers:assign:pick:serdar")],
        [InlineKeyboardButton(text="Назначить → Иля", callback_data="dealers:assign:pick:ilya")],
        [InlineKeyboardButton(text="Назначить → Без дилера", callback_data="dealers:assign:pick:main")],
    ])

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
        reply_markup=dealers_pick_kb(),
    )

@router.callback_query(F.data.startswith("dealers:assign:pick:"))
async def dealers_assign_pick(cb: CallbackQuery, state: FSMContext) -> None:
    if is_dealer_mode():
        await cb.answer("Недоступно", show_alert=False)
        return
    await cb.answer()
    dealer = cb.data.split(":")[-1]
    if dealer not in DEALER_TITLES:
        await cb.message.answer("Неизвестный дилер.")
        return
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
            if it.dealer != dealer:
                it.dealer = dealer
                changed += 1
        await session.commit()

    await state.clear()
    title = DEALER_TITLES[dealer]
    await cb.message.answer(
        f"Готово. Передано дилеру: {title}\n"
        f"- USERID в запросе: {len(ids)}\n"
        f"- Найдено записей: {found}\n"
        f"- Обновлено (изменён dealer): {changed}\n",
        reply_markup=dealers_menu_kb(),
    )

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