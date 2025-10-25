import os

class Settings:
    # Бот
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
    OWNER_CHAT_ID: str | None = os.getenv("OWNER_CHAT_ID")

    # Режим работы бота: admin | dealer
    BOT_MODE: str = os.getenv("BOT_MODE", "admin").strip().lower()

    # Имя дилера (для dealer-ботов и фильтрации)
    DEALER_NAME: str = os.getenv("DEALER_NAME", "main")

    # Таймзона (используется админ-ботом; у дилеров переключатель отключаем)
    TIMEZONE: str = os.getenv("TIMEZONE", "Asia/Ashgabat")

    # Планировщик и уведомления (оставляем как были)
    CHECK_INTERVAL_MINUTES: int = int(os.getenv("CHECK_INTERVAL_MINUTES", "1"))
    PRE_NOTIFY_HOURS: int = int(os.getenv("PRE_NOTIFY_HOURS", "3"))
    NOTIFY_EVERY_MINUTES: int = int(os.getenv("NOTIFY_EVERY_MINUTES", "180"))
    MAX_NOTIFICATIONS: int = int(os.getenv("MAX_NOTIFICATIONS", "2"))

    # База данных
    # Админ-бот: sqlite+aiosqlite:///./data/data.db
    # Дилер-бот (read-only): sqlite+aiosqlite:///file:/app/data/data.db?mode=ro&uri=true
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./data/data.db")

settings = Settings()