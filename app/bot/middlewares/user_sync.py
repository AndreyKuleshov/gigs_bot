"""Middleware that keeps ``User.username`` / ``User.full_name`` in sync with
Telegram on every incoming update.

Also creates the ``User`` row for first-time interactions, so individual
handlers (``/start``, ``/auth``, …) can assume the row exists.
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from app.services.auth_service import auth_service

logger = logging.getLogger(__name__)

_USER_SOURCES = (
    "message",
    "edited_message",
    "callback_query",
    "inline_query",
    "my_chat_member",
    "chat_member",
)


class UserSyncMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        from_user = None
        for attr in _USER_SOURCES:
            obj = getattr(event, attr, None)
            if obj is not None and getattr(obj, "from_user", None):
                from_user = obj.from_user
                break
        if from_user is not None:
            try:
                await auth_service.upsert_user_info(
                    from_user.id,
                    username=from_user.username,
                    full_name=from_user.full_name,
                )
            except Exception:
                logger.exception(
                    "Failed to sync user info for %d (@%s)",
                    from_user.id,
                    from_user.username or "—",
                )
        return await handler(event, data)
