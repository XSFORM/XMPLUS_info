from __future__ import annotations

import logging
import re
from collections import defaultdict
from datetime import datetime, timezone

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select, update, func

from app.db import (
    SessionLocal, Item, RouterItem, Service, PriceCategory, IncomeTxn, BalanceTxn,
    MAIN_CODE, get_price,
)
from app.states import PriceStates, CategoryStates
from app.utils import now_tz, to_tz, fmt_dt_human
from app.pricing import (
    DEFAULT_CURRENCY, KIND_TITLES, list_categories, categories_map, price_of,
    fmt_money, log_client_income,
)
from app.services_util import period_len_days

log = logging.getLogger(__name__)

router = Router()

# =====================================================================
#   📊 ДОХОДЫ — категории цен клиентов, ожидаемый и фактический доход
# =====================================================================

KIND_BY_LETTER = {"k": "key", "r": "router"}
MODEL_BY_LETTER = {"k": Item, "r": RouterItem}
MONTHS_RU = ["январь", "февраль", "март", "апрель", "май", "июнь",
             "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]


def _obj_label(k: str, obj) -> str:
    if k == "k":
        parts = [f"🔑 {obj.user_id}", obj.username]
        if obj.note:
            parts.append(obj.note)
    else:
        parts = [f"📡 {obj.client_name}"]
        if obj.note:
            parts.append(obj.note)
    return " • ".join(parts)


def _fmt_totals(totals: dict[str, float]) -> str:
    items = [(c, v) for c, v in totals.items() if abs(v) > 1e-9]
    if not items:
        return "0"
    # TMT первым, остальные по алфавиту
    items.sort(key=lambda x: (x[0] != DEFAULT_CURRENCY, x[0]))
    return " • ".join(("+" if v > 0 else "−") + fmt_money(abs(v), c) for c, v in items)


# ---------------------------------------------------------------------
#   Выбор категории для ключа / роутера
# ---------------------------------------------------------------------

async def category_kb(k: str, oid: int, flag: str) -> InlineKeyboardMarkup:
    """flag: n — новый клиент (запишем доход), e — редактирование, b — массовая простановка."""
    cats = await list_categories(KIND_BY_LETTER[k])
    rows, row = [], []
    for c in cats:
        row.append(InlineKeyboardButton(
            text=f"{c.name} — {fmt_money(c.price, c.currency)}",
            callback_data=f"pc:{k}:{oid}:{c.id}:{flag}",
        ))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="✍️ Своя сумма", callback_data=f"pc:{k}:{oid}:c:{flag}")])
    if flag == "b":
        rows.append([
            InlineKeyboardButton(text="⏭ Пропустить", callback_data=f"pc:{k}:{oid}:s:b"),
            InlineKeyboardButton(text="⏹ Хватит", callback_data="inc:home"),
        ])
    elif flag == "n":
        rows.append([InlineKeyboardButton(text="⏭ Не указывать", callback_data=f"pc:{k}:{oid}:s:n")])
    else:
        rows.append([InlineKeyboardButton(text="🚫 Без категории", callback_data=f"pc:{k}:{oid}:x:e")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def ask_category(message: Message, k: str, oid: int) -> None:
    """Вызывается сразу после добавления нового ключа/роутера."""
    await message.answer("💰 Категория клиента (для учёта дохода)?", reply_markup=await category_kb(k, oid, "n"))


@router.callback_query(F.data.startswith("pcask:"))
async def pc_ask(cb: CallbackQuery) -> None:
    await cb.answer()
    _, k, oid = cb.data.split(":")
    oid = int(oid)
    async with SessionLocal() as session:
        obj = await session.get(MODEL_BY_LETTER[k], oid)
    if not obj:
        await cb.message.answer("Запись не найдена.")
        return
    if k == "k" and obj.dealer and obj.dealer != MAIN_CODE:
        price = await get_price()
        await cb.message.answer(
            f"Это ключ дилера «{obj.dealer}». Доход по нему считается автоматически "
            f"по цене продления ({fmt_money(price, '$')})."
        )
        return
    p = price_of(obj, await categories_map())
    cur = f"{p[2]} — {fmt_money(p[0], p[1])}" if p else "не указана"
    await cb.message.answer(
        f"{_obj_label(k, obj)}\nТекущая категория: {cur}\n\nВыберите:",
        reply_markup=await category_kb(k, oid, "e"),
    )


async def _apply_choice(k: str, oid: int, category_id: int | None, custom: float | None):
    async with SessionLocal() as session:
        obj = await session.get(MODEL_BY_LETTER[k], oid)
        if not obj:
            return None
        obj.category_id = category_id
        obj.custom_price = custom
        await session.commit()
        await session.refresh(obj)
        return obj


async def _after_set(message: Message, k: str, obj, flag: str) -> None:
    p = price_of(obj, await categories_map())
    label = f"{p[2]} — {fmt_money(p[0], p[1])}" if p else "без категории"
    if flag == "n":
        source = "key" if k == "k" else "router"
        ref = f"USERID={obj.user_id}" if k == "k" else obj.client_name
        line = await log_client_income(obj, source, f"Новый: {ref}")
        await message.answer(f"✅ Категория: {label}{line}")
    elif flag == "b":
        await message.answer(f"✅ {_obj_label(k, obj)} → {label}")
        await _bulk_next(message, k, obj.id)
    else:
        await message.answer(f"✅ {_obj_label(k, obj)}\nКатегория: {label}")


@router.callback_query(F.data.startswith("pc:"))
async def pc_choose(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    _, k, oid, choice, flag = cb.data.split(":")
    oid = int(oid)
    if choice == "s":  # пропустить
        if flag == "b":
            await _bulk_next(cb.message, k, oid)
        else:
            await cb.message.answer("Ок, категорию можно указать позже: ✏️ Изменить → 💰 Категория.")
        return
    if choice == "c":
        await state.set_state(PriceStates.waiting_custom)
        await state.update_data(pc_k=k, pc_oid=oid, pc_flag=flag)
        await cb.message.answer(f"Введите сумму в месяц ({DEFAULT_CURRENCY}), например 180:")
        return
    cat_id = None if choice == "x" else int(choice)
    obj = await _apply_choice(k, oid, cat_id, None)
    if not obj:
        await cb.message.answer("Запись не найдена.")
        return
    await _after_set(cb.message, k, obj, flag)


@router.message(PriceStates.waiting_custom)
async def pc_custom(message: Message, state: FSMContext) -> None:
    t = (message.text or "").strip().replace(",", ".")
    m = re.fullmatch(r"\d+(?:\.\d+)?", t)
    if not m:
        await message.answer("Нужно число, например 180. Или /cancel.")
        return
    data = await state.get_data()
    await state.clear()
    k, oid, flag = data["pc_k"], int(data["pc_oid"]), data["pc_flag"]
    obj = await _apply_choice(k, oid, None, float(t))
    if not obj:
        await message.answer("Запись не найдена.")
        return
    await _after_set(message, k, obj, flag)


# ---------------------------------------------------------------------
#   Массовая простановка категорий старым записям
# ---------------------------------------------------------------------

def _no_cat_query(k: str):
    model = MODEL_BY_LETTER[k]
    q = select(model).where(model.category_id.is_(None), model.custom_price.is_(None))
    if k == "k":
        q = q.where(Item.dealer == MAIN_CODE)
    return q


async def _bulk_next(message: Message, k: str, after_id: int) -> None:
    model = MODEL_BY_LETTER[k]
    async with SessionLocal() as session:
        obj = (await session.execute(
            _no_cat_query(k).where(model.id > after_id).order_by(model.id.asc()).limit(1)
        )).scalars().first()
        left = (await session.execute(
            select(func.count()).select_from(_no_cat_query(k).where(model.id > after_id).subquery())
        )).scalar() or 0
    if not obj:
        await message.answer(
            f"🎉 {KIND_TITLES[KIND_BY_LETTER[k]]}: больше нет записей без категории.",
            reply_markup=_home_kb(),
        )
        return
    await message.answer(
        f"{_obj_label(k, obj)}\nДо: {fmt_dt_human(obj.due_date)}\n"
        f"Осталось без категории: {left}",
        reply_markup=await category_kb(k, obj.id, "b"),
    )


@router.callback_query(F.data.startswith("inc:bulk:"))
async def inc_bulk(cb: CallbackQuery) -> None:
    await cb.answer()
    await _bulk_next(cb.message, cb.data.split(":")[2], 0)


# ---------------------------------------------------------------------
#   Отчёт
# ---------------------------------------------------------------------

def _home_kb(month: str | None = None) -> InlineKeyboardMarkup:
    now = now_tz()
    cur = month or now.strftime("%Y-%m")
    y, m = map(int, cur.split("-"))
    prev_m = f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"
    next_m = f"{y + 1}-01" if m == 12 else f"{y}-{m + 1:02d}"
    nav = [InlineKeyboardButton(text="◀ Пред. месяц", callback_data=f"inc:m:{prev_m}")]
    if next_m <= now.strftime("%Y-%m"):
        nav.append(InlineKeyboardButton(text="След. месяц ▶", callback_data=f"inc:m:{next_m}"))
    return InlineKeyboardMarkup(inline_keyboard=[
        nav,
        [
            InlineKeyboardButton(text="✍️ Категории ключам", callback_data="inc:bulk:k"),
            InlineKeyboardButton(text="✍️ Категории роутерам", callback_data="inc:bulk:r"),
        ],
        [InlineKeyboardButton(text="🏷 Настроить категории и цены", callback_data="inc:cats")],
    ])


def _local(dt: datetime, naive_is_utc: bool) -> datetime:
    if dt.tzinfo is None and naive_is_utc:
        dt = dt.replace(tzinfo=timezone.utc)
    return to_tz(dt)


async def build_report(month: str | None = None) -> str:
    now = now_tz()
    month = month or now.strftime("%Y-%m")
    cats = await categories_map()
    dealer_price = await get_price()

    async with SessionLocal() as session:
        items = (await session.execute(select(Item))).scalars().all()
        routers = (await session.execute(select(RouterItem))).scalars().all()
        services = (await session.execute(select(Service))).scalars().all()
        txns = (await session.execute(select(IncomeTxn))).scalars().all()
        btxns = (await session.execute(
            select(BalanceTxn).where(BalanceTxn.kind.in_(["renewal", "order", "payment"]))
        )).scalars().all()

    expected: dict[str, float] = defaultdict(float)
    lines = ["📊 Доходы", "", "📈 Ожидаемо в месяц (по активным клиентам):"]

    def block(title: str, objs) -> None:
        groups: dict[tuple[str, float, str], int] = defaultdict(int)
        unknown = 0
        subtotal: dict[str, float] = defaultdict(float)
        for o in objs:
            p = price_of(o, cats)
            if not p:
                unknown += 1
                continue
            groups[(p[2], p[0], p[1])] += 1
            subtotal[p[1]] += p[0]
            expected[p[1]] += p[0]
        total_s = " • ".join(fmt_money(v, c) for c, v in subtotal.items()) or "0"
        lines.append(f"{title}: {total_s} ({len(objs)} шт.)")
        for (label, price, cur), cnt in sorted(groups.items(), key=lambda x: -x[0][1]):
            lines.append(f"   {label} {fmt_money(price, cur)} × {cnt} = {fmt_money(price * cnt, cur)}")
        if unknown:
            lines.append(f"   ⚠️ без категории: {unknown} шт.")

    active_own = [i for i in items if (i.dealer or MAIN_CODE) == MAIN_CODE and to_tz(i.due_date) > now]
    active_dealer = [i for i in items if (i.dealer or MAIN_CODE) != MAIN_CODE and to_tz(i.due_date) > now]
    active_rt = [r for r in routers if to_tz(r.due_date) > now]
    block("🔑 Свои ключи", active_own)
    block("📡 Роутеры", active_rt)

    d_sum = len(active_dealer) * dealer_price
    expected["$"] += d_sum
    lines.append(f"🤝 Дилеры: {len(active_dealer)} × {fmt_money(dealer_price, '$')} = {fmt_money(d_sum, '$')}")

    svc_exp: dict[str, float] = defaultdict(float)
    for s in services:
        if s.amount:
            svc_exp[s.currency] += s.amount * 30 / max(1, period_len_days(s.period_months, s.period_days))
    if svc_exp:
        for c, v in svc_exp.items():
            expected[c] -= v
        lines.append("🧾 Сервисы: " + " • ".join("−" + fmt_money(round(v, 2), c) for c, v in svc_exp.items()))
    lines.append(f"Итого: {_fmt_totals(expected)}")

    # ---- Факт за месяц ----
    y, m = map(int, month.split("-"))
    fact: dict[str, float] = defaultdict(float)
    by_src: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    cnt_src: dict[str, int] = defaultdict(int)
    for t in txns:
        lt = _local(t.created_at, naive_is_utc=False)
        if (lt.year, lt.month) != (y, m):
            continue
        by_src[t.source][t.currency] += t.amount
        cnt_src[t.source] += 1
        fact[t.currency] += t.amount
    d_accr, d_cnt, d_paid = 0.0, 0, 0.0
    for b in btxns:
        lt = _local(b.created_at, naive_is_utc=True)
        if (lt.year, lt.month) != (y, m):
            continue
        if b.kind == "payment":
            d_paid += -b.amount
        else:
            d_accr += b.amount
            d_cnt += 1
    fact["$"] += d_accr

    def money(src: str) -> str:
        return " • ".join(fmt_money(abs(v), c) for c, v in by_src[src].items()) or "0"

    lines += ["", f"📅 Факт за {MONTHS_RU[m - 1]} {y}:"]
    lines.append(f"🔑 Свои ключи: {money('key')} ({cnt_src['key']} опл.)")
    lines.append(f"📡 Роутеры: {money('router')} ({cnt_src['router']} опл.)")
    lines.append(f"🤝 Дилеры: начислено {fmt_money(d_accr, '$')} ({d_cnt}), получено {fmt_money(d_paid, '$')}")
    if by_src["service"]:
        lines.append(f"🧾 Сервисы: −{money('service')} ({cnt_src['service']} опл.)")
    else:
        lines.append("🧾 Сервисы: 0")
    lines.append(f"Итого: {_fmt_totals(fact)}")
    lines += ["", "ℹ️ Факт по ключам и роутерам копится с момента запуска этой функции "
                  "(каждое продление записывается по категории клиента)."]
    return "\n".join(lines)


@router.message(Command("income"))
@router.message(F.text.in_(["📊 Доходы", "/income"]))
async def inc_home(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(await build_report(), reply_markup=_home_kb())


@router.callback_query(F.data == "inc:home")
async def inc_home_cb(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await state.clear()
    await cb.message.answer(await build_report(), reply_markup=_home_kb())


@router.callback_query(F.data.startswith("inc:m:"))
async def inc_month(cb: CallbackQuery) -> None:
    await cb.answer()
    month = cb.data.split(":")[2]
    await cb.message.answer(await build_report(month), reply_markup=_home_kb(month))


# ---------------------------------------------------------------------
#   Настройка категорий
# ---------------------------------------------------------------------

async def _usage_counts() -> dict[int, int]:
    counts: dict[int, int] = defaultdict(int)
    async with SessionLocal() as session:
        for model in (Item, RouterItem):
            rows = (await session.execute(
                select(model.category_id, func.count()).where(model.category_id.is_not(None))
                .group_by(model.category_id)
            )).all()
            for cid, n in rows:
                counts[cid] += n
    return counts


@router.callback_query(F.data == "inc:cats")
async def inc_cats(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await state.clear()
    cats = await list_categories()
    usage = await _usage_counts()
    lines = ["🏷 Категории цен (в месяц):"]
    rows = []
    for kind, title in KIND_TITLES.items():
        icon = "🔑" if kind == "key" else "📡"
        lines.append(f"\n{icon} {title}:")
        for c in [c for c in cats if c.kind == kind]:
            lines.append(f"  • {c.name} — {fmt_money(c.price, c.currency)} ({usage.get(c.id, 0)} кл.)")
            rows.append([InlineKeyboardButton(
                text=f"{icon} {c.name} — {fmt_money(c.price, c.currency)}", callback_data=f"inc:cat:{c.id}",
            )])
    rows.append([
        InlineKeyboardButton(text="➕ Для ключей", callback_data="inc:catadd:key"),
        InlineKeyboardButton(text="➕ Для роутеров", callback_data="inc:catadd:router"),
    ])
    rows.append([InlineKeyboardButton(text="◀ К доходам", callback_data="inc:home")])
    await cb.message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


def _cat_kb(cid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✏️ Название", callback_data=f"inc:catf:{cid}:name"),
            InlineKeyboardButton(text="✏️ Цена", callback_data=f"inc:catf:{cid}:price"),
        ],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"inc:catdel:{cid}")],
        [InlineKeyboardButton(text="◀ К категориям", callback_data="inc:cats")],
    ])


@router.callback_query(F.data.startswith("inc:cat:"))
async def inc_cat_card(cb: CallbackQuery) -> None:
    await cb.answer()
    cid = int(cb.data.split(":")[2])
    async with SessionLocal() as session:
        c = await session.get(PriceCategory, cid)
    if not c:
        await cb.message.answer("Категория не найдена.")
        return
    used = (await _usage_counts()).get(cid, 0)
    await cb.message.answer(
        f"🏷 {c.name} ({KIND_TITLES[c.kind].lower()})\nЦена: {fmt_money(c.price, c.currency)} в месяц\n"
        f"Клиентов: {used}",
        reply_markup=_cat_kb(cid),
    )


@router.callback_query(F.data.startswith("inc:catadd:"))
async def inc_cat_add(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    kind = cb.data.split(":")[2]
    await state.set_state(CategoryStates.waiting_new)
    await state.update_data(cat_kind=kind)
    await cb.message.answer(
        f"Новая категория ({KIND_TITLES[kind].lower()}).\n"
        f"Введите название и цену в {DEFAULT_CURRENCY}, например «Соседи 180»:"
    )


@router.message(CategoryStates.waiting_new)
async def inc_cat_add_save(message: Message, state: FSMContext) -> None:
    m = re.fullmatch(r"(.+?)\s+(\d+(?:[.,]\d+)?)", (message.text or "").strip())
    if not m:
        await message.answer("Формат: название и цена, например «Соседи 180». Или /cancel.")
        return
    kind = (await state.get_data())["cat_kind"]
    await state.clear()
    async with SessionLocal() as session:
        n = (await session.execute(select(func.count()).select_from(PriceCategory))).scalar() or 0
        c = PriceCategory(kind=kind, name=m.group(1)[:64], price=float(m.group(2).replace(",", ".")),
                          currency=DEFAULT_CURRENCY, sort=n)
        session.add(c)
        await session.commit()
        await session.refresh(c)
    await message.answer(f"✅ Категория добавлена: {c.name} — {fmt_money(c.price, c.currency)}",
                         reply_markup=_cat_kb(c.id))


@router.callback_query(F.data.startswith("inc:catf:"))
async def inc_cat_field(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    _, _, cid, field = cb.data.split(":")
    await state.set_state(CategoryStates.waiting_name if field == "name" else CategoryStates.waiting_price)
    await state.update_data(cat_id=int(cid))
    await cb.message.answer("Новое название:" if field == "name" else f"Новая цена в {DEFAULT_CURRENCY}:")


@router.message(CategoryStates.waiting_name)
@router.message(CategoryStates.waiting_price)
async def inc_cat_field_save(message: Message, state: FSMContext) -> None:
    t = (message.text or "").strip()
    is_price = (await state.get_state()) == CategoryStates.waiting_price.state
    if is_price and not re.fullmatch(r"\d+(?:[.,]\d+)?", t):
        await message.answer("Нужно число, например 180:")
        return
    if not is_price and not t:
        await message.answer("Название не может быть пустым:")
        return
    cid = int((await state.get_data())["cat_id"])
    await state.clear()
    async with SessionLocal() as session:
        c = await session.get(PriceCategory, cid)
        if not c:
            await message.answer("Категория не найдена.")
            return
        if is_price:
            c.price = float(t.replace(",", "."))
        else:
            c.name = t[:64]
        await session.commit()
        await session.refresh(c)
    await message.answer(f"✅ Сохранено: {c.name} — {fmt_money(c.price, c.currency)}", reply_markup=_cat_kb(cid))


@router.callback_query(F.data.startswith("inc:catdel:"))
async def inc_cat_del(cb: CallbackQuery) -> None:
    await cb.answer()
    cid = int(cb.data.split(":")[2])
    used = (await _usage_counts()).get(cid, 0)
    warn = f"\nУ {used} клиентов категория станет «не указана»." if used else ""
    await cb.message.answer(
        f"Удалить категорию?{warn}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Удалить", callback_data=f"inc:catdok:{cid}"),
            InlineKeyboardButton(text="❌ Отмена", callback_data=f"inc:cat:{cid}"),
        ]]),
    )


@router.callback_query(F.data.startswith("inc:catdok:"))
async def inc_cat_del_ok(cb: CallbackQuery) -> None:
    await cb.answer()
    cid = int(cb.data.split(":")[2])
    async with SessionLocal() as session:
        for model in (Item, RouterItem):
            await session.execute(update(model).where(model.category_id == cid).values(category_id=None))
        c = await session.get(PriceCategory, cid)
        if c:
            await session.delete(c)
        await session.commit()
    await cb.message.answer("🗑 Категория удалена.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="◀ К категориям", callback_data="inc:cats"),
    ]]))
