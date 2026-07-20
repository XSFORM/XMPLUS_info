from __future__ import annotations

from typing import Optional
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import Integer, BigInteger, Float, Boolean, String, DateTime, select

from app.config import settings


# Async-движок под aiosqlite
engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
)

# Фабрика async-сессий
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

    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    username: Mapped[str] = mapped_column(String(255), nullable=False)
    due_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    dealer: Mapped[str] = mapped_column(String(64), nullable=False, default="main")

    note: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, default="")

    chat_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    notify_every_minutes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    max_notifications: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    notified_count: Mapped[int] = mapped_column(Integer, default=0)
    last_notified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class RouterItem(Base):
    """Роутер: клиент + дата отключения + заметка (без USERID)."""
    __tablename__ = "routers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    client_name: Mapped[str] = mapped_column(String(255), nullable=False)
    due_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    note: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, default="")
    notified_count: Mapped[int] = mapped_column(Integer, default=0)
    last_notified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class DealerOrder(Base):
    """Заявка дилера на ключи (раньше хранилось в памяти _pending_orders)."""
    __tablename__ = "dealer_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dealer_code: Mapped[str] = mapped_column(String(64), nullable=False)
    dealer_title: Mapped[str] = mapped_column(String(128), nullable=False)
    dealer_chat_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    names_json: Mapped[str] = mapped_column(String(2048), nullable=False, default="[]")
    fulfilled: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class Dealer(Base):
    """Дилер для раздела /dealers (динамический список)."""
    __tablename__ = "dealers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    chat_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    # Баланс (долг) дилера в долларах
    balance: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, server_default="0")


class AppSetting(Base):
    """Хранилище настроек ключ-значение (например, цена за продление)."""
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(255), nullable=False)


class BalanceTxn(Base):
    """Операция по балансу дилера (история начислений/списаний)."""
    __tablename__ = "balance_txns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dealer_code: Mapped[str] = mapped_column(String(64), nullable=False)
    # Знак: + увеличивает долг, - уменьшает
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    # 'renewal' | 'admin_add' | 'admin_sub' | 'payment'
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    comment: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class PaymentMethod(Base):
    """Метод оплаты (ByBit, YooMoney, EnPara, Наличные и пр.) с реквизитами."""
    __tablename__ = "payment_methods"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    requisites: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1")


class PaymentVariant(Base):
    """Вид оплаты (сеть/способ) под методом — со своими реквизитами."""
    __tablename__ = "payment_variants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    method_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    requisites: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1")


class Payment(Base):
    """Заявка дилера на оплату — ожидает подтверждения админом."""
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dealer_code: Mapped[str] = mapped_column(String(64), nullable=False)
    method: Mapped[str] = mapped_column(String(64), nullable=False)
    variant: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    # 'pending' | 'confirmed' | 'rejected'
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


def _migrate_schema(conn) -> None:
    """
    Лёгкие миграции для уже существующих БД: create_all не добавляет
    новые КОЛОНКИ в существующие таблицы — добавляем их вручную.
    Плюс одноразовый перенос реквизитов методов в виды «Основной».
    """
    try:
        # dealers.balance
        rows = conn.exec_driver_sql("PRAGMA table_info(dealers)").fetchall()
        cols = {r[1] for r in rows}
        if rows and "balance" not in cols:
            conn.exec_driver_sql(
                "ALTER TABLE dealers ADD COLUMN balance REAL NOT NULL DEFAULT 0"
            )
        # items.note
        rows = conn.exec_driver_sql("PRAGMA table_info(items)").fetchall()
        cols = {r[1] for r in rows}
        if rows and "note" not in cols:
            conn.exec_driver_sql(
                "ALTER TABLE items ADD COLUMN note TEXT DEFAULT ''"
            )
        # payments.variant
        rows = conn.exec_driver_sql("PRAGMA table_info(payments)").fetchall()
        cols = {r[1] for r in rows}
        if rows and "variant" not in cols:
            conn.exec_driver_sql("ALTER TABLE payments ADD COLUMN variant TEXT")
        # Перенос: метод с непустыми реквизитами и без видов → создать вид «Основной»
        try:
            pm_rows = conn.exec_driver_sql(
                "SELECT id, requisites FROM payment_methods "
                "WHERE requisites IS NOT NULL AND length(trim(requisites)) > 0"
            ).fetchall()
            for row in pm_rows:
                m_id, m_req = row[0], row[1]
                existing = conn.exec_driver_sql(
                    "SELECT id FROM payment_variants WHERE method_id = ? LIMIT 1",
                    (m_id,),
                ).fetchall()
                if not existing:
                    conn.exec_driver_sql(
                        "INSERT INTO payment_variants (method_id, name, requisites, active) "
                        "VALUES (?, ?, ?, 1)",
                        (m_id, "Основной", m_req),
                    )
        except Exception as e:
            print(f"_migrate_schema: data-migrate warning: {e}", flush=True)
    except Exception as e:
        print(f"_migrate_schema: warning: {e}", flush=True)


# Создание таблиц при первом запуске (существующие таблицы не трогаются)
async def init_db() -> None:
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(_migrate_schema)
    except Exception as e:
        print(f"init_db: warning: {e}", flush=True)


# Дилеры по умолчанию — переносим прежний «вшитый» список в БД при первом запуске.
DEFAULT_DEALERS: list[tuple[str, str, Optional[int]]] = [
    ("serdar", "Сердар", 1832345568),
    ("ilya", "Иля", 1007155034),
]


async def seed_default_dealers() -> None:
    """Первичное наполнение таблицы дилеров. Вызывать только из admin-бота."""
    try:
        async with SessionLocal() as session:
            exists = (await session.execute(select(Dealer.id))).first()
            if exists:
                return
            for code, title, chat_id in DEFAULT_DEALERS:
                session.add(Dealer(code=code, title=title, chat_id=chat_id))
            await session.commit()
            print(f"seed_default_dealers: added {len(DEFAULT_DEALERS)} dealers", flush=True)
    except Exception as e:
        print(f"seed_default_dealers: skipped ({e})", flush=True)


DEFAULT_PAYMENT_METHODS = ["РУБЛИ", "КРИПТА", "VISA/MASTER"]


async def seed_payment_methods() -> None:
    """Первичное наполнение методов оплаты. Вызывать только из admin-бота."""
    try:
        async with SessionLocal() as session:
            exists = (await session.execute(select(PaymentMethod.id))).first()
            if exists:
                return
            for name in DEFAULT_PAYMENT_METHODS:
                session.add(PaymentMethod(name=name, requisites="", active=True))
            await session.commit()
            print(f"seed_payment_methods: added {len(DEFAULT_PAYMENT_METHODS)} methods", flush=True)
    except Exception as e:
        print(f"seed_payment_methods: skipped ({e})", flush=True)


# ====== Цена за продление и баланс дилеров ======

PRICE_KEY = "price_per_month"
DEFAULT_PRICE = 5.0


async def get_price() -> float:
    """Текущая цена за продление (в $). По умолчанию — DEFAULT_PRICE."""
    try:
        async with SessionLocal() as session:
            row = (await session.execute(
                select(AppSetting.value).where(AppSetting.key == PRICE_KEY)
            )).first()
        if row and row[0]:
            return float(row[0])
    except Exception:
        pass
    return DEFAULT_PRICE


async def set_price(value: float) -> None:
    """Установить цену за продление."""
    async with SessionLocal() as session:
        existing = (await session.execute(
            select(AppSetting).where(AppSetting.key == PRICE_KEY)
        )).scalars().first()
        if existing:
            existing.value = str(value)
        else:
            session.add(AppSetting(key=PRICE_KEY, value=str(value)))
        await session.commit()


async def apply_balance_change(dealer_code: str, amount: float, kind: str, comment: str = "") -> Optional[float]:
    """
    Изменить баланс дилера на amount (со знаком) и записать операцию в историю.
    Возвращает новый баланс или None, если дилер не найден.
    """
    async with SessionLocal() as session:
        d = (await session.execute(
            select(Dealer).where(Dealer.code == dealer_code)
        )).scalars().first()
        if not d:
            return None
        d.balance = (d.balance or 0.0) + amount
        session.add(BalanceTxn(dealer_code=dealer_code, amount=amount, kind=kind, comment=comment or ""))
        new_balance = d.balance
        await session.commit()
    return new_balance


MAIN_CODE = "main"


async def list_dealers() -> list[Dealer]:
    """Все дилеры из БД (без main), отсортированы по названию."""
    async with SessionLocal() as session:
        return (
            await session.execute(select(Dealer).order_by(Dealer.title.asc()))
        ).scalars().all()


async def get_dealer(code: str) -> "Dealer | None":
    """Дилер по коду; None для пустого или несуществующего кода."""
    code = (code or "").strip().lower()
    if not code:
        return None
    async with SessionLocal() as session:
        return (
            await session.execute(select(Dealer).where(Dealer.code == code))
        ).scalars().first()


async def list_payment_methods(active_only: bool = False) -> list[PaymentMethod]:
    """Все методы оплаты (опционально — только активные)."""
    async with SessionLocal() as session:
        q = select(PaymentMethod).order_by(PaymentMethod.id.asc())
        if active_only:
            q = q.where(PaymentMethod.active.is_(True))
        return (await session.execute(q)).scalars().all()


async def get_payment_method(pm_id: int) -> "PaymentMethod | None":
    """Метод оплаты по id."""
    async with SessionLocal() as session:
        return await session.get(PaymentMethod, pm_id)


async def list_payment_variants(method_id: int, active_only: bool = False) -> list[PaymentVariant]:
    """Варианты оплаты по методу (опционально — только активные)."""
    async with SessionLocal() as session:
        q = select(PaymentVariant).where(PaymentVariant.method_id == method_id).order_by(PaymentVariant.id.asc())
        if active_only:
            q = q.where(PaymentVariant.active.is_(True))
        return (await session.execute(q)).scalars().all()


async def get_payment_variant(var_id: int) -> "PaymentVariant | None":
    """Вариант оплаты по id."""
    async with SessionLocal() as session:
        return await session.get(PaymentVariant, var_id)
