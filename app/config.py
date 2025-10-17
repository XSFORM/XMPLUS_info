import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Settings:
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
    OWNER_CHAT_ID: str | None = os.getenv("OWNER_CHAT_ID")  # можно оставить пустым
    DEALER_NAME: str = os.getenv("DEALER_NAME", "main")
    TIMEZONE: str = os.getenv("TIMEZONE", "Europe/Moscow")

    # Интервалы и лимиты (то, что уже использовалось в compose)
    CHECK_INTERVAL_MINUTES: int = int(os.getenv("CHECK_INTERVAL_MINUTES", "1"))
    NOTIFY_EVERY_MINUTES: int = int(os.getenv("NOTIFY_EVERY_MINUTES", "180"))
    MAX_NOTIFICATIONS: int = int(os.getenv("MAX_NOTIFICATIONS", "9"))

    # БД: по умолчанию SQLite в ./data/
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./data/data.db")


settings = Settings()