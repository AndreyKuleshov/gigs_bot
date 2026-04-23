"""Background cron scheduler for event reminders."""

import asyncio
import logging
from datetime import UTC, datetime

from aiogram import Bot
from croniter import croniter

from app.core.config import settings
from app.services.reminder_service import send_reminders, tick_daily_digests

logger = logging.getLogger(__name__)


async def start_scheduler(bot: Bot) -> None:
    """Run reminders on the cron schedule defined by REMINDER_CRON.

    Blocks forever (meant to be launched as a background task).
    Does nothing and returns immediately if REMINDER_CRON is empty.
    """
    cron_expr = settings.reminder_cron
    if not cron_expr:
        logger.info("REMINDER_CRON not set — scheduler disabled")
        return

    logger.info("Reminder scheduler started (cron: %s)", cron_expr)
    cron = croniter(cron_expr, datetime.now(tz=UTC))

    while True:
        next_run = cron.get_next(datetime)
        delay = (next_run - datetime.now(tz=UTC)).total_seconds()
        if delay > 0:
            await asyncio.sleep(delay)

        logger.info("Running scheduled reminders…")
        try:
            sent = await send_reminders(bot)
            logger.info("Reminders sent to %d user(s)", sent)
        except Exception:
            logger.exception("Scheduled reminder failed")


async def start_daily_digest_scheduler(bot: Bot) -> None:
    """Per-minute pulse: send each user their daily digest at DAILY_DIGEST_HOUR
    in their own timezone. Idempotent via ``User.last_daily_sent_date``.

    Blocks forever; meant to be launched as a background task.
    """
    interval = settings.daily_digest_poll_seconds
    logger.info(
        "Daily digest scheduler started (hour=%d local, poll=%ds)",
        settings.daily_digest_hour,
        interval,
    )
    while True:
        try:
            sent = await tick_daily_digests(bot)
            if sent:
                logger.info("Daily digest sent to %d user(s)", sent)
        except Exception as exc:
            # Fallback to WARNING so a transient tick failure doesn't spam
            # the PA error log every 60s. Includes the exception class and
            # message; omit stack trace unless DEBUG is enabled.
            logger.warning("Daily digest tick failed: %s: %s", type(exc).__name__, exc)
        await asyncio.sleep(interval)
