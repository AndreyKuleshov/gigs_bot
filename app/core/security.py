"""Token encryption helpers using Fernet symmetric encryption."""

import json
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings


def _cipher() -> Fernet:
    if not settings.fernet_key:
        raise RuntimeError(
            "FERNET_KEY is not set. "
            'Generate one with: python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"'
        )
    return Fernet(settings.fernet_key.encode())


def encrypt_json(data: dict[str, Any]) -> str:
    """Serialize *data* to JSON and encrypt it; returns a UTF-8 string."""
    return _cipher().encrypt(json.dumps(data).encode()).decode()


def decrypt_json(token: str) -> dict[str, Any]:
    """Decrypt and deserialize a token produced by :func:`encrypt_json`.

    Raises :class:`ValueError` if the token is invalid or tampered with.
    """
    try:
        return json.loads(_cipher().decrypt(token.encode()))
    except (InvalidToken, json.JSONDecodeError) as exc:
        raise ValueError("Cannot decrypt token") from exc
