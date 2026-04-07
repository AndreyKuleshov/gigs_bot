"""Bot and Dispatcher factory."""

import os

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.fsm.storage.memory import MemoryStorage

from app.bot.handlers import button_mode, common, text_mode
from app.bot.middlewares.db_session import DbSessionMiddleware
from app.core.config import settings


def create_bot() -> Bot:
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
    # PythonAnywhere (and some other hosts) route outbound traffic through a
    # proxy. aiohttp does NOT pick up system proxy env vars automatically, so
    # we pass it explicitly when available.
    proxy = (
        settings.proxy_url
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("https_proxy")
        or os.environ.get("HTTP_PROXY")
        or os.environ.get("http_proxy")
    )
    if proxy:
        return Bot(token=settings.telegram_bot_token, session=AiohttpSession(proxy=proxy))
    return Bot(token=settings.telegram_bot_token)


def create_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())

    # Middleware – injects AsyncSession into each update's data dict
    dp.update.middleware(DbSessionMiddleware())

    # Routers must be registered in priority order:
    #   common → button_mode → text_mode
    # button_mode FSM state filters take priority over text_mode's plain F.text
    dp.include_router(common.router)
    dp.include_router(button_mode.router)
    dp.include_router(text_mode.router)

    return dp
