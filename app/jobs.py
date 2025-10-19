from __future__ import annotations

from datetime import timedelta

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import settings
from app.db import SessionLocal, Item
from app.utils import now_tz, fmt_dt_human


async def check_expiries(bot: Bot) -> None:
    now = now_tz()

    async with SessionLocal() as session:
        result = await session.execute(
            select(Item).options(selectinload("*")).order_by(Item.due_date.asc())
        )
        items = result.scalars().all()

        for it in items:
            notify_every = it.notify_every_minutes or settings.NOTIFY_EVERY_MINUTES
            max_notifs = it.max_notifications or settings.MAX_NOTIFICATIONS

            if it.last_notified_at and (now - it.last_notified_at) < timedelta(minutes=notify_every):
                continue
            if it.notified_count >= max_notifs:
                continue

            should_notify = (it.due_date - now) <= timedelta(minutes=notify_every)
            if not should_notify:
                continue

            target_chat = it.chat_id or (int(settings.OWNER_CHAT_ID) if settings.OWNER_CHAT_ID else None)
            if not target_chat:
                continue

            if it.due_date >= now:
                text = f"⏰ Истечение: USERID={it.user_id}, USERNAME={it.username} — {fmt_dt_human(it.due_date)}"
            else:
                text = f"⛔ Просрочено: USERID={it.user_id}, USERNAME={it.username} — {fmt_dt_human(it.due_date)}"

            try:
                await bot.send_message(target_chat, text)
            except Exception:
                continue

            it.notified_count += 1
            it.last_notified_at = now

        await session.commit()