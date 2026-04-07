"""SQLAlchemy async engine and session factory."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import StaticPool

from app.core.config import settings

_db_url = settings.database_url.replace("?sslmode=require", "").replace("&sslmode=require", "")
_ssl_required = "sslmode=require" in settings.database_url
_is_sqlite = _db_url.startswith("sqlite")

if _is_sqlite:
    # SQLite requires StaticPool for async use and check_same_thread=False
    engine = create_async_engine(
        _db_url,
        echo=settings.debug,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
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


async def close_engine() -> None:
    await engine.dispose()
