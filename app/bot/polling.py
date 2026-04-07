"""Long-polling runner — used only in local development (when WEBHOOK_URL is unset)."""

import logging

from aiogram import Bot, Dispatcher

logger = logging.getLogger(__name__)


async def start_polling(bot: Bot, dp: Dispatcher) -> None:
    logger.info("Starting Telegram bot (long-polling)…")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()
