"""Google OAuth2 authentication service.

Flow:
  1. Bot sends user to :meth:`AuthService.get_auth_url`.
  2. User authorises in browser and is redirected to FastAPI callback.
  3. FastAPI calls :meth:`AuthService.handle_oauth_callback`.
  4. Encrypted tokens are stored in PostgreSQL.
  5. Bot retrieves credentials via :meth:`AuthService.get_credentials`.
"""

import asyncio
import logging
import secrets

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from sqlalchemy import select

from app.cache.redis_client import get_redis
from app.core.config import settings
from app.core.security import decrypt_json, encrypt_json
from app.db.base import get_session
from app.db.models import User

logger = logging.getLogger(__name__)

_STATE_PREFIX = "oauth_state:"
_STATE_TTL_SECONDS = 600  # 10 minutes


def _build_flow() -> Flow:
    client_config = {
        "web": {
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "redirect_uris": [settings.google_redirect_uri],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }
    return Flow.from_client_config(client_config, scopes=settings.google_scopes)


class AuthService:
    # ──────────────────────────── OAuth flow ──────────────────────────────────

    async def get_auth_url(self, telegram_user_id: int) -> str:
        """Store a random state in Redis and return the Google authorisation URL."""
        state = secrets.token_urlsafe(32)
        redis = get_redis()
        await redis.setex(
            f"{_STATE_PREFIX}{state}",
            _STATE_TTL_SECONDS,
            str(telegram_user_id),
        )
        flow = _build_flow()
        flow.redirect_uri = settings.google_redirect_uri
        auth_url, _ = flow.authorization_url(
            state=state,
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
        )
        return auth_url

    async def handle_oauth_callback(self, code: str, state: str) -> int:
        """Exchange an authorisation code for tokens and persist them.

        Returns the Telegram user_id associated with *state*.
        Raises :class:`ValueError` if the state is unknown or expired.
        """
        redis = get_redis()
        raw = await redis.get(f"{_STATE_PREFIX}{state}")
        if raw is None:
            raise ValueError("OAuth state is expired or invalid")
        telegram_user_id = int(raw)
        await redis.delete(f"{_STATE_PREFIX}{state}")

        flow = _build_flow()
        flow.redirect_uri = settings.google_redirect_uri
        # fetch_token is a synchronous HTTP call – offload to thread pool
        await asyncio.to_thread(flow.fetch_token, code=code)
        creds = flow.credentials

        token_data = {
            "token": creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri": getattr(creds, "token_uri", "https://oauth2.googleapis.com/token"),
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "scopes": list(creds.scopes or settings.google_scopes),
        }
        encrypted = encrypt_json(token_data)

        async with get_session() as session:
            user = await session.get(User, telegram_user_id)
            if user is None:
                user = User(id=telegram_user_id)
                session.add(user)
            user.google_tokens_encrypted = encrypted

        logger.info("Stored OAuth tokens for user %d", telegram_user_id)
        return telegram_user_id

    # ──────────────────────────── Credentials ─────────────────────────────────

    async def get_credentials(self, telegram_user_id: int) -> Credentials | None:
        """Return valid Google credentials for *telegram_user_id*, or ``None``."""
        async with get_session() as session:
            user = await session.get(User, telegram_user_id)

        if user is None or not user.google_tokens_encrypted:
            return None

        try:
            data = decrypt_json(user.google_tokens_encrypted)
        except ValueError:
            logger.warning("Failed to decrypt tokens for user %d", telegram_user_id)
            return None

        return Credentials(
            token=data["token"],
            refresh_token=data.get("refresh_token"),
            token_uri=data.get("token_uri", "https://oauth2.googleapis.com/token"),
            client_id=data.get("client_id"),
            client_secret=data.get("client_secret"),
            scopes=data.get("scopes"),
        )

    async def is_authenticated(self, telegram_user_id: int) -> bool:
        return (await self.get_credentials(telegram_user_id)) is not None

    async def revoke_tokens(self, telegram_user_id: int) -> None:
        """Remove stored Google tokens for *telegram_user_id*."""
        async with get_session() as session:
            user = await session.get(User, telegram_user_id)
            if user is not None:
                user.google_tokens_encrypted = None

    # ──────────────────────────── User helpers ────────────────────────────────

    async def get_or_create_user(
        self,
        telegram_user_id: int,
        username: str | None = None,
        full_name: str | None = None,
    ) -> User:
        async with get_session() as session:
            user = await session.get(User, telegram_user_id)
            if user is None:
                user = User(
                    id=telegram_user_id,
                    username=username,
                    full_name=full_name,
                )
                session.add(user)
        return user

    async def get_user_mode(self, telegram_user_id: int) -> str:
        async with get_session() as session:
            result = await session.execute(select(User.mode).where(User.id == telegram_user_id))
            row = result.scalar_one_or_none()
        return row if row is not None else "button"

    async def set_user_mode(self, telegram_user_id: int, mode: str) -> None:
        async with get_session() as session:
            user = await session.get(User, telegram_user_id)
            if user is None:
                user = User(id=telegram_user_id, mode=mode)
                session.add(user)
            else:
                user.mode = mode


auth_service = AuthService()
