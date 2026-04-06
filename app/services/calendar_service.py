"""Google Calendar API service.

All Google API calls are synchronous (google-api-python-client uses httplib2).
Each public method wraps its work in :func:`asyncio.to_thread` so callers
remain fully async.  A fresh ``build()`` resource is created per call to
avoid httplib2 thread-safety issues.
"""

import asyncio
import logging
from datetime import UTC, datetime

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from pydantic import BaseModel, field_validator

logger = logging.getLogger(__name__)

# ── Pydantic schemas ──────────────────────────────────────────────────────────


class EventCreate(BaseModel):
    summary: str
    start: datetime
    end: datetime
    description: str | None = None
    location: str | None = None

    @field_validator("end")
    @classmethod
    def end_after_start(cls, v: datetime, info) -> datetime:
        start = info.data.get("start")
        if start and v <= start:
            raise ValueError("end must be after start")
        return v


class EventUpdate(BaseModel):
    event_id: str
    summary: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    description: str | None = None
    location: str | None = None


class EventRead(BaseModel):
    event_id: str
    summary: str
    start: datetime
    end: datetime
    description: str | None = None
    location: str | None = None
    html_link: str | None = None


# ── Helpers ───────────────────────────────────────────────────────────────────


def _refresh_credentials(credentials: Credentials) -> Credentials:
    """Refresh token if expired.  Must run inside a thread (synchronous I/O)."""
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
    return credentials


def _make_service(credentials: Credentials):
    """Return a fresh Calendar v3 resource.  Always call from a thread."""
    credentials = _refresh_credentials(credentials)
    return build("calendar", "v3", credentials=credentials, cache_discovery=False)


def _parse_dt(raw: str) -> datetime:
    """Parse a Google API datetime string to an aware :class:`datetime`."""
    if "T" in raw:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    # All-day event: date only
    return datetime.fromisoformat(raw).replace(tzinfo=UTC)


def _parse_event(raw: dict) -> EventRead:
    start_raw = raw.get("start", {})
    end_raw = raw.get("end", {})
    start_str = start_raw.get("dateTime") or start_raw.get("date", "")
    end_str = end_raw.get("dateTime") or end_raw.get("date", "")
    return EventRead(
        event_id=raw["id"],
        summary=raw.get("summary", "(no title)"),
        start=_parse_dt(start_str) if start_str else datetime.now(tz=UTC),
        end=_parse_dt(end_str) if end_str else datetime.now(tz=UTC),
        description=raw.get("description"),
        location=raw.get("location"),
        html_link=raw.get("htmlLink"),
    )


# ── Service ───────────────────────────────────────────────────────────────────


class CalendarService:
    async def list_events(
        self,
        credentials: Credentials,
        max_results: int = 10,
        time_min: datetime | None = None,
        time_max: datetime | None = None,
        query: str | None = None,
    ) -> list[EventRead]:
        now_dt = time_min or datetime.now(tz=UTC)
        time_min_str = now_dt.isoformat()

        def _call() -> list[dict]:
            svc = _make_service(credentials)
            kwargs: dict = {
                "calendarId": "primary",
                "maxResults": max_results,
                "timeMin": time_min_str,
                "singleEvents": True,
                "orderBy": "startTime",
            }
            if time_max:
                kwargs["timeMax"] = time_max.isoformat()
            if query:
                kwargs["q"] = query
            return svc.events().list(**kwargs).execute().get("items", [])

        try:
            items: list[dict] = await asyncio.to_thread(_call)
        except HttpError as exc:
            raise RuntimeError(f"Google Calendar error: {exc.status_code} {exc.reason}") from exc

        return [_parse_event(item) for item in items]

    async def create_event(self, credentials: Credentials, event: EventCreate) -> EventRead:
        def _call() -> dict:
            svc = _make_service(credentials)
            body: dict = {
                "summary": event.summary,
                "start": {"dateTime": event.start.isoformat()},
                "end": {"dateTime": event.end.isoformat()},
            }
            if event.description:
                body["description"] = event.description
            if event.location:
                body["location"] = event.location
            return svc.events().insert(calendarId="primary", body=body).execute()

        try:
            raw: dict = await asyncio.to_thread(_call)
        except HttpError as exc:
            raise RuntimeError(f"Google Calendar error: {exc.status_code} {exc.reason}") from exc

        return _parse_event(raw)

    async def update_event(self, credentials: Credentials, event: EventUpdate) -> EventRead:
        def _call() -> dict:
            svc = _make_service(credentials)
            existing: dict = (
                svc.events().get(calendarId="primary", eventId=event.event_id).execute()
            )
            if event.summary is not None:
                existing["summary"] = event.summary
            if event.start is not None:
                existing["start"] = {"dateTime": event.start.isoformat()}
            if event.end is not None:
                existing["end"] = {"dateTime": event.end.isoformat()}
            if event.description is not None:
                existing["description"] = event.description
            if event.location is not None:
                existing["location"] = event.location
            return (
                svc.events()
                .update(calendarId="primary", eventId=event.event_id, body=existing)
                .execute()
            )

        try:
            raw: dict = await asyncio.to_thread(_call)
        except HttpError as exc:
            raise RuntimeError(f"Google Calendar error: {exc.status_code} {exc.reason}") from exc

        return _parse_event(raw)

    async def delete_event(self, credentials: Credentials, event_id: str) -> None:
        def _call() -> None:
            svc = _make_service(credentials)
            svc.events().delete(calendarId="primary", eventId=event_id).execute()

        try:
            await asyncio.to_thread(_call)
        except HttpError as exc:
            raise RuntimeError(f"Google Calendar error: {exc.status_code} {exc.reason}") from exc


calendar_service = CalendarService()
