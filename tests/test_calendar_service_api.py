"""End-to-end coverage for CalendarService — Google API mocked at _make_service."""

from datetime import UTC, date, datetime
from unittest.mock import MagicMock, patch

import pytest
from googleapiclient.errors import HttpError

from app.services.calendar_service import (
    CalendarRead,
    CalendarService,
    EventCreate,
    EventUpdate,
    _parse_event,
    _refresh_credentials,
    _retry,
    calendar_service,
)


def _http_error(status: int, reason: str = "x") -> HttpError:
    """Build a googleapiclient HttpError with the given status."""
    resp = MagicMock(status=status, reason=reason)
    resp.status = status
    err = HttpError(resp=resp, content=b"")
    # The constructor populates status_code from resp.status; double-check:
    assert err.status_code == status
    return err


# ── _refresh_credentials ──────────────────────────────────────────────────────


def test_refresh_credentials_skips_when_fresh():
    creds = MagicMock()
    creds.refresh_token = "rt"
    creds.expired = False
    creds.expiry = datetime(2099, 1, 1)  # not expired and known
    out = _refresh_credentials(creds)
    creds.refresh.assert_not_called()
    assert out is creds


def test_refresh_credentials_refreshes_when_expired():
    creds = MagicMock()
    creds.refresh_token = "rt"
    creds.expired = True
    creds.expiry = datetime(2020, 1, 1)
    _refresh_credentials(creds)
    creds.refresh.assert_called_once()


def test_refresh_credentials_refreshes_when_expiry_unknown():
    creds = MagicMock()
    creds.refresh_token = "rt"
    creds.expired = False
    creds.expiry = None
    _refresh_credentials(creds)
    creds.refresh.assert_called_once()


def test_refresh_credentials_no_refresh_token_skips():
    creds = MagicMock()
    creds.refresh_token = None
    creds.expired = True
    creds.expiry = None
    _refresh_credentials(creds)
    creds.refresh.assert_not_called()


# ── _retry ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_retry_returns_value_on_first_success():
    def fn():
        return 42

    assert await _retry(fn) == 42


@pytest.mark.asyncio
async def test_retry_passes_through_non_retryable_http():
    def fn():
        raise _http_error(404, "Not Found")

    with pytest.raises(HttpError) as ei:
        await _retry(fn)
    assert ei.value.status_code == 404


@pytest.mark.asyncio
async def test_retry_succeeds_after_retryable_http():
    """First call raises 503, second succeeds."""
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] == 1:
            raise _http_error(503, "Service Unavailable")
        return "ok"

    with patch("app.services.calendar_service._RETRY_DELAY", 0.0):
        out = await _retry(fn)
    assert out == "ok"
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_retry_exhausts_retries_on_transient():
    def fn():
        raise ConnectionError("network down")

    with (
        patch("app.services.calendar_service._RETRY_DELAY", 0.0),
        pytest.raises(ConnectionError),
    ):
        await _retry(fn)


# ── _parse_event ──────────────────────────────────────────────────────────────


def test_parse_event_with_datetime():
    raw = {
        "id": "evt1",
        "summary": "Meeting",
        "start": {"dateTime": "2026-05-10T14:00:00+02:00"},
        "end": {"dateTime": "2026-05-10T15:00:00+02:00"},
        "location": "Office",
        "htmlLink": "https://calendar.google.com/evt1",
    }
    parsed = _parse_event(raw)
    assert parsed.event_id == "evt1"
    assert parsed.summary == "Meeting"
    assert parsed.location == "Office"
    assert parsed.html_link is not None
    assert parsed.html_link.endswith("evt1")
    assert parsed.start.hour == 14


def test_parse_event_all_day():
    raw = {
        "id": "evt2",
        "summary": "Holiday",
        "start": {"date": "2026-05-10"},
        "end": {"date": "2026-05-11"},
    }
    parsed = _parse_event(raw)
    assert parsed.start.date() == date(2026, 5, 10)
    assert parsed.end.date() == date(2026, 5, 11)


def test_parse_event_missing_summary_uses_placeholder():
    raw = {"id": "evt3", "start": {}, "end": {}}
    parsed = _parse_event(raw)
    assert parsed.summary == "(no title)"
    # missing dates → fall back to "now" (just sanity check tz)
    assert parsed.start.tzinfo is not None


# ── CalendarService methods (mock _make_service) ──────────────────────────────


@pytest.fixture
def service_factory():
    """Patch _make_service to return a MagicMock svc; return the mock factory
    plus the service mock so each test can wire the responses it expects."""
    svc = MagicMock()
    with patch("app.services.calendar_service._make_service", return_value=svc):
        yield svc


@pytest.mark.asyncio
async def test_get_user_timezone_happy_path(service_factory):
    service_factory.settings.return_value.get.return_value.execute.return_value = {
        "value": "Europe/Belgrade"
    }
    tz = await calendar_service.get_user_timezone(MagicMock())
    assert tz == "Europe/Belgrade"


@pytest.mark.asyncio
async def test_get_user_timezone_falls_back_to_utc_on_error(service_factory):
    service_factory.settings.return_value.get.return_value.execute.side_effect = ConnectionError(
        "no net"
    )
    with patch("app.services.calendar_service._RETRY_DELAY", 0.0):
        tz = await calendar_service.get_user_timezone(MagicMock())
    assert tz == "UTC"


@pytest.mark.asyncio
async def test_list_calendars_happy_path(service_factory):
    service_factory.calendarList.return_value.list.return_value.execute.return_value = {
        "items": [
            {"id": "primary", "summary": "Primary", "primary": True},
            {"id": "work@x", "summary": "Work"},
            {"id": "anon@x"},  # missing summary → defaults to id
        ]
    }
    cals = await calendar_service.list_calendars(MagicMock())
    assert len(cals) == 3
    assert cals[0] == CalendarRead(calendar_id="primary", name="Primary", primary=True)
    assert cals[2].name == "anon@x"  # fallback


@pytest.mark.asyncio
async def test_list_calendars_http_error_wraps_runtime(service_factory):
    service_factory.calendarList.return_value.list.return_value.execute.side_effect = _http_error(
        404, "Not Found"
    )
    with pytest.raises(RuntimeError, match="Google Calendar error: 404"):
        await calendar_service.list_calendars(MagicMock())


@pytest.mark.asyncio
async def test_list_calendars_transient_error_wraps_network(service_factory):
    service_factory.calendarList.return_value.list.return_value.execute.side_effect = TimeoutError(
        "slow"
    )
    with (
        patch("app.services.calendar_service._RETRY_DELAY", 0.0),
        pytest.raises(RuntimeError, match="Network error"),
    ):
        await calendar_service.list_calendars(MagicMock())


@pytest.mark.asyncio
async def test_list_events_happy_path(service_factory):
    service_factory.events.return_value.list.return_value.execute.return_value = {
        "items": [
            {
                "id": "e1",
                "summary": "X",
                "start": {"dateTime": "2026-05-10T10:00:00+00:00"},
                "end": {"dateTime": "2026-05-10T11:00:00+00:00"},
            }
        ]
    }
    events = await calendar_service.list_events(
        MagicMock(),
        calendar_id="primary",
        max_results=5,
        time_min=datetime(2026, 5, 1, tzinfo=UTC),
        time_max=datetime(2026, 5, 31, tzinfo=UTC),
        query="meeting",
    )
    assert len(events) == 1
    # Verify kwargs passed to the API
    list_kwargs = service_factory.events.return_value.list.call_args.kwargs
    assert list_kwargs["calendarId"] == "primary"
    assert list_kwargs["q"] == "meeting"
    assert "timeMax" in list_kwargs


@pytest.mark.asyncio
async def test_list_events_uses_now_when_time_min_missing(service_factory):
    service_factory.events.return_value.list.return_value.execute.return_value = {"items": []}
    await calendar_service.list_events(MagicMock())
    list_kwargs = service_factory.events.return_value.list.call_args.kwargs
    assert "timeMin" in list_kwargs  # always set
    assert "timeMax" not in list_kwargs  # not provided
    assert "q" not in list_kwargs


@pytest.mark.asyncio
async def test_list_events_http_error_wraps_runtime(service_factory):
    service_factory.events.return_value.list.return_value.execute.side_effect = _http_error(
        404, "x"
    )
    with pytest.raises(RuntimeError, match="404"):
        await calendar_service.list_events(MagicMock())


@pytest.mark.asyncio
async def test_list_events_transient_error_wraps_network(service_factory):
    service_factory.events.return_value.list.return_value.execute.side_effect = OSError("net")
    with (
        patch("app.services.calendar_service._RETRY_DELAY", 0.0),
        pytest.raises(RuntimeError, match="Network error"),
    ):
        await calendar_service.list_events(MagicMock())


@pytest.mark.asyncio
async def test_create_event_timed(service_factory):
    service_factory.events.return_value.insert.return_value.execute.return_value = {
        "id": "new",
        "summary": "Meeting",
        "start": {"dateTime": "2026-05-10T14:00:00+02:00"},
        "end": {"dateTime": "2026-05-10T15:00:00+02:00"},
    }
    ev = EventCreate(
        summary="Meeting",
        start=datetime(2026, 5, 10, 14, tzinfo=UTC),
        end=datetime(2026, 5, 10, 15, tzinfo=UTC),
        description="notes",
        location="Room 1",
    )
    out = await calendar_service.create_event(MagicMock(), ev)
    assert out.event_id == "new"
    body = service_factory.events.return_value.insert.call_args.kwargs["body"]
    assert "dateTime" in body["start"]
    assert body["description"] == "notes"
    assert body["location"] == "Room 1"


@pytest.mark.asyncio
async def test_create_event_all_day(service_factory):
    service_factory.events.return_value.insert.return_value.execute.return_value = {
        "id": "new",
        "summary": "Holiday",
        "start": {"date": "2026-05-10"},
        "end": {"date": "2026-05-11"},
    }
    ev = EventCreate(
        summary="Holiday",
        start_date=date(2026, 5, 10),
        end_date=date(2026, 5, 11),
    )
    await calendar_service.create_event(MagicMock(), ev)
    body = service_factory.events.return_value.insert.call_args.kwargs["body"]
    assert "date" in body["start"]
    assert "description" not in body
    assert "location" not in body


@pytest.mark.asyncio
async def test_create_event_http_error(service_factory):
    service_factory.events.return_value.insert.return_value.execute.side_effect = _http_error(
        409, "conflict"
    )
    ev = EventCreate(
        summary="X",
        start=datetime(2026, 5, 10, tzinfo=UTC),
        end=datetime(2026, 5, 10, 1, tzinfo=UTC),
    )
    with pytest.raises(RuntimeError, match="409"):
        await calendar_service.create_event(MagicMock(), ev)


@pytest.mark.asyncio
async def test_create_event_transient_error(service_factory):
    service_factory.events.return_value.insert.return_value.execute.side_effect = ConnectionError(
        "x"
    )
    ev = EventCreate(
        summary="X",
        start=datetime(2026, 5, 10, tzinfo=UTC),
        end=datetime(2026, 5, 10, 1, tzinfo=UTC),
    )
    with (
        patch("app.services.calendar_service._RETRY_DELAY", 0.0),
        pytest.raises(RuntimeError, match="Network error"),
    ):
        await calendar_service.create_event(MagicMock(), ev)


@pytest.mark.asyncio
async def test_update_event_each_field(service_factory):
    """Exercise every branch in update_event's body merging."""
    existing = {
        "id": "e1",
        "summary": "old",
        "start": {"dateTime": "2026-05-10T10:00:00+00:00"},
        "end": {"dateTime": "2026-05-10T11:00:00+00:00"},
    }
    service_factory.events.return_value.get.return_value.execute.return_value = dict(existing)
    service_factory.events.return_value.update.return_value.execute.return_value = {
        "id": "e1",
        "summary": "new title",
        "start": {"date": "2026-05-15"},
        "end": {"date": "2026-05-16"},
        "description": "desc",
        "location": "Loc",
    }

    upd = EventUpdate(
        event_id="e1",
        summary="new title",
        start_date=date(2026, 5, 15),
        end_date=date(2026, 5, 16),
        description="desc",
        location="Loc",
    )
    out = await calendar_service.update_event(MagicMock(), upd)
    assert out.event_id == "e1"
    body = service_factory.events.return_value.update.call_args.kwargs["body"]
    assert body["summary"] == "new title"
    assert body["start"] == {"date": "2026-05-15"}
    assert body["description"] == "desc"


@pytest.mark.asyncio
async def test_update_event_dt_branch(service_factory):
    """When start/end are datetimes (not dates), the dateTime branch fires."""
    service_factory.events.return_value.get.return_value.execute.return_value = {
        "id": "e2",
        "summary": "old",
        "start": {},
        "end": {},
    }
    service_factory.events.return_value.update.return_value.execute.return_value = {
        "id": "e2",
        "summary": "old",
        "start": {"dateTime": "2026-06-01T09:00:00+00:00"},
        "end": {"dateTime": "2026-06-01T10:00:00+00:00"},
    }
    upd = EventUpdate(
        event_id="e2",
        start=datetime(2026, 6, 1, 9, tzinfo=UTC),
        end=datetime(2026, 6, 1, 10, tzinfo=UTC),
    )
    await calendar_service.update_event(MagicMock(), upd)
    body = service_factory.events.return_value.update.call_args.kwargs["body"]
    assert "dateTime" in body["start"]


@pytest.mark.asyncio
async def test_update_event_http_error(service_factory):
    service_factory.events.return_value.get.return_value.execute.side_effect = _http_error(
        404, "Not Found"
    )
    with pytest.raises(RuntimeError, match="404"):
        await calendar_service.update_event(
            MagicMock(), EventUpdate(event_id="missing", summary="x")
        )


@pytest.mark.asyncio
async def test_update_event_transient(service_factory):
    service_factory.events.return_value.get.return_value.execute.side_effect = ConnectionError("x")
    with (
        patch("app.services.calendar_service._RETRY_DELAY", 0.0),
        pytest.raises(RuntimeError, match="Network error"),
    ):
        await calendar_service.update_event(MagicMock(), EventUpdate(event_id="e", summary="y"))


@pytest.mark.asyncio
async def test_delete_event_happy_path(service_factory):
    service_factory.events.return_value.delete.return_value.execute.return_value = None
    await calendar_service.delete_event(MagicMock(), "e1")
    service_factory.events.return_value.delete.assert_called_once_with(
        calendarId="primary", eventId="e1"
    )


@pytest.mark.asyncio
async def test_delete_event_http_error(service_factory):
    service_factory.events.return_value.delete.return_value.execute.side_effect = _http_error(
        404, "Not Found"
    )
    with pytest.raises(RuntimeError, match="404"):
        await calendar_service.delete_event(MagicMock(), "missing")


@pytest.mark.asyncio
async def test_delete_event_transient(service_factory):
    service_factory.events.return_value.delete.return_value.execute.side_effect = ConnectionError(
        "x"
    )
    with (
        patch("app.services.calendar_service._RETRY_DELAY", 0.0),
        pytest.raises(RuntimeError, match="Network error"),
    ):
        await calendar_service.delete_event(MagicMock(), "e1")


def test_calendar_service_singleton():
    assert isinstance(calendar_service, CalendarService)
