"""FastAPI dependency callables."""

from typing import Annotated

from fastapi import Header, HTTPException
from google.oauth2.credentials import Credentials

from app.services.auth_service import auth_service


async def get_current_credentials(
    x_telegram_user_id: Annotated[str | None, Header()] = None,
) -> Credentials:
    """Extract and validate Google credentials from the X-Telegram-User-Id header."""
    if not x_telegram_user_id:
        raise HTTPException(status_code=401, detail="X-Telegram-User-Id header is required")
    try:
        user_id = int(x_telegram_user_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail="X-Telegram-User-Id must be an integer"
        ) from exc

    credentials = await auth_service.get_credentials(user_id)
    if credentials is None:
        raise HTTPException(
            status_code=401,
            detail="User is not authenticated with Google. Use /auth in the bot.",
        )
    return credentials
