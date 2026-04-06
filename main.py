"""Entry point.

Starts both the aiogram long-polling bot and the FastAPI/uvicorn server
in the same asyncio event loop using asyncio.gather.
"""

import asyncio
import logging

import uvicorn

from app.api.app import create_app
from app.bot.setup import create_bot, create_dispatcher
from app.core.config import settings

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def run_bot() -> None:
    bot = create_bot()
    dp = create_dispatcher()
    logger.info("Starting Telegram bot (long-polling)…")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()


async def run_api() -> None:
    app = create_app()
    config = uvicorn.Config(
        app,
        host=settings.api_host,
        port=settings.api_port,
        log_level="debug" if settings.debug else "info",
    )
    server = uvicorn.Server(config)
    logger.info("Starting FastAPI on %s:%d…", settings.api_host, settings.api_port)
    await server.serve()


async def main() -> None:
    if not settings.telegram_bot_token:
        logger.error("TELEGRAM_BOT_TOKEN is not set – exiting.")
        return

    await asyncio.gather(run_bot(), run_api())


if __name__ == "__main__":
    asyncio.run(main())
