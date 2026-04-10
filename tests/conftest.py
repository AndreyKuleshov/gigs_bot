"""Shared fixtures for the test suite."""

import os

# Set minimal env vars before any app code is imported
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000000000:AAFakeTokenForTests")
os.environ.setdefault("GOOGLE_CLIENT_ID", "fake-client-id")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "fake-client-secret")
os.environ.setdefault("FERNET_KEY", "uN5G7QOJHAoEefkLBrumiB5jm19dJI7TECz878jB7A0=")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("OPENAI_API_KEY", "sk-fake-key-for-tests")
