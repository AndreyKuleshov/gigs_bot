"""Background cron scheduler for event reminders."""

import asyncio
import logging
from datetime import UTC, datetime

from aiogram import Bot
from croniter import croniter

from app.core.config import settings
from app.services.reminder_service import send_reminders

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
