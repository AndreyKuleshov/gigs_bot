"""Tests for config and settings."""

from app.core.config import Settings


class TestSettings:
    def test_defaults(self):
        s = Settings(
            telegram_bot_token="test",
            fernet_key="test",
            reminder_cron="",
            proxy_url="",
        )
        assert s.openai_model == "gpt-4o-mini"
        assert s.api_port == 8000
        assert s.reminder_cron == ""
        assert s.proxy_url == ""

    def test_google_scopes_default(self):
        s = Settings(telegram_bot_token="t", fernet_key="k")
        assert "https://www.googleapis.com/auth/calendar" in s.google_scopes
