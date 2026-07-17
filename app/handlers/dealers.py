from __future__ import annotations

import logging, csv, io, html
from typing import List

from aiogram import Router, Bot, F
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile,
)
from aiogram.fsm.context import FSMContext
from sqlalchemy import select, delete

from app.db import SessionLocal, Item, Dealer
from app.config import settings
from app.states import DealerAssignStates, AddDealerStates, MsgDealerStates, BroadcastStates
from app.keyboards import main_menu_kb
from app.utils import fmt_dt_human, now_tz, to_tz
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest
from app.bot import split_text_chunks, send_pre_chunk, make_table_lines_without_id, build_items_csv_bytes

log = logging.getLogger(__name__)

router = Router()

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
    await state.clear()
    text = await dealers_counts_text()
    await message.answer(text, reply_markup=await dealers_menu_kb())

@router.callback_query(F.data.startswith("dealers:view:"))
async def dealers_view(cb: CallbackQuery) -> None:
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

@router.callback_query(F.data == "dealers:assign:start")
async def dealers_assign_start(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await state.clear()
    await state.set_state(DealerAssignStates.waiting_ids)
    await cb.message.answer(
        "Отправьте список USERID через запятую/пробел/новую строку.\n"
        "Пример: 1323, 2005, 1383\n"
        "После этого предложу выбрать дилера.",
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

@router.callback_query(F.data == "dealers:add:start")
async def dealer_add_start(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await state.clear()
    await state.set_state(AddDealerStates.waiting_code)
    await cb.message.answer(
        "Добавление дилера.\n"
        "Шаг 1/3. Введите КОД дилера — латиницей, без пробелов "
        "(буквы, цифры, _ ; 2–32 символа). Например: vasya\n"
        "Если такой код уже есть — данные дилера будут обновлены.\n\n"
        "Отмена — /cancel",
    )


@router.message(AddDealerStates.waiting_code)
async def dealer_add_code(message: Message, state: FSMContext) -> None:
    code = (message.text or "").strip().lower()
    if code == MAIN_CODE:
        await message.answer("Код «main» зарезервирован системой. Введите другой код или /cancel.")
        return
    if not DEALER_CODE_RE.match(code):
        await message.answer(
            "Неверный код. Разрешены латинские буквы, цифры и _ (2–32 символа), без пробелов.\n"
            "Попробуйте ещё раз или /cancel.",
        )
        return
    await state.update_data(code=code)
    await state.set_state(AddDealerStates.waiting_title)
    await message.answer(
        "Шаг 2/3. Введите НАЗВАНИЕ дилера — как показывать в меню (например: Вася):",
    )


@router.message(AddDealerStates.waiting_title)
async def dealer_add_title(message: Message, state: FSMContext) -> None:
    title = (message.text or "").strip()
    if not title or len(title) > 64:
        await message.answer("Название не может быть пустым или длиннее 64 символов. Ещё раз или /cancel.")
        return
    await state.update_data(title=title)
    await state.set_state(AddDealerStates.waiting_chat_id)
    await message.answer(
        "Шаг 3/3. Введите Telegram ID дилера (число) — на него бот будет отправлять сообщения.\n"
        "Если ID пока неизвестен — отправьте «-» (можно задать позже, добавив дилера с тем же кодом).",
    )


@router.message(AddDealerStates.waiting_chat_id)
async def dealer_add_chat_id(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    chat_id = None
    if raw not in ("-", "—"):
        digits = raw[1:] if raw.startswith("-") else raw
        if not digits.isdigit():
            await message.answer("Telegram ID должен быть числом, либо «-» чтобы пропустить. Ещё раз или /cancel.")
            return
        chat_id = int(raw)
    data = await state.get_data()
    code = data.get("code")
    title = data.get("title")
    if not code or not title:
        await state.clear()
        await message.answer("Ввод сброшен. Начните заново через /dealers.")
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
    )
    await message.answer(await dealers_counts_text(), reply_markup=await dealers_menu_kb())


# ===== Удаление дилера (только админ) =====

@router.callback_query(F.data == "dealers:del:start")
async def dealer_del_start(cb: CallbackQuery) -> None:
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

@router.callback_query(F.data == "dealers:msg:start")
async def dealer_msg_start(cb: CallbackQuery) -> None:
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
    )


@router.message(MsgDealerStates.waiting_text)
async def dealer_msg_send(message: Message, state: FSMContext, bot: Bot) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Пустое сообщение. Введите текст или /cancel.")
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
    )


@router.message(BroadcastStates.waiting_text)
async def dealer_broadcast_send(message: Message, state: FSMContext, bot: Bot) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Пустое сообщение. Введите текст или /cancel.")
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

