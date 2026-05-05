"""Shared fixtures for the test suite."""

import os

# Set minimal env vars before any app code is imported
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000000000:AAFakeTokenForTests")
os.environ.setdefault("GOOGLE_CLIENT_ID", "fake-client-id")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "fake-client-secret")
os.environ.setdefault("FERNET_KEY", "uN5G7QOJHAoEefkLBrumiB5jm19dJI7TECz878jB7A0=")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("OPENAI_API_KEY", "sk-fake-key-for-tests")


import pytest_asyncio


@pytest_asyncio.fixture
async def db():
    """Per-test in-memory SQLite with the schema created.

    NullPool gives every connection a fresh in-memory DB by default; we
    work around that by pinning a static_pool so the same connection (and
    same in-memory store) is reused for the duration of the test.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import StaticPool

    from app.db import base as db_base
    from app.db.base import Base

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Swap the module-level engine + session factory so app code uses ours.
    orig_engine = db_base.engine
    orig_factory = db_base.async_session_factory
    db_base.engine = engine
    db_base.async_session_factory = factory
    try:
        yield engine
    finally:
        db_base.engine = orig_engine
        db_base.async_session_factory = orig_factory
        await engine.dispose()
