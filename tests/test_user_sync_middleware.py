"""Tests for UserSyncMiddleware — extracts from_user and upserts on every update."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.bot.middlewares.user_sync import UserSyncMiddleware


def _update_with_message(user_id: int, username: str | None, full_name: str | None):
    """Fake aiogram Update carrying a message with a from_user."""
    msg = SimpleNamespace(
        from_user=SimpleNamespace(id=user_id, username=username, full_name=full_name)
    )
    # Other inner-event slots must be falsy so the middleware picks "message".
    return SimpleNamespace(
        message=msg,
        edited_message=None,
        callback_query=None,
        inline_query=None,
        my_chat_member=None,
        chat_member=None,
    )


@pytest.fixture
def mw():
    return UserSyncMiddleware()


@pytest.mark.asyncio
async def test_calls_upsert_with_from_user_fields(mw):
    handler = AsyncMock(return_value="handler-result")
    event = _update_with_message(42, "andrei", "Andrei G")
    with patch("app.bot.middlewares.user_sync.auth_service") as auth:
        auth.upsert_user_info = AsyncMock()
        result = await mw(handler, event, {})
    assert result == "handler-result"
    auth.upsert_user_info.assert_awaited_once_with(42, username="andrei", full_name="Andrei G")
    handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_skips_upsert_when_no_from_user(mw):
    handler = AsyncMock(return_value="ok")
    event = SimpleNamespace(
        message=None,
        edited_message=None,
        callback_query=None,
        inline_query=None,
        my_chat_member=None,
        chat_member=None,
    )
    with patch("app.bot.middlewares.user_sync.auth_service") as auth:
        auth.upsert_user_info = AsyncMock()
        await mw(handler, event, {})
    auth.upsert_user_info.assert_not_called()
    handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_handler_runs_even_if_upsert_fails(mw):
    """A DB hiccup on user-sync must not block the handler."""
    handler = AsyncMock(return_value="ok")
    event = _update_with_message(42, "x", "Y")
    with patch("app.bot.middlewares.user_sync.auth_service") as auth:
        auth.upsert_user_info = AsyncMock(side_effect=RuntimeError("db down"))
        result = await mw(handler, event, {})
    assert result == "ok"
    handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_extracts_from_callback_query(mw):
    handler = AsyncMock(return_value="ok")
    cb = SimpleNamespace(from_user=SimpleNamespace(id=7, username="u7", full_name="User Seven"))
    event = SimpleNamespace(
        message=None,
        edited_message=None,
        callback_query=cb,
        inline_query=None,
        my_chat_member=None,
        chat_member=None,
    )
    with patch("app.bot.middlewares.user_sync.auth_service") as auth:
        auth.upsert_user_info = AsyncMock()
        await mw(handler, event, {})
    auth.upsert_user_info.assert_awaited_once_with(7, username="u7", full_name="User Seven")
