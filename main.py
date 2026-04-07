"""Entry point.

Starts the FastAPI/uvicorn server. The Telegram bot runs via webhook (production)
or long-polling (local dev, when WEBHOOK_URL is unset).
"""

import asyncio
import logging

import uvicorn

from app.api.app import create_app
from app.core.config import settings

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def main() -> None:
    if not settings.telegram_bot_token:
        logger.error("TELEGRAM_BOT_TOKEN is not set – exiting.")
        return

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


if __name__ == "__main__":
    asyncio.run(main())
