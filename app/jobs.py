from __future__ import annotations

from datetime import timedelta

from aiogram import Bot
from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal, Item
from app.utils import now_tz, fmt_dt_human, tz_offset_str


async def check_expiries(bot: Bot) -> None:
    """
    Уведомления по каждому Item:
    - 1-й раз: Ровно в окно за PRE_NOTIFY_HOURS до due_date (если ещё не отправляли и срок не наступил).
    - 2-й раз: После наступления due_date (если ещё не отправляли «просрочено»).
    Больше 2 уведомлений по одной записи не отправляем.
    """
    now = now_tz()
    pre_hours = settings.PRE_NOTIFY_HOURS
    tz_str = f"UTC{tz_offset_str()}"

    async with SessionLocal() as session:
        items = (await session.execute(select(Item).order_by(Item.due_date.asc()))).scalars().all()

        for it in items:
            # Куда отправлять: chat_id элемента или OWNER_CHAT_ID
            target_chat = it.chat_id or (int(settings.OWNER_CHAT_ID) if settings.OWNER_CHAT_ID else None)
            if not target_chat:
                continue

            if it.notified_count >= 2:
                # Уже отправляли оба уведомления
                continue

            due = it.due_date
            delta = due - now

            # 1) Предупреждение за N часов до истечения (отправляем ровно один раз)
            if it.notified_count == 0 and now < due and delta <= timedelta(hours=pre_hours):
                text = (
                    "⏰ Уведомление\n"
                    f"Подписка отключится через {pre_hours} ч. ({tz_str})\n\n"
                    f"Клиент: USERID={it.user_id}, USERNAME={it.username}\n"
                    f"Дата/время отключения: {fmt_dt_human(due)}"
                )
                try:
                    await bot.send_message(target_chat, text)
                except Exception:
                    # не валим всю задачу из-за одной ошибки
                    pass
                else:
                    it.notified_count = 1
                    it.last_notified_at = now
                    continue  # к следующему item

            # 2) Сообщение о просрочке (один раз)
            if now >= due and it.notified_count < 2:
                text = (
                    "⛔ Просрочено\n"
                    f"Срок подписки истёк ({fmt_dt_human(due)}; {tz_str}).\n\n"
                    f"Клиент: USERID={it.user_id}, USERNAME={it.username}\n"
                    "Уточните у администратора."
                )
                try:
                    await bot.send_message(target_chat, text)
                except Exception:
                    pass
                else:
                    it.notified_count = 2
                    it.last_notified_at = now

        await session.commit()