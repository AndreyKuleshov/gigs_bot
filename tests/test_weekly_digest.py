"""Tests for the weekly digest service."""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.services.calendar_service import EventRead
from app.services.reminder_service import send_weekly_digest_to_user, tick_weekly_digests

TZ_NAME = "Europe/Belgrade"
TZ = ZoneInfo(TZ_NAME)

_NOW = datetime.now(tz=TZ)
PASS_HOUR = _NOW.hour
BLOCK_HOUR = (PASS_HOUR + 1) % 24
PASS_DOW = _NOW.weekday()
BLOCK_DOW = (PASS_DOW + 1) % 7
CURRENT_MONDAY = _NOW.date() - timedelta(days=_NOW.weekday())


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
        settings_mock.weekly_digest_hour = PASS_HOUR
        settings_mock.weekly_digest_dow = PASS_DOW
        yield SimpleNamespace(auth=auth, cal=cal, settings=settings_mock, session=captured_session)


@pytest.mark.asyncio
async def test_skips_on_wrong_dow(deps, mock_bot):
    deps.settings.weekly_digest_dow = BLOCK_DOW
    sent = await send_weekly_digest_to_user(
        mock_bot, user_id=1, tz_name=TZ_NAME, last_sent_monday=None
    )
    assert sent is False
    mock_bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_skips_on_wrong_hour(deps, mock_bot):
    deps.settings.weekly_digest_hour = BLOCK_HOUR
    sent = await send_weekly_digest_to_user(
        mock_bot, user_id=1, tz_name=TZ_NAME, last_sent_monday=None
    )
    assert sent is False
    mock_bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_skips_if_already_sent_this_week(deps, mock_bot):
    sent = await send_weekly_digest_to_user(
        mock_bot, user_id=1, tz_name=TZ_NAME, last_sent_monday=CURRENT_MONDAY
    )
    assert sent is False
    mock_bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_sends_when_gate_open_and_not_sent(deps, mock_bot):
    sent = await send_weekly_digest_to_user(
        mock_bot, user_id=1, tz_name=TZ_NAME, last_sent_monday=None
    )
    assert sent is True
    mock_bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_empty_week_says_nothing_planned(deps, mock_bot):
    deps.cal.list_events = AsyncMock(return_value=[])
    await send_weekly_digest_to_user(mock_bot, user_id=1, tz_name=TZ_NAME, last_sent_monday=None)
    args, _ = mock_bot.send_message.await_args
    body = args[1]
    assert "ничего не запланировано" in body.lower()


@pytest.mark.asyncio
async def test_non_empty_groups_by_day(deps, mock_bot):
    """Events on different days must appear under separate day headers."""
    # Two events on different days of THIS week — pick Mon and Wed of the
    # current week so the test stays valid regardless of when it runs.
    mon = CURRENT_MONDAY
    wed = CURRENT_MONDAY + timedelta(days=2)
    events = [
        EventRead(
            event_id="a",
            summary="Standup",
            start=datetime(mon.year, mon.month, mon.day, 10, 0, tzinfo=TZ),
            end=datetime(mon.year, mon.month, mon.day, 10, 30, tzinfo=TZ),
            location=None,
        ),
        EventRead(
            event_id="b",
            summary="Design review",
            start=datetime(wed.year, wed.month, wed.day, 14, 0, tzinfo=TZ),
            end=datetime(wed.year, wed.month, wed.day, 15, 0, tzinfo=TZ),
            location="HQ",
        ),
    ]
    deps.cal.list_events = AsyncMock(return_value=events)
    await send_weekly_digest_to_user(mock_bot, user_id=1, tz_name=TZ_NAME, last_sent_monday=None)
    args, _ = mock_bot.send_message.await_args
    body = args[1]
    assert "Standup" in body
    assert "Design review" in body
    assert "Понедельник" in body
    assert "Среда" in body
    # Location only on the second event.
    assert "HQ" in body


@pytest.mark.asyncio
async def test_force_bypasses_dow_and_hour_gates(deps, mock_bot):
    deps.settings.weekly_digest_dow = BLOCK_DOW
    deps.settings.weekly_digest_hour = BLOCK_HOUR
    sent = await send_weekly_digest_to_user(
        mock_bot, user_id=1, tz_name=TZ_NAME, last_sent_monday=CURRENT_MONDAY, force=True
    )
    assert sent is True
    mock_bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_calendar_window_is_mon_to_sun(deps, mock_bot):
    """list_events must be called with time_min=this-Monday 00:00 and
    time_max=next-Monday 00:00 in the user's tz."""
    await send_weekly_digest_to_user(mock_bot, user_id=1, tz_name=TZ_NAME, last_sent_monday=None)
    kwargs = deps.cal.list_events.await_args.kwargs
    assert kwargs["time_min"].date() == CURRENT_MONDAY
    assert kwargs["time_max"].date() == CURRENT_MONDAY + timedelta(days=7)
    assert kwargs["time_min"].tzinfo is not None


@pytest.mark.asyncio
async def test_tick_iterates_all_authed_users(mock_bot):
    """tick_weekly_digests should look up users via get_session and dispatch
    one send_weekly_digest_to_user per authed user."""
    captured_session = AsyncMock()
    # Mock SQLAlchemy result.all() return.
    rows = [
        (1, TZ_NAME, "alice", "Alice", None),
        (2, TZ_NAME, "bob", "Bob", CURRENT_MONDAY),  # already sent → no-op
    ]
    captured_session.execute = AsyncMock(return_value=MagicMock(all=lambda: rows))

    @asynccontextmanager
    async def _fake_get_session():
        yield captured_session

    with (
        patch("app.services.reminder_service.get_session", _fake_get_session),
        patch(
            "app.services.reminder_service.send_weekly_digest_to_user",
            new_callable=AsyncMock,
        ) as send_mock,
    ):
        send_mock.side_effect = [True, False]
        sent = await tick_weekly_digests(mock_bot)
    assert sent == 1
    assert send_mock.await_count == 2
