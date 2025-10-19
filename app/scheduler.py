from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiogram import Bot

from app.config import settings
from app.jobs import check_expiries


def start_scheduler(bot: Bot) -> AsyncIOScheduler:
    """
    Запускает планировщик и регистрирует периодическую задачу проверки истечений.
    """
    scheduler = AsyncIOScheduler(timezone=settings.TIMEZONE)
    scheduler.add_job(
        check_expiries,
        "interval",
        minutes=settings.CHECK_INTERVAL_MINUTES,
        args=[bot],
        id="check_expiries",
        replace_existing=True,
    )
    scheduler.start()
    return scheduler