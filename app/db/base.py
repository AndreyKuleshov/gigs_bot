"""SQLAlchemy async engine and session factory."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool

from app.core.config import settings

_db_url = settings.database_url.replace("?sslmode=require", "").replace("&sslmode=require", "")
_ssl_required = "sslmode=require" in settings.database_url
_is_sqlite = _db_url.startswith("sqlite")

if _is_sqlite:
    # NullPool creates a fresh connection per operation — required when multiple
    # asyncio.run() calls share the same engine (each call has its own event
    # loop, so a pooled connection object would be bound to the wrong loop).
    engine = create_async_engine(
        _db_url,
        echo=settings.debug,
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )
else:
    engine = create_async_engine(
        _db_url,
        echo=settings.debug,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
        connect_args={"ssl": True} if _ssl_required else {},
    )

async_session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    pass


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Yield a managed :class:`AsyncSession` that commits on success and rolls
    back on any exception."""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def create_tables() -> None:
    """Create all tables that are not yet present in the database."""
    import importlib

    importlib.import_module("app.db.models")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Lightweight column migrations for SQLite (create_all won't add new columns)
    async with engine.begin() as conn:
        await conn.run_sync(_add_missing_columns)


def _add_missing_columns(conn) -> None:
    """Add columns that create_all skips on existing tables."""
    from sqlalchemy import inspect, text

    inspector = inspect(conn)
    if "users" not in inspector.get_table_names():
        return
    existing = {c["name"] for c in inspector.get_columns("users")}
    if "timezone" not in existing:
        conn.execute(
            text("ALTER TABLE users ADD COLUMN timezone VARCHAR(50) NOT NULL DEFAULT 'UTC'")
        )


async def close_engine() -> None:
    await engine.dispose()
