"""Redis client factory.

Two clients are provided because they serve different consumers:

- :func:`get_redis`   – string-decoded client for application code (AuthService etc.)
- :func:`get_raw_redis` – binary client required by aiogram RedisStorage

Both are lazily created singletons that share the same underlying connection pool URL.
"""

import redis.asyncio as aioredis

from app.core.config import settings

_string_client: aioredis.Redis | None = None
_binary_client: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    """Return a Redis client that decodes responses to :class:`str`."""
    global _string_client
    if _string_client is None:
        _string_client = aioredis.Redis.from_url(settings.redis_url, decode_responses=True)
    return _string_client


def get_raw_redis() -> aioredis.Redis:
    """Return a Redis client that returns raw :class:`bytes` (required by aiogram)."""
    global _binary_client
    if _binary_client is None:
        _binary_client = aioredis.Redis.from_url(settings.redis_url, decode_responses=False)
    return _binary_client


async def close_redis() -> None:
    """Close both Redis clients gracefully."""
    global _string_client, _binary_client
    for client in (_string_client, _binary_client):
        if client is not None:
            await client.aclose()
    _string_client = None
    _binary_client = None
