"""Tests for scheduler entry-point guards (empty cron → no-op)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.bot import scheduler as sched


@pytest.mark.asyncio
async def test_start_scheduler_returns_when_cron_empty():
    bot = MagicMock()
    with patch.object(sched.settings, "reminder_cron", ""):
        await sched.start_scheduler(bot)
    # Should return without scheduling; no exception, no pending tasks.


@pytest.mark.asyncio
async def test_start_daily_digest_returns_when_cron_empty():
    bot = MagicMock()
    with patch.object(sched.settings, "daily_digest_cron", ""):
        await sched.start_daily_digest_scheduler(bot)


@pytest.mark.asyncio
async def test_start_scheduler_runs_one_iteration():
    """Make croniter return immediate next-run, run send_reminders once,
    cancel after the first iteration via an exception."""
    bot = MagicMock()
    send_reminders_mock = AsyncMock(return_value=2)

    # Force the loop to break after one iteration by raising on the second cron pick.
    fake_cron = MagicMock()
    fake_cron.get_next.side_effect = [
        # iteration 1: pretend next run is "now" so sleep is skipped
        __import__("datetime").datetime(2026, 1, 1, tzinfo=__import__("datetime").UTC),
        # iteration 2: blow up to exit the loop
        RuntimeError("stop"),
    ]

    with (
        patch.object(sched.settings, "reminder_cron", "0 9 * * *"),
        patch.object(sched, "croniter", return_value=fake_cron),
        patch.object(sched, "send_reminders", new=send_reminders_mock),
        pytest.raises(RuntimeError, match="stop"),
    ):
        await sched.start_scheduler(bot)
    send_reminders_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_daily_digest_warning_on_failed_tick():
    """A failing tick logs WARNING but does not crash the loop."""
    bot = MagicMock()
    tick_mock = AsyncMock(side_effect=RuntimeError("boom"))

    fake_cron = MagicMock()
    fake_cron.get_next.side_effect = [
        __import__("datetime").datetime(2026, 1, 1, tzinfo=__import__("datetime").UTC),
        KeyboardInterrupt(),  # exit the loop
    ]

    with (
        patch.object(sched.settings, "daily_digest_cron", "0 * * * *"),
        patch.object(sched, "croniter", return_value=fake_cron),
        patch.object(sched, "tick_daily_digests", new=tick_mock),
        pytest.raises(KeyboardInterrupt),
    ):
        await sched.start_daily_digest_scheduler(bot)
    tick_mock.assert_awaited_once()
