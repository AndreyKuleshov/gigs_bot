"""Tests for AI agent logic (no API calls)."""

from datetime import datetime
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.services.ai_agent import AgentResponse, AIAgent, PendingAction


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
