from __future__ import annotations

from datetime import datetime, timezone

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

# Команды для меню Telegram
BOT_COMMANDS = [
    BotCommand(command="start", description="Запуск бота"),
    BotCommand(command="help", description="Справка по командам"),
    BotCommand(command="add", description="Добавить (мастер: USERID → USERNAME → дата/время)"),
    BotCommand(command="renew", description="Продлить по USERID"),
    BotCommand(command="delete", description="Удалить по USERID (с подтверждением)"),
    BotCommand(command="list", description="Список (отсортировано по дате)"),
    BotCommand(command="disabled", description="Список отключённых (просроченных)"),
    BotCommand(command="next", description="Ближайшие истечения"),
    BotCommand(command="status", description="Статус бота"),
    BotCommand(command="timezone", description="Показать/сменить локальное время (TZ)"),
    BotCommand(command="cancel", description="Отменить текущий ввод"),
    BotCommand(command="menu", description="Показать клавиатуру"),
    BotCommand(command="hide", description="Скрыть клавиатуру"),
]


def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="/add"), KeyboardButton(text="/renew")],
            [KeyboardButton(text="/list"), KeyboardButton(text="/disabled")],
            [KeyboardButton(text="/next"), KeyboardButton(text="/status")],
            [KeyboardButton(text="/delete"), KeyboardButton(text="/help")],
            [KeyboardButton(text="/timezone"), KeyboardButton(text="/cancel")],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выберите команду…",
        selective=True,
    )


def confirm_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="✅ Подтвердить"), KeyboardButton(text="❌ Отмена")],
        ],
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


def date_copy_kb(date_str: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📤 Отправить дату", callback_data=f"send_date:{date_str}")],
        [InlineKeyboardButton(text="📎 Вставить дату в поле", switch_inline_query_current_chat=date_str)],
    ])


async def set_bot_commands(bot: Bot) -> None:
    await bot.set_my_commands(commands=BOT_COMMANDS, scope=BotCommandScopeDefault())
    try:
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    except Exception:
        pass


@router.message(CommandStart())
@router.message(F.text == "/start")
async def on_start(message: Message) -> None:
    await message.answer(
        "✅ XMPLUS запущен.\n"
        "Команды — в меню (кнопка с квадратами) и на клавиатуре ниже.",
        reply_markup=main_menu_kb(),
    )


@router.message(Command("help"))
@router.message(F.text == "/help")
async def on_help(message: Message) -> None:
    text = (
        "Доступные команды:\n"
        + "\n".join([f"/{c.command} — {c.description}" for c in BOT_COMMANDS])
        + "\n\nПодсказка: /menu — показать клавиатуру, /hide — скрыть."
    )
    await message.answer(text, reply_markup=main_menu_kb())


@router.message(Command("menu"))
@router.message(F.text == "/menu")
async def show_menu(message: Message) -> None:
    await message.answer("Клавиатура показана.", reply_markup=main_menu_kb())


@router.message(Command("hide"))
@router.message(F.text == "/hide")
async def hide_menu(message: Message) -> None:
    await message.answer("Клавиатура скрыта.", reply_markup=ReplyKeyboardRemove())


@router.message(Command("status"))
@router.message(F.text == "/status")
async def on_status(message: Message) -> None:
    async with SessionLocal() as session:
        total = (await session.execute(select(Item))).scalars().unique().all()
    await message.answer(
        f"Бот работает ✅\nВ базе записей: {len(total)}\nACTIVE_TZ: {get_active_timezone_name()} (UTC{tz_offset_str()})",
        reply_markup=main_menu_kb(),
    )


# ==== Таймзона: показ и переключение ====

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
    await cb.answer()
    tz_name = cb.data.split(":", 2)[-1]
    ok = set_active_timezone_name(tz_name)
    if ok:
        await cb.message.answer(f"✅ Часовой пояс установлен: {tz_name} (UTC{tz_offset_str()})")
    else:
        await cb.message.answer("❌ Не удалось установить часовой пояс. Проверьте логи.")


# ==== Мастер добавления ====

class AddStates(StatesGroup):
    waiting_user_id = State()
    waiting_username = State()
    waiting_duedatetime = State()


@router.message(Command("cancel"))
@router.message(F.text == "/cancel")
async def on_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.", reply_markup=main_menu_kb())


@router.message(Command("add"))
@router.message(F.text == "/add")
async def add_start(message: Message, state: FSMContext) -> None:
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
        f"Добавлено: [{item.id}] USERID={user_id}, USERNAME={username}, DUE={fmt_dt_human(dt)}",
        reply_markup=main_menu_kb(),
    )


# ==== Продление по USERID (/renew) ====

class RenewStates(StatesGroup):
    waiting_userid = State()
    waiting_new_due = State()
    waiting_confirm = State()


@router.message(Command("renew"))
@router.message(F.text == "/renew")
async def renew_start(message: Message, state: FSMContext) -> None:
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
        await message.answer(
            "Клиент:\n"
            f"USERID: {it.user_id}\n"
            f"USERNAME: {it.username}\n"
            f"Текущая дата отключения: {fmt_dt_human(it.due_date)}",
            reply_markup=date_copy_kb(fmt_dt_human(it.due_date)),
        )
        await message.answer(
            "Отправьте новую дату в формате:\nYYYY-MM-DD HH:MM:SS",
            reply_markup=main_menu_kb(),
        )
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
    await cb.message.answer(
        "Клиент:\n"
        f"USERID: {it.user_id}\n"
        f"USERNAME: {it.username}\n"
        f"Текущая дата отключения: {fmt_dt_human(it.due_date)}",
        reply_markup=date_copy_kb(fmt_dt_human(it.due_date)),
    )
    await cb.message.answer(
        "Отправьте новую дату в формате:\nYYYY-MM-DD HH:MM:SS",
        reply_markup=main_menu_kb(),
    )


@router.message(RenewStates.waiting_new_due)
async def renew_get_new_due(message: Message, state: FSMContext) -> None:
    s = (message.text or "").strip()
    dt = parse_datetime_human(s)
    if not dt:
        await message.answer(
            "Неверный формат даты. Используйте YYYY-MM-DD HH:MM:SS.\n"
            "Попробуйте ещё раз или /cancel.",
            reply_markup=main_menu_kb(),
        )
        return
    new_due = fmt_dt_human(dt)
    data = await state.get_data()
    await state.update_data(new_due=new_due)
    await state.set_state(RenewStates.waiting_confirm)
    await message.answer(
        "Подтвердите продление:\n"
        f"USERID: {data['user_id']}\n"
        f"USERNAME: {data['username']}\n"
        f"Было: {data['old_due']}\n"
        f"Станет: {new_due}",
        reply_markup=confirm_kb(),
    )


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
        f"✅ Продлено: USERID={data['user_id']}, USERNAME={data['username']}\n"
        f"Новая дата DUE={new_due_str}",
        reply_markup=main_menu_kb(),
    )


@router.callback_query(F.data.startswith("send_date:"))
async def send_date(cb: CallbackQuery) -> None:
    await cb.answer()
    date_str = cb.data.split(":", 1)[1]
    await cb.message.answer(date_str)


# ==== Удаление по USERID (/delete) ====

class DeleteStates(StatesGroup):
    waiting_userid = State()
    waiting_confirm = State()


@router.message(Command("delete"))
@router.message(F.text == "/delete")
async def delete_start(message: Message, state: FSMContext) -> None:
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


# ==== Списки/ближайшие ====

@router.message(Command("list"))
@router.message(F.text == "/list")
async def on_list(message: Message) -> None:
    async with SessionLocal() as session:
        result = await session.execute(select(Item).order_by(Item.due_date.asc()))
        items = result.scalars().all()

    if not items:
        await message.answer("Список пуст.", reply_markup=main_menu_kb())
        return

    lines = [f"[{it.id}] {it.user_id} | {it.username} | {fmt_dt_human(it.due_date)}" for it in items]
    header = "ID | USERID | USERNAME | DUE DATE\n" + "-" * 40
    await message.answer(header + "\n" + "\n".join(lines), reply_markup=main_menu_kb())


@router.message(Command("disabled"))
@router.message(F.text == "/disabled")
async def on_disabled(message: Message) -> None:
    now = now_tz()
    async with SessionLocal() as session:
        result = await session.execute(select(Item).order_by(Item.due_date.asc()))
        items = result.scalars().all()

    expired = [it for it in items if to_tz(it.due_date) <= now]
    if not expired:
        await message.answer("Отключённых (просроченных) нет.", reply_markup=main_menu_kb())
        return

    lines = [f"[{it.id}] {it.user_id} | {it.username} | {fmt_dt_human(it.due_date)}" for it in expired]
    header = "Disabled (просроченные):\n" + "-" * 40
    await message.answer(header + "\n" + "\n".join(lines), reply_markup=main_menu_kb())


@router.message(Command("next"))
@router.message(F.text == "/next")
async def on_next(message: Message) -> None:
    async with SessionLocal() as session:
        result = await session.execute(select(Item).order_by(Item.due_date.asc()).limit(10))
        items = result.scalars().all()

    if not items:
        await message.answer("Нет ближайших истечений.", reply_markup=main_menu_kb())
        return

    lines = [f"[{it.id}] {it.user_id} | {it.username} | {fmt_dt_human(it.due_date)}" for it in items]
    await message.answer("Ближайшие:\n" + "\n".join(lines), reply_markup=main_menu_kb())