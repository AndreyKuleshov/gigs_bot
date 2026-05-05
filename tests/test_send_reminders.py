"""Tests for send_reminders / _remind_user happy path + auth-failure handling."""

from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.services.calendar_service import EventRead
from app.services.reminder_service import _remind_user, send_reminders


@pytest.fixture
def mock_bot():
    bot = MagicMock()
    bot.send_message = AsyncMock()
    return bot


@pytest.fixture
def deps():
    @asynccontextmanager
    async def _fake_get_session():
        # send_reminders only uses the session for SELECT; we mock the whole thing
        # via select() returning a list, so this stub is unused.
        yield AsyncMock()

    with (
        patch("app.services.reminder_service.auth_service") as auth,
        patch("app.services.reminder_service.calendar_service") as cal,
        patch("app.services.reminder_service.get_session", _fake_get_session),
    ):
        auth.get_credentials = AsyncMock(return_value=object())
        auth.get_calendar_id = AsyncMock(return_value="primary")
        cal.list_events = AsyncMock(return_value=[])
        yield SimpleNamespace(auth=auth, cal=cal)


@pytest.mark.asyncio
async def test_remind_user_no_credentials_returns_false(deps, mock_bot):
    deps.auth.get_credentials = AsyncMock(return_value=None)
    sent = await _remind_user(mock_bot, user_id=1, tz_name="Europe/Belgrade")
    assert sent is False
    mock_bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_remind_user_no_events_returns_false(deps, mock_bot):
    deps.cal.list_events = AsyncMock(return_value=[])
    sent = await _remind_user(mock_bot, user_id=1, tz_name="Europe/Belgrade")
    assert sent is False
    mock_bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_remind_user_with_events_sends_message(deps, mock_bot):
    tz = ZoneInfo("Europe/Belgrade")
    deps.cal.list_events = AsyncMock(
        return_value=[
            EventRead(
                event_id="e1",
                summary="Padel Camp",
                start=datetime(2026, 5, 6, 10, 0, tzinfo=tz),
                end=datetime(2026, 5, 6, 11, 30, tzinfo=tz),
                location="Smash Padel",
            )
        ]
    )
    sent = await _remind_user(mock_bot, user_id=1, tz_name="Europe/Belgrade")
    assert sent is True
    mock_bot.send_message.assert_awaited_once()
    body = mock_bot.send_message.await_args.args[1]
    assert "Padel Camp" in body
    assert "Smash Padel" in body


@pytest.mark.asyncio
async def test_remind_user_falls_back_to_utc_for_invalid_tz(deps, mock_bot):
    """Invalid IANA strings should not crash — _remind_user logs and uses UTC."""
    deps.cal.list_events = AsyncMock(return_value=[])
    sent = await _remind_user(mock_bot, user_id=1, tz_name="Not/A/Real/Zone")
    assert sent is False  # no events, but no exception either


@pytest.mark.asyncio
async def test_send_reminders_iterates_users():
    """send_reminders pulls user rows and calls _remind_user for each.
    Mock at the SQL boundary so we don't need a real DB."""
    fake_session = AsyncMock()
    fake_result = MagicMock()
    fake_result.all.return_value = [
        (1, "Europe/Belgrade", "alice"),
        (2, "Europe/Moscow", "bob"),
    ]
    fake_session.execute = AsyncMock(return_value=fake_result)

    @asynccontextmanager
    async def _fake_get_session():
        yield fake_session

    with (
        patch("app.services.reminder_service.get_session", _fake_get_session),
        patch(
            "app.services.reminder_service._remind_user",
            new=AsyncMock(return_value=True),
        ) as remind,
    ):
        sent = await send_reminders(MagicMock())
    assert sent == 2
    assert remind.await_count == 2


@pytest.mark.asyncio
async def test_send_reminders_swallows_per_user_exceptions():
    """One user blowing up must not abort the whole batch."""
    fake_session = AsyncMock()
    fake_result = MagicMock()
    fake_result.all.return_value = [
        (1, "Europe/Belgrade", "alice"),
        (2, "Europe/Moscow", "bob"),
    ]
    fake_session.execute = AsyncMock(return_value=fake_result)

    @asynccontextmanager
    async def _fake_get_session():
        yield fake_session

    remind = AsyncMock(side_effect=[RuntimeError("alice down"), True])
    with (
        patch("app.services.reminder_service.get_session", _fake_get_session),
        patch("app.services.reminder_service._remind_user", new=remind),
    ):
        sent = await send_reminders(MagicMock())
    assert sent == 1  # only bob succeeded
    assert remind.await_count == 2
