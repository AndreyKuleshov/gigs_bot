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

        try:
            sent = await send_reminders(bot)
            if sent:
                logger.info("Reminders sent to %d user(s)", sent)
        except Exception as exc:
            logger.warning("Scheduled reminder failed: %s: %s", type(exc).__name__, exc)


async def start_daily_digest_scheduler(bot: Bot) -> None:
    """Cron-driven daily digest tick. Per-user idempotency is guarded by
    ``User.last_daily_sent_date``, so the cron can fire often without
    double-sending.

    Blocks forever; meant to be launched as a background task. Returns
    immediately if ``DAILY_DIGEST_CRON`` is empty.
    """
    cron_expr = settings.daily_digest_cron
    if not cron_expr:
        logger.info("DAILY_DIGEST_CRON not set — daily digest disabled")
        return
    logger.info(
        "Daily digest scheduler started (cron: %s, hour=%d local)",
        cron_expr,
        settings.daily_digest_hour,
    )
    cron = croniter(cron_expr, datetime.now(tz=UTC))
    while True:
        next_run = cron.get_next(datetime)
        delay = (next_run - datetime.now(tz=UTC)).total_seconds()
        if delay > 0:
            await asyncio.sleep(delay)
        try:
            sent = await tick_daily_digests(bot)
            if sent:
                logger.info("Daily digest sent to %d user(s)", sent)
        except Exception as exc:
            logger.warning("Daily digest tick failed: %s: %s", type(exc).__name__, exc)
