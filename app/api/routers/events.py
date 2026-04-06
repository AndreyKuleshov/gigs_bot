"""Calendar event CRUD endpoints."""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from google.oauth2.credentials import Credentials

from app.api.deps import get_current_credentials
from app.services.calendar_service import (
    EventCreate,
    EventRead,
    EventUpdate,
    calendar_service,
)

router = APIRouter(prefix="/events", tags=["events"])

CurrentCreds = Annotated[Credentials, Depends(get_current_credentials)]


@router.get("/", response_model=list[EventRead])
async def list_events(
    credentials: CurrentCreds,
    max_results: int = 10,
    time_min: datetime | None = None,
    time_max: datetime | None = None,
    query: str | None = None,
) -> list[EventRead]:
    try:
        return await calendar_service.list_events(
            credentials,
            max_results=max_results,
            time_min=time_min,
            time_max=time_max,
            query=query,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/", response_model=EventRead, status_code=201)
async def create_event(
    event: EventCreate,
    credentials: CurrentCreds,
) -> EventRead:
    try:
        return await calendar_service.create_event(credentials, event)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.put("/{event_id}", response_model=EventRead)
async def update_event(
    event_id: str,
    event: EventUpdate,
    credentials: CurrentCreds,
) -> EventRead:
    # Ensure path param takes precedence over any body event_id
    patched = event.model_copy(update={"event_id": event_id})
    try:
        return await calendar_service.update_event(credentials, patched)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.delete("/{event_id}", status_code=204)
async def delete_event(
    event_id: str,
    credentials: CurrentCreds,
) -> None:
    try:
        await calendar_service.delete_event(credentials, event_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
