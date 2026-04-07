from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Resolve .env relative to this file so it works regardless of the working
# directory (e.g. uWSGI on PythonAnywhere starts from /home/username/).
_ENV_FILE = Path(__file__).parent.parent.parent / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(_ENV_FILE), extra="ignore")

    # Telegram
    telegram_bot_token: str = ""

    # Google OAuth2
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://localhost:8000/auth/google/callback"
    google_scopes: list[str] = ["https://www.googleapis.com/auth/calendar"]

    # OpenAI
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"

    # Database — SQLite by default (PythonAnywhere), set to PostgreSQL for Fly.io / Docker
    database_url: str = "sqlite+aiosqlite:///./gigs_bot.db"

    # Fernet symmetric key for encrypting Google tokens at rest.
    # Generate: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"  # noqa: E501
    fernet_key: str = ""

    # Webhook (set to your public URL on Render, leave empty for local long-polling)
    webhook_url: str = ""  # e.g. https://gigs-bot.onrender.com
    webhook_secret: str = ""  # random secret to validate Telegram requests

    # FastAPI
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    debug: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings: Settings = get_settings()
