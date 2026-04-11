"""Bot and Dispatcher factory."""

import asyncio
import logging
import os
from typing import Any, cast

import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramNetworkError
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ErrorEvent

from app.bot.handlers import button_mode, common, text_mode
from app.bot.middlewares.db_session import DbSessionMiddleware
from app.core.config import settings

logger = logging.getLogger(__name__)

# Sensible default: 30s total, 10s to establish connection.
# Without an explicit timeout the aiohttp session can hang indefinitely.
_BOT_TIMEOUT = aiohttp.ClientTimeout(total=30, connect=10)
_PROXY_RETRIES = 5
_PROXY_RETRY_DELAY = 1.0


class _NativeProxySession(AiohttpSession):
    """AiohttpSession that passes proxy= at the request level.

    aiohttp_socks.ProxyConnector (the approach used by AiohttpSession's
    built-in proxy= support) can block the asyncio event loop on some
    platforms (e.g. PythonAnywhere uWSGI).  Using aiohttp's native
    per-request proxy= avoids the ProxyConnector entirely.
    """

    def __init__(self, proxy_url: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)  # no proxy= → no ProxyConnector created
        self._native_proxy = proxy_url

    async def make_request(  # type: ignore[override]
        self,
        bot: Bot,
        method: Any,
        timeout: Any = None,
    ) -> Any:
        effective_timeout = _BOT_TIMEOUT if timeout is None else timeout
        last_exc: BaseException | None = None
        for attempt in range(_PROXY_RETRIES + 1):
            session = await self.create_session()
            url = self.api.api_url(token=bot.token, method=method.__api_method__)
            form = self.build_form_data(bot=bot, method=method)
            try:
                async with session.post(
                    url,
                    data=form,
                    timeout=effective_timeout,
                    proxy=self._native_proxy,
                ) as resp:
                    raw_result = await resp.text()
                response = self.check_response(
                    bot=bot, method=method, status_code=resp.status, content=raw_result
                )
                return cast(Any, response.result)
            except TimeoutError as exc:
                last_exc = exc
            except aiohttp.ClientError as exc:
                last_exc = exc
            if attempt < _PROXY_RETRIES:
                logger.warning(
                    "Proxy error (attempt %d/%d): %s",
                    attempt + 1,
                    _PROXY_RETRIES + 1,
                    last_exc,
                )
                await asyncio.sleep(_PROXY_RETRY_DELAY * (attempt + 1))
        raise TelegramNetworkError(method=method, message=f"{type(last_exc).__name__}: {last_exc}")


def create_bot() -> Bot:
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
    proxy = (
        settings.proxy_url
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("https_proxy")
        or os.environ.get("HTTP_PROXY")
        or os.environ.get("http_proxy")
    )
    if proxy:
        # Use native proxy session to avoid aiohttp_socks blocking the loop.
        session: AiohttpSession = _NativeProxySession(proxy_url=proxy)
    else:
        session = AiohttpSession()
    return Bot(token=settings.telegram_bot_token, session=session)


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

    @dp.errors()
    async def _global_error_handler(event: ErrorEvent) -> bool:
        logger.exception("Unhandled error: %s", event.exception)
        # Try to notify the user
        update = event.update
        chat_id: int | None = None
        if update.message:
            chat_id = update.message.chat.id
        elif update.callback_query and update.callback_query.message:
            chat_id = update.callback_query.message.chat.id
        if chat_id and event.update.bot:
            try:
                await event.update.bot.send_message(
                    chat_id,
                    "⚠️ Произошла ошибка, попробуй ещё раз.",
                )
            except Exception:
                pass  # can't reach user — already logged above
        if update.callback_query:
            try:
                await update.callback_query.answer()
            except Exception:
                pass
        return True  # mark as handled

    return dp
