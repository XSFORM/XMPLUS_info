from __future__ import annotations

from datetime import timedelta

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import settings
from app.db import SessionLocal, Item
from app.utils import now_tz, fmt_dt_human


async def check_expiries(bot: Bot) -> None:
    """
    Отправляет напоминания по элементам, у которых
    - истечение наступит в ближайший NOTIFY_EVERY_MINUTES
    - или уже просрочено,
    и при этом прошло не меньше NOTIFY_EVERY_MINUTES с последнего напоминания,
    и не превышен лимит MAX_NOTIFICATIONS.
    """
    now = now_tz()

    async with SessionLocal() as session:
        result = await session.execute(
            select(Item).options(selectinload("*")).order_by(Item.expires_at.asc())
        )
        items = result.scalars().all()

        for it in items:
            notify_every = it.notify_every_minutes or settings.NOTIFY_EVERY_MINUTES
            max_notifs = it.max_notifications or settings.MAX_NOTIFICATIONS

            # Проверяем интервал с последней отправки
            if it.last_notified_at and (now - it.last_notified_at) < timedelta(minutes=notify_every):
                continue

            if it.notified_count >= max_notifs:
                continue

            # Условие уведомления: expires_at близко или уже просрочено
            should_notify = (it.expires_at - now) <= timedelta(minutes=notify_every)
            if not should_notify:
                continue

            # Куда отправлять: chat_id элемента или OWNER_CHAT_ID
            target_chat = it.chat_id or (int(settings.OWNER_CHAT_ID) if settings.OWNER_CHAT_ID else None)
            if not target_chat:
                continue

            # Текст сообщения
            if it.expires_at >= now:
                text = f"⏰ Истечение: '{it.title}' — {fmt_dt_human(it.expires_at)}"
            else:
                text = f"⛔ Просрочено: '{it.title}' — {fmt_dt_human(it.expires_at)}"

            try:
                await bot.send_message(target_chat, text)
            except Exception:
                # Не валим задачу из-за одной ошибки отправки
                continue

            # Обновляем счётчики
            it.notified_count += 1
            it.last_notified_at = now

        await session.commit()