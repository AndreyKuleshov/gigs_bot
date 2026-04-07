"""SQLAlchemy async engine and session factory."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings

# asyncpg doesn't accept `sslmode` as a query param — strip it and pass ssl via
# connect_args instead so Neon (and any other SSL-required host) works correctly.
_db_url = settings.database_url.replace("?sslmode=require", "").replace("&sslmode=require", "")
_ssl_required = "sslmode=require" in settings.database_url

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
    # Side-effect: registers ORM models with Base.metadata before create_all.
    # importlib.import_module avoids a "not accessed" diagnostic on an unused import.
    import importlib

    importlib.import_module("app.db.models")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def close_engine() -> None:
    await engine.dispose()
