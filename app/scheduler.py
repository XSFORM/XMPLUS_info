from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import settings


def start_scheduler() -> AsyncIOScheduler:
    """
    Заготовка планировщика — сюда позже добавятся задачи проверки истечений.
    Сейчас просто запускается пустой шедулер.
    """
    scheduler = AsyncIOScheduler(timezone=settings.TIMEZONE)
    scheduler.start()
    return scheduler