"""Reach the last few uncovered branches in reminder_service."""

from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from google.auth.exceptions import RefreshError

from app.services.reminder_service import (
    _generate_empty_day_message,
    _is_auth_failure,
    send_daily_digest_to_user,
    tick_daily_digests,
)

TZ_NAME = "Europe/Belgrade"
PASS_HOUR = datetime.now(tz=ZoneInfo(TZ_NAME)).hour


# ── _is_auth_failure ─────────────────────────────────────────────────────────


def test_is_auth_failure_refresh_error():
    assert _is_auth_failure(RefreshError("revoked")) is True


def test_is_auth_failure_runtime_with_401():
    assert _is_auth_failure(RuntimeError("Calendar 401 Unauthorized")) is True


def test_is_auth_failure_runtime_with_403():
    assert _is_auth_failure(RuntimeError("Calendar 403 Forbidden")) is True


def test_is_auth_failure_unrelated_runtime_returns_false():
    assert _is_auth_failure(RuntimeError("Network error — please try again.")) is False


def test_is_auth_failure_other_exception_returns_false():
    assert _is_auth_failure(ValueError("nope")) is False


# ── _generate_empty_day_message ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_day_returns_fallback_when_no_api_key():
    with patch("app.services.reminder_service.settings") as s:
        s.openai_api_key = ""
        out = await _generate_empty_day_message()
    assert "ничего не запланировано" in out.lower()


@pytest.mark.asyncio
async def test_empty_day_uses_llm_when_configured():
    """LLM returns the phrase, helper trims and forwards it."""
    fake_client = MagicMock()
    fake_client.chat.completions.create = AsyncMock(
        return_value=MagicMock(
            choices=[MagicMock(message=MagicMock(content=" Свободный день! 🎉  "))]
        )
    )
    with (
        patch("app.services.reminder_service.settings") as s,
        patch("app.services.reminder_service.AsyncOpenAI", return_value=fake_client),
    ):
        s.openai_api_key = "sk-x"
        s.openai_model = "gpt-4o-mini"
        out = await _generate_empty_day_message()
    assert out == "Свободный день! 🎉"


@pytest.mark.asyncio
async def test_empty_day_returns_fallback_when_llm_returns_blank():
    fake_client = MagicMock()
    fake_client.chat.completions.create = AsyncMock(
        return_value=MagicMock(choices=[MagicMock(message=MagicMock(content=""))])
    )
    with (
        patch("app.services.reminder_service.settings") as s,
        patch("app.services.reminder_service.AsyncOpenAI", return_value=fake_client),
    ):
        s.openai_api_key = "sk-x"
        out = await _generate_empty_day_message()
    assert "ничего не запланировано" in out.lower()


@pytest.mark.asyncio
async def test_empty_day_returns_fallback_when_llm_raises():
    """Any exception (timeout, network, API error) → static fallback."""
    fake_client = MagicMock()
    fake_client.chat.completions.create = AsyncMock(side_effect=RuntimeError("boom"))
    with (
        patch("app.services.reminder_service.settings") as s,
        patch("app.services.reminder_service.AsyncOpenAI", return_value=fake_client),
    ):
        s.openai_api_key = "sk-x"
        out = await _generate_empty_day_message()
    assert "ничего не запланировано" in out.lower()


# ── send_daily_digest auth-failure revokes tokens ────────────────────────────


@pytest.fixture
def deps():
    captured_session = AsyncMock()
    captured_session.execute = AsyncMock()

    @asynccontextmanager
    async def _fake_get_session():
        yield captured_session

    with (
        patch("app.services.reminder_service.auth_service") as auth,
        patch("app.services.reminder_service.calendar_service") as cal,
        patch("app.services.reminder_service.get_session", _fake_get_session),
        patch("app.services.reminder_service.settings") as settings_mock,
    ):
        auth.get_credentials = AsyncMock(return_value=object())
        auth.get_calendar_id = AsyncMock(return_value="primary")
        auth.revoke_tokens = AsyncMock()
        cal.list_events = AsyncMock(return_value=[])
        settings_mock.daily_digest_hour = PASS_HOUR
        yield SimpleNamespace(auth=auth, cal=cal)


@pytest.fixture
def mock_bot():
    bot = MagicMock()
    bot.send_message = AsyncMock()
    return bot


@pytest.mark.asyncio
async def test_send_daily_digest_revokes_on_refresh_error(deps, mock_bot):
    """RefreshError from list_events triggers revoke_tokens and returns False."""
    deps.cal.list_events = AsyncMock(side_effect=RefreshError("revoked"))
    sent = await send_daily_digest_to_user(mock_bot, user_id=1, tz_name=TZ_NAME, last_sent=None)
    assert sent is False
    deps.auth.revoke_tokens.assert_awaited_once_with(1)


@pytest.mark.asyncio
async def test_send_daily_digest_revoke_tokens_failure_is_swallowed(deps, mock_bot):
    """If revoke_tokens itself raises, we still return False — never raise."""
    deps.cal.list_events = AsyncMock(side_effect=RefreshError("revoked"))
    deps.auth.revoke_tokens = AsyncMock(side_effect=RuntimeError("DB down"))
    sent = await send_daily_digest_to_user(mock_bot, user_id=1, tz_name=TZ_NAME, last_sent=None)
    assert sent is False


@pytest.mark.asyncio
async def test_send_daily_digest_non_auth_list_events_failure_returns_false(deps, mock_bot):
    """500 / network errors don't revoke; just log + return False."""
    deps.cal.list_events = AsyncMock(side_effect=RuntimeError("Network error"))
    sent = await send_daily_digest_to_user(mock_bot, user_id=1, tz_name=TZ_NAME, last_sent=None)
    assert sent is False
    deps.auth.revoke_tokens.assert_not_called()


# ── tick_daily_digests iterates DB users ─────────────────────────────────────


@pytest.mark.asyncio
async def test_tick_daily_digests_iterates_and_aggregates():
    fake_session = AsyncMock()
    fake_result = MagicMock()
    fake_result.all.return_value = [
        (1, "Europe/Belgrade", "alice", "Alice", None),
        (2, "Europe/Moscow", "bob", "Bob", None),
    ]
    fake_session.execute = AsyncMock(return_value=fake_result)

    @asynccontextmanager
    async def _fake_get_session():
        yield fake_session

    with (
        patch("app.services.reminder_service.get_session", _fake_get_session),
        patch(
            "app.services.reminder_service.send_daily_digest_to_user",
            new=AsyncMock(side_effect=[True, True]),
        ),
    ):
        sent = await tick_daily_digests(MagicMock())
    assert sent == 2


@pytest.mark.asyncio
async def test_tick_daily_digests_swallows_per_user_exceptions():
    """The "Daily digest iteration bug" branch: exceptions inside the loop
    are caught and logged at WARNING."""
    fake_session = AsyncMock()
    fake_result = MagicMock()
    fake_result.all.return_value = [
        (1, "Europe/Belgrade", "alice", "Alice", None),
        (2, "Europe/Moscow", "bob", "Bob", None),
    ]
    fake_session.execute = AsyncMock(return_value=fake_result)

    @asynccontextmanager
    async def _fake_get_session():
        yield fake_session

    send = AsyncMock(side_effect=[RuntimeError("crash for user 1"), True])
    with (
        patch("app.services.reminder_service.get_session", _fake_get_session),
        patch("app.services.reminder_service.send_daily_digest_to_user", new=send),
    ):
        sent = await tick_daily_digests(MagicMock())
    assert sent == 1
    assert send.await_count == 2
