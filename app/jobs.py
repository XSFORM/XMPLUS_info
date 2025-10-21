from __future__ import annotations

from datetime import timedelta

from aiogram import Bot
from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal, Item
from app.utils import now_tz, fmt_dt_human, tz_offset_str, to_tz


async def check_expiries(bot: Bot) -> None:
    """
    Уведомления по каждому Item:
    - 1-й раз: когда до due_date осталось <= PRE_NOTIFY_HOURS (и ещё не наступил срок).
    - 2-й раз: после наступления due_date (о просрочке).
    Больше 2 уведомлений по записи не отправляем.
    Всегда приводим due_date к локальной TZ, чтобы избежать "offset-naive vs aware".
    """
    now = now_tz()  # tz-aware в вашей TZ (Asia/Ashgabat)
    pre_hours = settings.PRE_NOTIFY_HOURS
    tz_str = f"UTC{tz_offset_str()}"

    async with SessionLocal() as session:
        items = (await session.execute(select(Item).order_by(Item.due_date.asc()))).scalars().all()

        for it in items:
            # Куда отправлять: chat_id записи или OWNER_CHAT_ID из .env
            target_chat = it.chat_id or (int(settings.OWNER_CHAT_ID) if settings.OWNER_CHAT_ID else None)
            if not target_chat:
                continue

            if it.notified_count >= 2:
                continue  # уже отправили оба уведомления

            # Приводим due_date к tz-aware в локальной TZ (если вдруг было без tz)
            due = to_tz(it.due_date)
            delta = due - now

            # 1) Предупреждение за N часов до истечения (один раз)
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
                    pass
                else:
                    it.notified_count = 1
                    it.last_notified_at = now
                    continue

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