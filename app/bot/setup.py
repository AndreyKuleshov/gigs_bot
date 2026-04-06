"""Bot and Dispatcher factory."""

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.redis import RedisStorage

from app.bot.handlers import button_mode, common, text_mode
from app.bot.middlewares.db_session import DbSessionMiddleware
from app.cache.redis_client import get_raw_redis
from app.core.config import settings


def create_bot() -> Bot:
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
    return Bot(token=settings.telegram_bot_token)


def create_dispatcher() -> Dispatcher:
    storage = RedisStorage(redis=get_raw_redis())
    dp = Dispatcher(storage=storage)

    # Middleware – injects AsyncSession into each update's data dict
    dp.update.middleware(DbSessionMiddleware())

    # Routers must be registered in priority order:
    #   common → button_mode → text_mode
    # button_mode FSM state filters take priority over text_mode's plain F.text
    dp.include_router(common.router)
    dp.include_router(button_mode.router)
    dp.include_router(text_mode.router)

    return dp
