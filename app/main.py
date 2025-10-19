import asyncio
import logging

from aiogram import Bot, Dispatcher

from app.config import settings
from app.bot import router, set_bot_commands
from app.db import init_db
from app.scheduler import start_scheduler


async def main() -> None:
    if not settings.BOT_TOKEN:
        print("ERROR: BOT_TOKEN is not set in .env", flush=True)
        return

    logging.basicConfig(level=logging.INFO)
    bot = Bot(token=settings.BOT_TOKEN)
    dp = Dispatcher()

    # Инициализируем БД
    await init_db()

    # Регистрируем команды бота (кнопка «меню» в Telegram)
    await set_bot_commands(bot)

    # Подключаем роутеры
    dp.include_router(router)

    # Запускаем планировщик задач (проверка истечений)
    start_scheduler(bot)

    print("XMPLUS: starting polling...", flush=True)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("XMPLUS: stopped", flush=True)