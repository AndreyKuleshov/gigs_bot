"""Tests for the auth-failure handling in button_mode."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from google.auth.exceptions import RefreshError

from app.bot.handlers.button_mode import _handle_auth_failure, _is_auth_error


class TestIsAuthError:
    def test_refresh_error_is_auth(self):
        assert _is_auth_error(RefreshError("token revoked")) is True

    def test_runtime_with_401_is_auth(self):
        assert _is_auth_error(RuntimeError("Google Calendar error: 401 Unauthorized")) is True

    def test_runtime_with_403_is_auth(self):
        assert _is_auth_error(RuntimeError("Google Calendar error: 403 Forbidden")) is True

    def test_runtime_with_500_is_not_auth(self):
        assert _is_auth_error(RuntimeError("Google Calendar error: 500 Server Error")) is False

    def test_runtime_network_error_is_not_auth(self):
        assert _is_auth_error(RuntimeError("Network error — please try again.")) is False

    def test_value_error_is_not_auth(self):
        assert _is_auth_error(ValueError("bad input")) is False


@pytest.mark.asyncio
async def test_handle_auth_failure_clears_state_and_renders_oauth_link():
    state = AsyncMock()
    state.clear = AsyncMock()

    msg = MagicMock()
    msg.edit_text = AsyncMock()

    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=42)
    callback.answer = AsyncMock()

    with patch("app.bot.handlers.button_mode.auth_service") as auth:
        auth.is_authenticated = AsyncMock(return_value=True)
        auth.revoke_tokens = AsyncMock()
        auth.get_auth_url = AsyncMock(return_value="https://oauth/abc")
        await _handle_auth_failure(callback, state, msg)

    state.clear.assert_awaited_once()
    auth.revoke_tokens.assert_awaited_once_with(42)
    auth.get_auth_url.assert_awaited_once_with(42)
    msg.edit_text.assert_awaited_once()
    args, kwargs = msg.edit_text.await_args
    body = args[0]
    keyboard = kwargs["reply_markup"]
    assert "expired" in body or "missing" in body
    # Inline keyboard: first row has the OAuth URL button
    first_button = keyboard.inline_keyboard[0][0]
    assert first_button.url == "https://oauth/abc"
    callback.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_auth_failure_skips_revoke_when_no_tokens():
    """If user is not currently authenticated, don't try to revoke."""
    state = AsyncMock()
    state.clear = AsyncMock()
    msg = MagicMock()
    msg.edit_text = AsyncMock()
    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=1)
    callback.answer = AsyncMock()

    with patch("app.bot.handlers.button_mode.auth_service") as auth:
        auth.is_authenticated = AsyncMock(return_value=False)
        auth.revoke_tokens = AsyncMock()
        auth.get_auth_url = AsyncMock(return_value="https://oauth/x")
        await _handle_auth_failure(callback, state, msg)

    auth.revoke_tokens.assert_not_called()
    msg.edit_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_auth_failure_falls_back_when_oauth_url_fails():
    """If get_auth_url itself raises, still tell the user to /auth — don't crash."""
    state = AsyncMock()
    state.clear = AsyncMock()
    msg = MagicMock()
    msg.edit_text = AsyncMock()
    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=1)
    callback.answer = AsyncMock()

    with patch("app.bot.handlers.button_mode.auth_service") as auth:
        auth.is_authenticated = AsyncMock(return_value=False)
        auth.get_auth_url = AsyncMock(side_effect=RuntimeError("OAuth provider down"))
        await _handle_auth_failure(callback, state, msg)

    # Two edit attempts: the rich keyboard one (raised inside try) and the
    # plain-text fallback. Plain fallback must succeed.
    assert msg.edit_text.await_count >= 1
    final_call = msg.edit_text.await_args_list[-1]
    assert "/auth" in final_call.args[0]
    callback.answer.assert_awaited_once()
