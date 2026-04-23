"""Tests for AI agent logic (no API calls)."""

from datetime import datetime
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.services.ai_agent import (
    AgentResponse,
    AIAgent,
    PendingAction,
    _detect_language,
    _resolve_date_range,
)


class TestPendingAction:
    def test_dataclass(self):
        pa = PendingAction(tool_name="create_event", args={"summary": "Test"})
        assert pa.tool_name == "create_event"
        assert pa.args == {"summary": "Test"}


class TestAgentResponse:
    def test_defaults(self):
        r = AgentResponse(text="Hello")
        assert r.text == "Hello"
        assert r.image_url is None
        assert r.pending_action is None

    def test_with_pending(self):
        pa = PendingAction(tool_name="delete_event", args={"event_id": "x"})
        r = AgentResponse(text="Confirm?", pending_action=pa)
        assert r.pending_action is pa


class TestExecuteTool:
    """Test _execute_tool intercepts mutating tools."""

    @pytest.fixture
    def agent(self):
        return AIAgent()

    @pytest.mark.asyncio
    async def test_mutating_tool_intercepted(self, agent):
        image_holder: list[str] = []
        pending_holder: list[PendingAction] = []
        result = await agent._execute_tool(
            user_id=123,
            name="create_event",
            args={"summary": "Test"},
            image_holder=image_holder,
            pending_holder=pending_holder,
        )
        assert "confirmation" in result.lower()
        assert len(pending_holder) == 1
        assert pending_holder[0].tool_name == "create_event"

    @pytest.mark.asyncio
    async def test_delete_intercepted(self, agent):
        pending_holder: list[PendingAction] = []
        await agent._execute_tool(
            user_id=123,
            name="delete_event",
            args={"event_id": "abc"},
            image_holder=[],
            pending_holder=pending_holder,
        )
        assert len(pending_holder) == 1
        assert pending_holder[0].tool_name == "delete_event"

    @pytest.mark.asyncio
    async def test_update_intercepted(self, agent):
        pending_holder: list[PendingAction] = []
        await agent._execute_tool(
            user_id=123,
            name="update_event",
            args={"event_id": "abc", "summary": "New"},
            image_holder=[],
            pending_holder=pending_holder,
        )
        assert len(pending_holder) == 1

    @pytest.mark.asyncio
    async def test_read_not_intercepted(self, agent):
        pending_holder: list[PendingAction] = []
        with patch.object(agent, "_run_calendar_tool", new_callable=AsyncMock) as mock:
            mock.return_value = "events list"
            result = await agent._execute_tool(
                user_id=123,
                name="read_events",
                args={},
                image_holder=[],
                pending_holder=pending_holder,
            )
        assert len(pending_holder) == 0
        assert result == "events list"

    @pytest.mark.asyncio
    async def test_web_search_not_intercepted(self, agent):
        pending_holder: list[PendingAction] = []
        with patch("app.services.ai_agent._web_search", new_callable=AsyncMock) as mock:
            mock.return_value = "search results"
            result = await agent._execute_tool(
                user_id=123,
                name="web_search",
                args={"query": "test"},
                image_holder=[],
                pending_holder=pending_holder,
            )
        assert result == "search results"
        assert len(pending_holder) == 0


class TestFixTz:
    """Test the timezone fixing logic inside _run_calendar_tool."""

    def test_fix_tz_strips_wrong_offset(self):
        """Model returns +02:00 but user is in Europe/Moscow (+03:00 summer)."""
        user_tz = ZoneInfo("Europe/Belgrade")

        def _fix_tz(iso: str) -> datetime:
            dt = datetime.fromisoformat(iso)
            return dt.replace(tzinfo=None).replace(tzinfo=user_tz)

        result = _fix_tz("2025-06-01T15:00:00+02:00")
        # Should be 15:00 in Belgrade, NOT converted
        assert result.hour == 15
        assert result.tzinfo == user_tz

    def test_fix_tz_naive_datetime(self):
        """Model returns naive datetime — should keep the hour as-is."""
        user_tz = ZoneInfo("Europe/Belgrade")

        def _fix_tz(iso: str) -> datetime:
            dt = datetime.fromisoformat(iso)
            return dt.replace(tzinfo=None).replace(tzinfo=user_tz)

        result = _fix_tz("2025-06-01T15:00:00")
        assert result.hour == 15
        assert result.tzinfo == user_tz


class TestDetectLanguage:
    def test_cyrillic_dominant(self):
        assert _detect_language("когда концерт?") == "Russian"

    def test_latin_dominant(self):
        assert _detect_language("when is the concert?") == "English"

    def test_empty_defaults_to_russian(self):
        assert _detect_language("") == "Russian"

    def test_mixed_cyrillic_wins_on_tie(self):
        # Ties fall through to Russian by design.
        assert _detect_language("123 !!!") == "Russian"


class TestHistory:
    """Per-user conversation memory."""

    def test_note_assistant_appends_to_history(self):
        agent = AIAgent()
        agent.note_assistant(42, "hello there")
        hist = list(agent._get_history(42))
        assert len(hist) == 1
        assert hist[0].get("role") == "assistant"
        assert hist[0].get("content") == "hello there"

    def test_note_assistant_ignores_empty(self):
        agent = AIAgent()
        agent.note_assistant(42, "")
        assert len(agent._get_history(42)) == 0

    def test_history_is_per_user(self):
        agent = AIAgent()
        agent.note_assistant(1, "user 1 msg")
        agent.note_assistant(2, "user 2 msg")
        assert len(agent._get_history(1)) == 1
        assert len(agent._get_history(2)) == 1
        assert agent._get_history(1)[0].get("content") == "user 1 msg"

    def test_history_is_capped(self):
        from app.services.ai_agent import _HISTORY_TURNS

        agent = AIAgent()
        for i in range(_HISTORY_TURNS * 2 + 5):
            agent.note_assistant(1, f"msg {i}")
        hist = agent._get_history(1)
        assert len(hist) == _HISTORY_TURNS * 2
        # Oldest messages are dropped; the last one added should still be there.
        assert hist[-1].get("content") == f"msg {_HISTORY_TURNS * 2 + 4}"


class TestResolveDateRange:
    """Relative date period resolution — avoids LLM arithmetic errors."""

    TZ = ZoneInfo("Europe/Belgrade")

    def test_this_weekend_from_wednesday(self):
        # 2026-04-23 is Thursday — the bug-report case.
        now = datetime(2026, 4, 23, 11, 17, tzinfo=self.TZ)
        start, end = _resolve_date_range("this_weekend", now)
        assert start == datetime(2026, 4, 25, tzinfo=self.TZ)  # Saturday
        assert end == datetime(2026, 4, 27, tzinfo=self.TZ)  # Monday (exclusive)

    def test_this_weekend_on_saturday_uses_today(self):
        now = datetime(2026, 4, 25, 9, 0, tzinfo=self.TZ)  # Saturday
        start, end = _resolve_date_range("this_weekend", now)
        assert start == datetime(2026, 4, 25, tzinfo=self.TZ)
        assert end == datetime(2026, 4, 27, tzinfo=self.TZ)

    def test_this_weekend_on_sunday_uses_yesterday(self):
        now = datetime(2026, 4, 26, 14, 0, tzinfo=self.TZ)  # Sunday
        start, end = _resolve_date_range("this_weekend", now)
        assert start == datetime(2026, 4, 25, tzinfo=self.TZ)
        assert end == datetime(2026, 4, 27, tzinfo=self.TZ)

    def test_this_weekend_on_monday(self):
        now = datetime(2026, 4, 27, 10, 0, tzinfo=self.TZ)  # Monday
        start, end = _resolve_date_range("this_weekend", now)
        assert start == datetime(2026, 5, 2, tzinfo=self.TZ)
        assert end == datetime(2026, 5, 4, tzinfo=self.TZ)

    def test_next_weekend_is_seven_days_after_this(self):
        now = datetime(2026, 4, 23, 11, 17, tzinfo=self.TZ)
        start, end = _resolve_date_range("next_weekend", now)
        assert start == datetime(2026, 5, 2, tzinfo=self.TZ)
        assert end == datetime(2026, 5, 4, tzinfo=self.TZ)

    def test_next_weekend_on_sunday(self):
        now = datetime(2026, 4, 26, 14, 0, tzinfo=self.TZ)  # Sunday
        start, end = _resolve_date_range("next_weekend", now)
        assert start == datetime(2026, 5, 2, tzinfo=self.TZ)
        assert end == datetime(2026, 5, 4, tzinfo=self.TZ)

    def test_this_week_covers_mon_to_next_mon(self):
        now = datetime(2026, 4, 23, 11, 17, tzinfo=self.TZ)  # Thursday
        start, end = _resolve_date_range("this_week", now)
        assert start == datetime(2026, 4, 20, tzinfo=self.TZ)  # Monday
        assert end == datetime(2026, 4, 27, tzinfo=self.TZ)  # next Monday

    def test_next_week(self):
        now = datetime(2026, 4, 23, 11, 17, tzinfo=self.TZ)  # Thursday
        start, end = _resolve_date_range("next_week", now)
        assert start == datetime(2026, 4, 27, tzinfo=self.TZ)
        assert end == datetime(2026, 5, 4, tzinfo=self.TZ)

    def test_unknown_period_raises(self):
        now = datetime(2026, 4, 23, tzinfo=self.TZ)
        with pytest.raises(ValueError):
            _resolve_date_range("yesterday", now)


class TestGetDateRangeTool:
    """Dispatch of get_date_range through _execute_tool."""

    @pytest.fixture
    def agent(self):
        return AIAgent()

    @pytest.mark.asyncio
    async def test_returns_iso_bounds_json(self, agent):
        with patch("app.services.ai_agent.auth_service") as auth:
            auth.get_user_timezone = AsyncMock(return_value="Europe/Belgrade")
            result = await agent._execute_tool(
                user_id=1,
                name="get_date_range",
                args={"period": "this_weekend"},
                image_holder=[],
                pending_holder=[],
            )
        import json as _json

        payload = _json.loads(result)
        assert "time_min" in payload
        assert "time_max" in payload
        # Parses back into tz-aware datetimes
        assert datetime.fromisoformat(payload["time_min"]).tzinfo is not None
        assert datetime.fromisoformat(payload["time_max"]).tzinfo is not None

    @pytest.mark.asyncio
    async def test_invalid_period_returns_error_string(self, agent):
        with patch("app.services.ai_agent.auth_service") as auth:
            auth.get_user_timezone = AsyncMock(return_value="Europe/Belgrade")
            result = await agent._execute_tool(
                user_id=1,
                name="get_date_range",
                args={"period": "bogus"},
                image_holder=[],
                pending_holder=[],
            )
        assert result.lower().startswith("error")


class TestRunCalendarTool:
    """Regression guards for the tz-aware/naive comparison bug."""

    @pytest.fixture
    def agent(self):
        return AIAgent()

    @pytest.mark.asyncio
    async def test_read_events_accepts_naive_time_min(self, agent):
        """Model may pass a naive ISO string; _fix_tz must attach user tz
        so the `time_min < now` comparison doesn't raise."""
        with (
            patch("app.services.ai_agent.auth_service") as auth,
            patch("app.services.ai_agent.calendar_service") as cal,
        ):
            auth.get_credentials = AsyncMock(return_value=object())
            auth.get_calendar_id = AsyncMock(return_value="primary")
            auth.get_user_timezone = AsyncMock(return_value="Europe/Belgrade")
            cal.list_events = AsyncMock(return_value=[])

            result = await agent._run_calendar_tool(
                user_id=1,
                name="read_events",
                args={"time_min": "2099-01-01T10:00:00"},  # naive, future
            )
        assert result == "No upcoming events found."
        # Verify time_min was passed through as tz-aware
        assert cal.list_events.await_args is not None
        kwargs = cal.list_events.await_args.kwargs
        assert kwargs["time_min"].tzinfo is not None

    @pytest.mark.asyncio
    async def test_read_events_drops_past_time_min_when_no_time_max(self, agent):
        """Past time_min without time_max should fall back to None (i.e. now)."""
        with (
            patch("app.services.ai_agent.auth_service") as auth,
            patch("app.services.ai_agent.calendar_service") as cal,
        ):
            auth.get_credentials = AsyncMock(return_value=object())
            auth.get_calendar_id = AsyncMock(return_value="primary")
            auth.get_user_timezone = AsyncMock(return_value="Europe/Belgrade")
            cal.list_events = AsyncMock(return_value=[])

            await agent._run_calendar_tool(
                user_id=1,
                name="read_events",
                args={"time_min": "2000-01-01T00:00:00"},  # past
            )
        assert cal.list_events.await_args is not None
        assert cal.list_events.await_args.kwargs["time_min"] is None

    @pytest.mark.asyncio
    async def test_read_events_keeps_past_time_min_when_time_max_given(self, agent):
        """A specific past date range (e.g. looking up yesterday) must be honored."""
        with (
            patch("app.services.ai_agent.auth_service") as auth,
            patch("app.services.ai_agent.calendar_service") as cal,
        ):
            auth.get_credentials = AsyncMock(return_value=object())
            auth.get_calendar_id = AsyncMock(return_value="primary")
            auth.get_user_timezone = AsyncMock(return_value="Europe/Belgrade")
            cal.list_events = AsyncMock(return_value=[])

            await agent._run_calendar_tool(
                user_id=1,
                name="read_events",
                args={
                    "time_min": "2000-01-01T00:00:00",
                    "time_max": "2000-01-02T00:00:00",
                },
            )
        assert cal.list_events.await_args is not None
        kwargs = cal.list_events.await_args.kwargs
        assert kwargs["time_min"] is not None
        assert kwargs["time_max"] is not None
