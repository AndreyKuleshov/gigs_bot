"""Tests for the daily morning digest service."""

from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.services.calendar_service import EventRead
from app.services.reminder_service import send_daily_digest_to_user

TZ_NAME = "Europe/Belgrade"
TZ = ZoneInfo(TZ_NAME)


@pytest.fixture
def mock_bot():
    bot = MagicMock()
    bot.send_message = AsyncMock()
    return bot


@pytest.fixture
def deps():
    """Patch out auth_service, calendar_service, get_session, and settings."""
    captured_session = AsyncMock()
    captured_session.execute = AsyncMock()

    @asynccontextmanager
    async def _fake_get_session():
        yield captured_session

    with (
        patch("app.services.reminder_service.auth_service") as auth,
        patch("app.services.reminder_service.calendar_service") as cal,
        patch("app.services.reminder_service.get_session", _fake_get_session),
        patch("app.services.reminder_service.settings") as settings_mock,
    ):
        auth.get_credentials = AsyncMock(return_value=object())
        auth.get_calendar_id = AsyncMock(return_value="primary")
        cal.list_events = AsyncMock(return_value=[])
        settings_mock.daily_digest_hour = 9
        yield SimpleNamespace(auth=auth, cal=cal, settings=settings_mock, session=captured_session)


@pytest.mark.asyncio
async def test_skips_before_digest_hour(deps, mock_bot):
    deps.settings.daily_digest_hour = 25  # never passes
    sent = await send_daily_digest_to_user(mock_bot, user_id=1, tz_name=TZ_NAME, last_sent=None)
    assert sent is False
    mock_bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_skips_if_already_sent_today(deps, mock_bot):
    deps.settings.daily_digest_hour = 0  # always past
    today_local = datetime.now(tz=TZ).date()
    sent = await send_daily_digest_to_user(
        mock_bot, user_id=1, tz_name=TZ_NAME, last_sent=today_local
    )
    assert sent is False
    mock_bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_sends_when_gate_open_and_not_sent(deps, mock_bot):
    deps.settings.daily_digest_hour = 0
    sent = await send_daily_digest_to_user(mock_bot, user_id=1, tz_name=TZ_NAME, last_sent=None)
    assert sent is True
    mock_bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_empty_events_sends_fallback_message(deps, mock_bot):
    deps.settings.daily_digest_hour = 0
    deps.cal.list_events = AsyncMock(return_value=[])
    await send_daily_digest_to_user(mock_bot, user_id=1, tz_name=TZ_NAME, last_sent=None)
    args, kwargs = mock_bot.send_message.await_args
    body = args[1] if len(args) > 1 else kwargs.get("text", "")
    assert "ничего не запланировано" in body.lower()


@pytest.mark.asyncio
async def test_non_empty_events_formats_list(deps, mock_bot):
    deps.settings.daily_digest_hour = 0
    start = datetime(2026, 4, 23, 14, 0, tzinfo=TZ)
    end = datetime(2026, 4, 23, 15, 30, tzinfo=TZ)
    deps.cal.list_events = AsyncMock(
        return_value=[
            EventRead(
                event_id="e1",
                summary="Padel Camp",
                start=start,
                end=end,
                location="Belgrade",
            )
        ]
    )
    await send_daily_digest_to_user(mock_bot, user_id=1, tz_name=TZ_NAME, last_sent=None)
    args, _ = mock_bot.send_message.await_args
    body = args[1]
    assert "Padel Camp" in body
    assert "14:00" in body
    assert "15:30" in body
    assert "Belgrade" in body


@pytest.mark.asyncio
async def test_force_bypasses_time_gate(deps, mock_bot):
    deps.settings.daily_digest_hour = 25  # would block
    sent = await send_daily_digest_to_user(
        mock_bot, user_id=1, tz_name=TZ_NAME, force=True, last_sent=None
    )
    assert sent is True
    mock_bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_force_bypasses_dedup(deps, mock_bot):
    deps.settings.daily_digest_hour = 0
    today_local = datetime.now(tz=TZ).date()
    sent = await send_daily_digest_to_user(
        mock_bot, user_id=1, tz_name=TZ_NAME, force=True, last_sent=today_local
    )
    assert sent is True
    mock_bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_updates_last_sent_date_on_success(deps, mock_bot):
    deps.settings.daily_digest_hour = 0
    await send_daily_digest_to_user(mock_bot, user_id=42, tz_name=TZ_NAME, last_sent=None)
    # The UPDATE statement is executed once after a successful send.
    assert deps.session.execute.await_count == 1


@pytest.mark.asyncio
async def test_no_send_when_credentials_missing(deps, mock_bot):
    deps.settings.daily_digest_hour = 0
    deps.auth.get_credentials = AsyncMock(return_value=None)
    sent = await send_daily_digest_to_user(mock_bot, user_id=1, tz_name=TZ_NAME, last_sent=None)
    assert sent is False
    mock_bot.send_message.assert_not_called()
