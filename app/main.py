import asyncio
import logging
import traceback

from aiogram import Bot, Dispatcher
from aiogram.types import ErrorEvent

from app.config import settings
from app.bot import router, dealer_router, guest_router, set_bot_commands
from app.db import init_db, seed_default_dealers, seed_payment_methods
from app.scheduler import start_scheduler

log = logging.getLogger(__name__)


async def main() -> None:
    if not settings.BOT_TOKEN:
        print("ERROR: BOT_TOKEN is not set in .env", flush=True)
        return

    logging.basicConfig(level=logging.INFO)
    bot = Bot(token=settings.BOT_TOKEN)
    dp = Dispatcher()

    @dp.errors()
    async def global_error_handler(event: ErrorEvent) -> bool:
        log.error("Unhandled exception:\n%s", traceback.format_exc())
        # Try to notify the user
        try:
            upd = event.update
            chat_id = None
            if upd.message:
                chat_id = upd.message.chat.id
            elif upd.callback_query and upd.callback_query.message:
                chat_id = upd.callback_query.message.chat.id
            if chat_id:
                await bot.send_message(chat_id, "⚠️ Произошла ошибка, нажмите /cancel")
        except Exception:
            pass
        # Notify admin
        owner = int(settings.OWNER_CHAT_ID) if settings.OWNER_CHAT_ID else None
        if owner:
            try:
                tb = traceback.format_exc()
                short = tb[-3500:] if len(tb) > 3500 else tb
                await bot.send_message(owner, f"⚠️ Bot error:\n<pre>{short}</pre>", parse_mode="HTML")
            except Exception:
                pass
        return True

    # Инициализируем БД
    await init_db()

    # Первичное наполнение списка дилеров и методов оплаты (только admin-бот)
    if settings.BOT_MODE != "dealer":
        await seed_default_dealers()
        await seed_payment_methods()

    # Регистрируем команды бота (кнопка «меню» в Telegram)
    await set_bot_commands(bot)

    # Подключаем роутеры (порядок: владелец, дилеры, гости-в-конце)
    dp.include_router(router)
    dp.include_router(dealer_router)
    dp.include_router(guest_router)

    # Запускаем планировщик задач (проверка истечений)
    start_scheduler(bot)

    print("XMPLUS: starting polling...", flush=True)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("XMPLUS: stopped", flush=True)
