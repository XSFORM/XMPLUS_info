from __future__ import annotations

from typing import Optional
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import Integer, String, DateTime

from app.config import settings


# Async-движок под aiosqlite
engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
)

# Фабрика async-сессий (то самое SessionLocal, которое использует бот)
SessionLocal = async_sessionmaker(
    bind=engine,
    expire_on_commit=False,
    class_=AsyncSession,
)


class Base(DeclarativeBase):
    pass


class Item(Base):
    __tablename__ = "items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Основные поля
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    username: Mapped[str] = mapped_column(String(255), nullable=False)
    due_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Метка дилера (по умолчанию 'main')
    dealer: Mapped[str] = mapped_column(String(64), nullable=False, default="main")

    # Служебные поля для уведомлений/чата (как было)
    chat_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    notify_every_minutes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    max_notifications: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    notified_count: Mapped[int] = mapped_column(Integer, default=0)
    last_notified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


# Создание таблиц при первом запуске (без миграций; существующие таблицы не трогаются)
async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)