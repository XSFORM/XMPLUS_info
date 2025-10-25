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
    Кол-во уведомлений ограничено MAX_NOTIFICATIONS.

    ВАЖНО: фильтрация по режиму бота
    - admin-бот: только dealer='main'
    - dealer-бот: только записи своего дилера (dealer == DEALER_NAME) и отправка в OWNER_CHAT_ID дилера.
    """
    now = now_tz()  # tz-aware в вашей TZ (например, Asia/Ashgabat)
    pre_hours = settings.PRE_NOTIFY_HOURS
    tz_str = f"UTC{tz_offset_str()}"

    dealer_mode = settings.BOT_MODE == "dealer"
    dealer_name = settings.DEALER_NAME

    async with SessionLocal() as session:
        # Базовый запрос: сортируем по due_date
        q = select(Item).order_by(Item.due_date.asc())
        if dealer_mode:
            # Дилер-бот видит только свои записи
            q = q.where(Item.dealer == dealer_name)
        else:
            # Админ-бот шлёт уведомления только по "main"
            q = q.where(Item.dealer == "main")

        items = (await session.execute(q)).scalars().all()

        for it in items:
            # Куда отправлять:
            if dealer_mode:
                # Дилерский бот всегда шлёт в свой OWNER_CHAT_ID
                target_chat = int(settings.OWNER_CHAT_ID) if settings.OWNER_CHAT_ID else None
            else:
                # Админ-бот — в chat_id записи (если сохранён), иначе в OWNER_CHAT_ID админа
                target_chat = it.chat_id or (int(settings.OWNER_CHAT_ID) if settings.OWNER_CHAT_ID else None)

            if not target_chat:
                continue

            if it.notified_count >= settings.MAX_NOTIFICATIONS:
                continue  # лимит уведомлений исчерпан

            # Приводим due_date к tz-aware в локальной TZ
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
            if now >= due and it.notified_count < settings.MAX_NOTIFICATIONS:
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
                    it.notified_count = min(settings.MAX_NOTIFICATIONS, it.notified_count + 1)
                    it.last_notified_at = now

        await session.commit()