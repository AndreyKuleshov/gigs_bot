"""Debounce in text_mode: several quick messages collapse to one AI call."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.bot.handlers import text_mode


@pytest.fixture(autouse=True)
def _reset_buffers():
    """Each test starts with empty per-user debounce state."""
    text_mode._debounce_messages.clear()
    text_mode._debounce_states.clear()
    text_mode._debounce_tasks.clear()
    yield
    # Cancel any in-flight tasks so they don't leak between tests.
    for t in list(text_mode._debounce_tasks.values()):
        t.cancel()
    text_mode._debounce_messages.clear()
    text_mode._debounce_states.clear()
    text_mode._debounce_tasks.clear()


def _make_message(user_id: int, text: str) -> MagicMock:
    msg = MagicMock()
    msg.from_user = SimpleNamespace(id=user_id)
    msg.text = text
    msg.answer = AsyncMock()
    return msg


@pytest.mark.asyncio
async def test_three_quick_messages_merge_into_one_ai_call():
    state = AsyncMock()
    state.clear = AsyncMock()
    state.set_state = AsyncMock()
    state.update_data = AsyncMock()

    process = AsyncMock(name="_process_text")
    with (
        patch("app.bot.handlers.text_mode.auth_service") as auth,
        patch("app.bot.handlers.text_mode._process_text", new=process),
        patch("app.bot.handlers.text_mode.settings") as s,
    ):
        auth.is_authenticated = AsyncMock(return_value=True)
        auth.get_calendar_id = AsyncMock(return_value="primary")
        s.text_debounce_seconds = 0.05  # 50ms — fast enough for unit tests

        m1 = _make_message(1, "что")
        m2 = _make_message(1, "сегодня")
        m3 = _make_message(1, "в календаре")
        await text_mode.handle_free_text(m1, state)
        await text_mode.handle_free_text(m2, state)
        await text_mode.handle_free_text(m3, state)
        # Wait past the debounce window
        await asyncio.sleep(0.15)

    process.assert_awaited_once()
    assert process.await_args is not None
    args = process.await_args.args
    # _process_text(user_id, anchor_message, merged_text, state)
    assert args[0] == 1
    assert args[1] is m3  # latest message used as reply anchor
    assert args[2] == "что\nсегодня\nв календаре"


@pytest.mark.asyncio
async def test_single_message_processed_after_debounce_window():
    state = AsyncMock()
    state.clear = AsyncMock()
    process = AsyncMock(name="_process_text")
    with (
        patch("app.bot.handlers.text_mode.auth_service") as auth,
        patch("app.bot.handlers.text_mode._process_text", new=process),
        patch("app.bot.handlers.text_mode.settings") as s,
    ):
        auth.is_authenticated = AsyncMock(return_value=True)
        auth.get_calendar_id = AsyncMock(return_value="primary")
        s.text_debounce_seconds = 0.05

        m1 = _make_message(2, "hello")
        await text_mode.handle_free_text(m1, state)
        await asyncio.sleep(0.15)

    process.assert_awaited_once()
    assert process.await_args is not None
    assert process.await_args.args[2] == "hello"


@pytest.mark.asyncio
async def test_debounce_zero_runs_inline():
    """text_debounce_seconds=0 must skip the buffer and call AI immediately."""
    state = AsyncMock()
    state.clear = AsyncMock()
    process = AsyncMock(name="_process_text")
    with (
        patch("app.bot.handlers.text_mode.auth_service") as auth,
        patch("app.bot.handlers.text_mode._process_text", new=process),
        patch("app.bot.handlers.text_mode.settings") as s,
    ):
        auth.is_authenticated = AsyncMock(return_value=True)
        auth.get_calendar_id = AsyncMock(return_value="primary")
        s.text_debounce_seconds = 0

        m = _make_message(3, "now")
        await text_mode.handle_free_text(m, state)

    process.assert_awaited_once()
    assert process.await_args is not None
    assert process.await_args.args[2] == "now"
    assert text_mode._debounce_messages == {}


@pytest.mark.asyncio
async def test_two_users_debounced_independently():
    state = AsyncMock()
    state.clear = AsyncMock()
    process = AsyncMock(name="_process_text")
    with (
        patch("app.bot.handlers.text_mode.auth_service") as auth,
        patch("app.bot.handlers.text_mode._process_text", new=process),
        patch("app.bot.handlers.text_mode.settings") as s,
    ):
        auth.is_authenticated = AsyncMock(return_value=True)
        auth.get_calendar_id = AsyncMock(return_value="primary")
        s.text_debounce_seconds = 0.05

        await text_mode.handle_free_text(_make_message(1, "a1"), state)
        await text_mode.handle_free_text(_make_message(2, "b1"), state)
        await text_mode.handle_free_text(_make_message(1, "a2"), state)
        await asyncio.sleep(0.15)

    assert process.await_count == 2
    merged_per_user = {call.args[0]: call.args[2] for call in process.await_args_list}
    assert merged_per_user[1] == "a1\na2"
    assert merged_per_user[2] == "b1"


@pytest.mark.asyncio
async def test_unauthenticated_message_skips_debounce_and_warns():
    state = AsyncMock()
    state.clear = AsyncMock()
    process = AsyncMock(name="_process_text")
    with (
        patch("app.bot.handlers.text_mode.auth_service") as auth,
        patch("app.bot.handlers.text_mode._process_text", new=process),
        patch("app.bot.handlers.text_mode.settings") as s,
    ):
        auth.is_authenticated = AsyncMock(return_value=False)
        s.text_debounce_seconds = 0.05

        m = _make_message(7, "anything")
        await text_mode.handle_free_text(m, state)
        await asyncio.sleep(0.15)

    process.assert_not_called()
    m.answer.assert_awaited_once()
    assert m.answer.await_args is not None
    body = m.answer.await_args.args[0]
    assert "/auth" in body
