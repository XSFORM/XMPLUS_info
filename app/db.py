from __future__ import annotations

from typing import Optional
from datetime import datetime

from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import Integer, String, DateTime

# В этом файле у вас уже настроены engine, SessionLocal и init_db().
# Мы лишь расширяем модель Item полем dealer под маркировку записей дилерам.

class Base(DeclarativeBase):
    pass

class Item(Base):
    __tablename__ = "items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Поля под вашу таблицу
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    username: Mapped[str] = mapped_column(String(255), nullable=False)
    due_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Новый признак владельца записи (дилера)
    dealer: Mapped[str] = mapped_column(String(64), nullable=False, default="main")

    # Служебные
    chat_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    notify_every_minutes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    max_notifications: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    notified_count: Mapped[int] = mapped_column(Integer, default=0)
    last_notified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)