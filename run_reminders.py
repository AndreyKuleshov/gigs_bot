#!/usr/bin/env python3
"""Standalone reminder runner for PythonAnywhere scheduled tasks.

Usage (PythonAnywhere scheduled task command):
  /home/greenolls/gigs_bot/.venv/bin/python /home/greenolls/gigs_bot/run_reminders.py
"""

import asyncio
import logging

from app.bot.setup import create_bot
from app.db.base import create_tables
from app.services.reminder_service import send_reminders

logging.basicConfig(level=logging.INFO)


async def main() -> None:
    await create_tables()
    bot = create_bot()
    try:
        sent = await send_reminders(bot)
        logging.info("Reminders sent to %d user(s)", sent)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
