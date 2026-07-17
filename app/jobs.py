from __future__ import annotations

import logging
from datetime import timedelta

from aiogram import Bot
from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal, Item, RouterItem, Dealer
from app.utils import now_tz, fmt_dt_human, tz_offset_str, to_tz

log = logging.getLogger(__name__)


async def check_expiries(bot: Bot) -> None:
    """
    Уведомления по всем записям. В единой схеме их шлёт ТОЛЬКО admin-бот:
    - записи 'main' → администратору (или в chat_id записи);
    - записи дилера → этому дилеру (по chat_id из таблицы dealers).
    Дилер-контейнеры (legacy) уведомления больше не отправляют.

    На каждую запись:
    - 1-й раз: когда до due_date осталось <= PRE_NOTIFY_HOURS (и срок ещё не наступил);
    - 2-й раз: один раз после наступления due_date (о просрочке).
    Счётчик notified_count растёт только при успешной отправке — поэтому,
    если получатель недоступен, уведомление повторяется, пока не дойдёт.
    """
    if settings.BOT_MODE == "dealer":
        return

    now = now_tz()
    pre_hours = settings.PRE_NOTIFY_HOURS
    tz_str = f"UTC{tz_offset_str()}"
    owner_chat = int(settings.OWNER_CHAT_ID) if settings.OWNER_CHAT_ID else None

    async with SessionLocal() as session:
        # Карта: код дилера -> chat_id
        dealer_rows = (await session.execute(select(Dealer.code, Dealer.chat_id))).all()
        dealer_chat = {code: chat_id for code, chat_id in dealer_rows}

        items = (await session.execute(select(Item).order_by(Item.due_date.asc()))).scalars().all()

        for it in items:
            # Кому отправлять уведомление по этой записи
            if it.dealer and it.dealer in dealer_chat:
                target_chat = dealer_chat[it.dealer]
            else:
                # 'main' или дилер без записи в таблице → администратору
                target_chat = it.chat_id or owner_chat
            if not target_chat:
                continue

            if it.notified_count >= settings.MAX_NOTIFICATIONS:
                continue

            due = to_tz(it.due_date)
            delta = due - now

            # 1) Предупреждение за N часов до истечения — один раз
            if it.notified_count == 0 and now < due and delta <= timedelta(hours=pre_hours):
                note = getattr(it, "note", "") or ""
                note_line = f" ({note})" if note else ""
                text = (
                    "⏰ Уведомление\n"
                    f"Подписка отключится через {pre_hours} ч. ({tz_str})\n\n"
                    f"Клиент: USERID={it.user_id}, USERNAME={it.username}{note_line}\n"
                    f"Дата/время отключения: {fmt_dt_human(due)}"
                )
                try:
                    await bot.send_message(target_chat, text)
                except Exception as e:
                    dealer_name = it.dealer if it.dealer != "main" else "admin"
                    log.warning("Notify failed (pre) for USERID=%s to %s: %s", it.user_id, dealer_name, e)
                    if owner_chat and target_chat != owner_chat:
                        try:
                            await bot.send_message(owner_chat, f"\u26a0\ufe0f \u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u0443\u0432\u0435\u0434\u043e\u043c\u0438\u0442\u044c {dealer_name} \u043e USERID={it.user_id}: {e}")
                        except Exception:
                            pass
                else:
                    it.notified_count = 1
                    it.last_notified_at = now
                    continue

            # 2) Просрочка — строго один раз
            if now >= due and it.notified_count < settings.MAX_NOTIFICATIONS:
                note = getattr(it, "note", "") or ""
                note_line = f" ({note})" if note else ""
                text = (
                    "⛔ Просрочено\n"
                    f"Срок подписки истёк ({fmt_dt_human(due)}; {tz_str}).\n\n"
                    f"Клиент: USERID={it.user_id}, USERNAME={it.username}{note_line}\n"
                    "Уточните у администратора."
                )
                try:
                    await bot.send_message(target_chat, text)
                except Exception as e:
                    dealer_name = it.dealer if it.dealer != "main" else "admin"
                    log.warning("Notify failed (overdue) for USERID=%s to %s: %s", it.user_id, dealer_name, e)
                    if owner_chat and target_chat != owner_chat:
                        try:
                            await bot.send_message(owner_chat, f"\u26a0\ufe0f \u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u0443\u0432\u0435\u0434\u043e\u043c\u0438\u0442\u044c {dealer_name} \u043e \u043f\u0440\u043e\u0441\u0440\u043e\u0447\u043a\u0435 USERID={it.user_id}: {e}")
                        except Exception:
                            pass
                else:
                    it.notified_count = settings.MAX_NOTIFICATIONS
                    it.last_notified_at = now

        # ---- Роутеры: уведомления только администратору ----
        routers = (await session.execute(
            select(RouterItem).order_by(RouterItem.due_date.asc())
        )).scalars().all()

        for rt in routers:
            if not owner_chat:
                break
            if rt.notified_count >= settings.MAX_NOTIFICATIONS:
                continue

            due = to_tz(rt.due_date)
            delta = due - now
            note = getattr(rt, "note", "") or ""
            note_line = f"\nЗаметка: {note}" if note else ""

            # 1) Предупреждение за N часов
            if rt.notified_count == 0 and now < due and delta <= timedelta(hours=pre_hours):
                text = (
                    f"⏰ Роутер: уведомление\n"
                    f"Подписка отключится через {pre_hours} ч. ({tz_str})\n\n"
                    f"Клиент: {rt.client_name}{note_line}\n"
                    f"Дата/время отключения: {fmt_dt_human(due)}"
                )
                try:
                    await bot.send_message(owner_chat, text)
                except Exception as e:
                    log.warning("Router notify failed (pre) for %s: %s", rt.client_name, e)
                else:
                    rt.notified_count = 1
                    rt.last_notified_at = now
                    continue

            # 2) Просрочка
            if now >= due and rt.notified_count < settings.MAX_NOTIFICATIONS:
                text = (
                    f"⛔ Роутер: просрочено\n"
                    f"Срок подписки истёк ({fmt_dt_human(due)}; {tz_str}).\n\n"
                    f"Клиент: {rt.client_name}{note_line}\n"
                    "Продлите или удалите роутер."
                )
                try:
                    await bot.send_message(owner_chat, text)
                except Exception as e:
                    log.warning("Router notify failed (overdue) for %s: %s", rt.client_name, e)
                else:
                    rt.notified_count = settings.MAX_NOTIFICATIONS
                    rt.last_notified_at = now

        await session.commit()
