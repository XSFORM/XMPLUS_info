"""Доходы: категории цен клиентов и журнал фактических поступлений/расходов."""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import select

from app.db import SessionLocal, PriceCategory, IncomeTxn
from app.utils import now_tz

log = logging.getLogger(__name__)

DEFAULT_CURRENCY = "TMT"
KIND_TITLES = {"key": "Ключи", "router": "Роутеры"}


async def list_categories(kind: Optional[str] = None) -> list[PriceCategory]:
    async with SessionLocal() as session:
        q = select(PriceCategory).order_by(PriceCategory.kind.asc(), PriceCategory.sort.asc(), PriceCategory.id.asc())
        if kind:
            q = q.where(PriceCategory.kind == kind)
        return list((await session.execute(q)).scalars().all())


async def categories_map() -> dict[int, PriceCategory]:
    return {c.id: c for c in await list_categories()}


def price_of(obj, cats: dict[int, PriceCategory]) -> Optional[tuple[float, str, str]]:
    """Цена клиента: (сумма, валюта, подпись) или None, если не указана."""
    if getattr(obj, "custom_price", None) is not None:
        return float(obj.custom_price), DEFAULT_CURRENCY, "своя цена"
    cid = getattr(obj, "category_id", None)
    if cid and cid in cats:
        c = cats[cid]
        return float(c.price), c.currency, c.name
    return None


async def price_for(obj) -> Optional[tuple[float, str, str]]:
    return price_of(obj, await categories_map())


def fmt_money(amount: float, currency: str) -> str:
    v = f"{amount:,.2f}".rstrip("0").rstrip(".").replace(",", " ")
    return f"{v} {currency}"


async def log_income(source: str, amount: float, currency: str, comment: str = "") -> Optional[int]:
    """Записать фактическое поступление (+) или расход (−). Возвращает id записи."""
    try:
        async with SessionLocal() as session:
            t = IncomeTxn(
                created_at=now_tz().replace(tzinfo=None),
                source=source, amount=amount, currency=currency, comment=comment[:255],
            )
            session.add(t)
            await session.commit()
            await session.refresh(t)
            return t.id
    except Exception as e:
        log.warning("log_income failed: %s", e)
        return None


async def delete_income(txn_id: int) -> None:
    async with SessionLocal() as session:
        t = await session.get(IncomeTxn, txn_id)
        if t:
            await session.delete(t)
            await session.commit()


async def log_client_income(obj, source: str, comment: str) -> str:
    """Записать доход по цене клиента. Возвращает строку для ответа пользователю ('' если цена не задана/0)."""
    p = await price_for(obj)
    if not p:
        return "\n💰 Категория не указана — доход не учтён (✏️ Изменить → 💰 Категория)."
    amount, cur, label = p
    if amount <= 0:
        return ""
    await log_income(source, amount, cur, comment)
    return f"\n💰 +{fmt_money(amount, cur)} в доходы ({label})"
