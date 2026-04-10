"""Tests for calendar_service helpers and models."""

from datetime import UTC, date, datetime

from app.services.calendar_service import EventCreate, EventRead, EventUpdate, _parse_dt


class TestParseDt:
    def test_datetime_with_offset(self):
        result = _parse_dt("2025-06-01T14:00:00+03:00")
        assert result.hour == 14
        assert result.tzinfo is not None

    def test_datetime_with_z(self):
        result = _parse_dt("2025-06-01T14:00:00Z")
        assert result.hour == 14
        assert result.tzinfo is not None

    def test_date_only(self):
        result = _parse_dt("2025-06-01")
        assert result.year == 2025
        assert result.month == 6
        assert result.day == 1
        assert result.tzinfo == UTC


class TestEventCreate:
    def test_all_day_true(self):
        ev = EventCreate(
            summary="Holiday",
            start_date=date(2025, 12, 25),
            end_date=date(2025, 12, 26),
        )
        assert ev.all_day is True

    def test_all_day_false(self):
        ev = EventCreate(
            summary="Meeting",
            start=datetime(2025, 6, 1, 14, 0, tzinfo=UTC),
            end=datetime(2025, 6, 1, 15, 0, tzinfo=UTC),
        )
        assert ev.all_day is False

    def test_summary_required(self):
        ev = EventCreate(summary="Test")
        assert ev.summary == "Test"
        assert ev.start is None
        assert ev.end is None
        assert ev.start_date is None


class TestEventUpdate:
    def test_partial_update(self):
        up = EventUpdate(event_id="abc", summary="New Title")
        assert up.event_id == "abc"
        assert up.summary == "New Title"
        assert up.start is None
        assert up.location is None


class TestEventRead:
    def test_full_event(self):
        ev = EventRead(
            event_id="e1",
            summary="Test",
            start=datetime(2025, 6, 1, 14, 0, tzinfo=UTC),
            end=datetime(2025, 6, 1, 15, 0, tzinfo=UTC),
            description="A test event",
            location="Office",
            html_link="https://calendar.google.com/event/e1",
        )
        assert ev.event_id == "e1"
        assert ev.location == "Office"

    def test_minimal_event(self):
        ev = EventRead(
            event_id="e2",
            summary="Minimal",
            start=datetime(2025, 6, 1, tzinfo=UTC),
            end=datetime(2025, 6, 1, tzinfo=UTC),
        )
        assert ev.description is None
        assert ev.html_link is None
