"""Tests for the daily morning digest service."""

from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.services.calendar_service import EventRead
from app.services.reminder_service import _greeting, send_daily_digest_to_user

TZ_NAME = "Europe/Belgrade"
TZ = ZoneInfo(TZ_NAME)


@pytest.fixture
def mock_bot():
    bot = MagicMock()
    bot.send_message = AsyncMock()
    return bot


@pytest.fixture
def deps():
    """Patch out auth_service, calendar_service, get_session, and settings."""
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
        cal.list_events = AsyncMock(return_value=[])
        settings_mock.daily_digest_hour = 9
        yield SimpleNamespace(auth=auth, cal=cal, settings=settings_mock, session=captured_session)


@pytest.mark.asyncio
async def test_skips_before_digest_hour(deps, mock_bot):
    deps.settings.daily_digest_hour = 25  # never passes
    sent = await send_daily_digest_to_user(mock_bot, user_id=1, tz_name=TZ_NAME, last_sent=None)
    assert sent is False
    mock_bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_skips_if_already_sent_today(deps, mock_bot):
    deps.settings.daily_digest_hour = 0  # always past
    today_local = datetime.now(tz=TZ).date()
    sent = await send_daily_digest_to_user(
        mock_bot, user_id=1, tz_name=TZ_NAME, last_sent=today_local
    )
    assert sent is False
    mock_bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_sends_when_gate_open_and_not_sent(deps, mock_bot):
    deps.settings.daily_digest_hour = 0
    sent = await send_daily_digest_to_user(mock_bot, user_id=1, tz_name=TZ_NAME, last_sent=None)
    assert sent is True
    mock_bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_empty_events_sends_llm_generated_message(deps, mock_bot):
    deps.settings.daily_digest_hour = 0
    deps.cal.list_events = AsyncMock(return_value=[])
    with patch(
        "app.services.reminder_service._generate_empty_day_message",
        new=AsyncMock(return_value="Ура, сегодня свободный день! 🎉"),
    ) as llm:
        await send_daily_digest_to_user(mock_bot, user_id=1, tz_name=TZ_NAME, last_sent=None)
    llm.assert_awaited_once()
    args, _ = mock_bot.send_message.await_args
    body = args[1]
    assert "Ура, сегодня свободный день! 🎉" in body


@pytest.mark.asyncio
async def test_empty_events_uses_static_fallback_when_llm_fails(deps, mock_bot):
    """If the LLM helper returns the static fallback (e.g. API down / no key),
    the bot still sends something sensible."""
    deps.settings.daily_digest_hour = 0
    deps.cal.list_events = AsyncMock(return_value=[])
    from app.services.reminder_service import _EMPTY_DAY_FALLBACK

    with patch(
        "app.services.reminder_service._generate_empty_day_message",
        new=AsyncMock(return_value=_EMPTY_DAY_FALLBACK),
    ):
        await send_daily_digest_to_user(mock_bot, user_id=1, tz_name=TZ_NAME, last_sent=None)
    args, _ = mock_bot.send_message.await_args
    body = args[1]
    assert "ничего не запланировано" in body.lower()


@pytest.mark.asyncio
async def test_non_empty_events_formats_list(deps, mock_bot):
    deps.settings.daily_digest_hour = 0
    start = datetime(2026, 4, 23, 14, 0, tzinfo=TZ)
    end = datetime(2026, 4, 23, 15, 30, tzinfo=TZ)
    deps.cal.list_events = AsyncMock(
        return_value=[
            EventRead(
                event_id="e1",
                summary="Padel Camp",
                start=start,
                end=end,
                location="Belgrade",
            )
        ]
    )
    await send_daily_digest_to_user(mock_bot, user_id=1, tz_name=TZ_NAME, last_sent=None)
    args, _ = mock_bot.send_message.await_args
    body = args[1]
    assert "Padel Camp" in body
    assert "14:00" in body
    assert "15:30" in body
    assert "Belgrade" in body


@pytest.mark.asyncio
async def test_force_bypasses_time_gate(deps, mock_bot):
    deps.settings.daily_digest_hour = 25  # would block
    sent = await send_daily_digest_to_user(
        mock_bot, user_id=1, tz_name=TZ_NAME, force=True, last_sent=None
    )
    assert sent is True
    mock_bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_force_bypasses_dedup(deps, mock_bot):
    deps.settings.daily_digest_hour = 0
    today_local = datetime.now(tz=TZ).date()
    sent = await send_daily_digest_to_user(
        mock_bot, user_id=1, tz_name=TZ_NAME, force=True, last_sent=today_local
    )
    assert sent is True
    mock_bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_updates_last_sent_date_on_success(deps, mock_bot):
    deps.settings.daily_digest_hour = 0
    await send_daily_digest_to_user(mock_bot, user_id=42, tz_name=TZ_NAME, last_sent=None)
    # The UPDATE statement is executed once after a successful send.
    assert deps.session.execute.await_count == 1


class TestGreeting:
    def test_morning(self):
        assert "Доброе утро" in _greeting("Andrei", 9)

    def test_afternoon(self):
        assert "Добрый день" in _greeting("Andrei", 14)

    def test_evening(self):
        assert "Добрый вечер" in _greeting("Andrei", 20)

    def test_night(self):
        assert "Доброй ночи" in _greeting("Andrei", 2)

    def test_includes_bolded_name(self):
        assert "<b>Andrei</b>" in _greeting("Andrei", 9)

    def test_escapes_html_in_name(self):
        # A name with HTML special chars must not break Telegram's parse_mode.
        out = _greeting("<script>", 9)
        assert "<script>" not in out
        assert "&lt;script&gt;" in out

    def test_no_name_renders_greeting_only(self):
        out = _greeting(None, 9)
        assert "Доброе утро" in out
        assert "<b>" not in out


@pytest.mark.asyncio
async def test_greeting_prepended_in_events_message(deps, mock_bot):
    deps.settings.daily_digest_hour = 0
    start = datetime(2026, 4, 23, 14, 0, tzinfo=TZ)
    end = datetime(2026, 4, 23, 15, 30, tzinfo=TZ)
    deps.cal.list_events = AsyncMock(
        return_value=[EventRead(event_id="e1", summary="Padel", start=start, end=end)]
    )
    await send_daily_digest_to_user(
        mock_bot, user_id=1, tz_name=TZ_NAME, last_sent=None, full_name="Andrei"
    )
    body = mock_bot.send_message.await_args.args[1]
    assert body.startswith(("Доброе утро", "Добрый день", "Добрый вечер", "Доброй ночи"))
    assert "<b>Andrei</b>" in body
    assert "Padel" in body


@pytest.mark.asyncio
async def test_greeting_prepended_in_empty_day_message(deps, mock_bot):
    deps.settings.daily_digest_hour = 0
    deps.cal.list_events = AsyncMock(return_value=[])
    with patch(
        "app.services.reminder_service._generate_empty_day_message",
        new=AsyncMock(return_value="Свободный день! 🎉"),
    ):
        await send_daily_digest_to_user(
            mock_bot, user_id=1, tz_name=TZ_NAME, last_sent=None, full_name="Andrei"
        )
    body = mock_bot.send_message.await_args.args[1]
    assert "<b>Andrei</b>" in body
    assert "Свободный день! 🎉" in body


@pytest.mark.asyncio
async def test_full_name_fallback_via_bot_get_chat(deps, mock_bot):
    """If User.full_name is None, fetch it from Telegram and persist."""
    deps.settings.daily_digest_hour = 0
    deps.cal.list_events = AsyncMock(return_value=[])
    mock_bot.get_chat = AsyncMock(return_value=SimpleNamespace(full_name="Andrei G"))
    with patch(
        "app.services.reminder_service._generate_empty_day_message",
        new=AsyncMock(return_value="."),
    ):
        await send_daily_digest_to_user(
            mock_bot, user_id=1, tz_name=TZ_NAME, last_sent=None, full_name=None
        )
    mock_bot.get_chat.assert_awaited_once_with(1)
    body = mock_bot.send_message.await_args.args[1]
    assert "<b>Andrei G</b>" in body


@pytest.mark.asyncio
async def test_full_name_fallback_tolerates_get_chat_failure(deps, mock_bot):
    """If bot.get_chat raises, digest still goes out — just without a name."""
    deps.settings.daily_digest_hour = 0
    deps.cal.list_events = AsyncMock(return_value=[])
    mock_bot.get_chat = AsyncMock(side_effect=RuntimeError("blocked"))
    with patch(
        "app.services.reminder_service._generate_empty_day_message",
        new=AsyncMock(return_value="Свободно!"),
    ):
        await send_daily_digest_to_user(
            mock_bot, user_id=1, tz_name=TZ_NAME, last_sent=None, full_name=None
        )
    mock_bot.send_message.assert_awaited_once()
    body = mock_bot.send_message.await_args.args[1]
    assert "Свободно!" in body


@pytest.mark.asyncio
async def test_no_send_when_credentials_missing(deps, mock_bot):
    deps.settings.daily_digest_hour = 0
    deps.auth.get_credentials = AsyncMock(return_value=None)
    sent = await send_daily_digest_to_user(mock_bot, user_id=1, tz_name=TZ_NAME, last_sent=None)
    assert sent is False
    mock_bot.send_message.assert_not_called()
