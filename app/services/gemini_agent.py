"""Gemini LLM agent with Google Calendar function calling.

Uses the current `google-genai` SDK (google.genai).

Design notes:
- Multi-turn conversation; function calls are executed locally and results
  are fed back to the model.
- The agent never assumes event IDs — it always calls ``read_events`` first
  before ``update_event`` or ``delete_event``.
- A hard cap of _MAX_TOOL_ROUNDS prevents runaway API calls.
"""

import logging
from datetime import UTC, datetime

from google import genai
from google.genai import types

from app.core.config import settings
from app.services.auth_service import auth_service
from app.services.calendar_service import (
    EventCreate,
    EventUpdate,
    calendar_service,
)

logger = logging.getLogger(__name__)

_MAX_TOOL_ROUNDS = 5

_SYSTEM_PROMPT = (
    "You are a helpful calendar assistant. Today is {now}.\n"
    "You manage the user's Google Calendar through the provided tools.\n"
    "Rules:\n"
    "- You do NOT know event IDs. Always call read_events first before "
    "update_event or delete_event.\n"
    "- When creating events, always ask for both start and end times if not given.\n"
    "- Respond in the same language the user uses.\n"
    "- Be concise."
)


def _tool_config() -> list[types.Tool]:
    schema = types.Schema
    typ = types.Type

    return [
        types.Tool(
            function_declarations=[
                types.FunctionDeclaration(
                    name="read_events",
                    description=(
                        "List upcoming calendar events. "
                        "Call this first whenever you need an event_id."
                    ),
                    parameters=schema(
                        type=typ.OBJECT,
                        properties={
                            "max_results": schema(
                                type=typ.INTEGER,
                                description="Maximum events to return (default 10).",
                            ),
                            "query": schema(
                                type=typ.STRING,
                                description="Optional free-text search query.",
                            ),
                        },
                    ),
                ),
                types.FunctionDeclaration(
                    name="create_event",
                    description="Create a new calendar event.",
                    parameters=schema(
                        type=typ.OBJECT,
                        properties={
                            "summary": schema(type=typ.STRING, description="Event title."),
                            "start_time": schema(
                                type=typ.STRING,
                                description="Start datetime ISO 8601 (e.g. 2025-06-01T14:00:00+03:00).",  # noqa: E501
                            ),
                            "end_time": schema(
                                type=typ.STRING,
                                description="End datetime ISO 8601.",
                            ),
                            "description": schema(
                                type=typ.STRING, description="Optional description."
                            ),
                            "location": schema(type=typ.STRING, description="Optional location."),
                        },
                        required=["summary", "start_time", "end_time"],
                    ),
                ),
                types.FunctionDeclaration(
                    name="update_event",
                    description=(
                        "Update an existing calendar event. "
                        "Requires event_id — obtain it from read_events first."
                    ),
                    parameters=schema(
                        type=typ.OBJECT,
                        properties={
                            "event_id": schema(
                                type=typ.STRING, description="Google Calendar event ID."
                            ),
                            "summary": schema(type=typ.STRING, description="New title."),
                            "start_time": schema(
                                type=typ.STRING, description="New start ISO 8601."
                            ),
                            "end_time": schema(type=typ.STRING, description="New end ISO 8601."),
                            "description": schema(type=typ.STRING, description="New description."),
                            "location": schema(type=typ.STRING, description="New location."),
                        },
                        required=["event_id"],
                    ),
                ),
                types.FunctionDeclaration(
                    name="delete_event",
                    description=(
                        "Delete a calendar event. "
                        "Requires event_id — obtain it from read_events first."
                    ),
                    parameters=schema(
                        type=typ.OBJECT,
                        properties={
                            "event_id": schema(
                                type=typ.STRING, description="Google Calendar event ID."
                            ),
                        },
                        required=["event_id"],
                    ),
                ),
            ]
        )
    ]


class GeminiAgent:
    def __init__(self) -> None:
        self._client: genai.Client | None = None

    def _get_client(self) -> genai.Client:
        if self._client is None:
            if not settings.gemini_api_key:
                raise RuntimeError("GEMINI_API_KEY is not configured")
            self._client = genai.Client(api_key=settings.gemini_api_key)
        return self._client

    async def _execute_tool(self, user_id: int, name: str, args: dict) -> str:
        credentials = await auth_service.get_credentials(user_id)
        if credentials is None:
            return "Error: user is not authenticated with Google. Ask them to run /auth."

        try:
            if name == "read_events":
                events = await calendar_service.list_events(
                    credentials,
                    max_results=int(args.get("max_results", 10)),
                    query=args.get("query"),
                )
                if not events:
                    return "No upcoming events found."
                lines = [
                    f"ID:{e.event_id} | {e.summary} | "
                    f"{e.start.strftime('%Y-%m-%d %H:%M')} – {e.end.strftime('%H:%M')}"
                    + (f" | 📍{e.location}" if e.location else "")
                    for e in events
                ]
                return "\n".join(lines)

            if name == "create_event":
                ev = EventCreate(
                    summary=args["summary"],
                    start=datetime.fromisoformat(args["start_time"]),
                    end=datetime.fromisoformat(args["end_time"]),
                    description=args.get("description"),
                    location=args.get("location"),
                )
                created = await calendar_service.create_event(credentials, ev)
                return f"Created: {created.summary} (ID:{created.event_id})"

            if name == "update_event":
                up = EventUpdate(
                    event_id=args["event_id"],
                    summary=args.get("summary"),
                    start=(
                        datetime.fromisoformat(args["start_time"])
                        if args.get("start_time")
                        else None
                    ),
                    end=(
                        datetime.fromisoformat(args["end_time"]) if args.get("end_time") else None
                    ),
                    description=args.get("description"),
                    location=args.get("location"),
                )
                updated = await calendar_service.update_event(credentials, up)
                return f"Updated: {updated.summary}"

            if name == "delete_event":
                await calendar_service.delete_event(credentials, args["event_id"])
                return f"Deleted event {args['event_id']}."

        except (RuntimeError, ValueError) as exc:
            logger.error("Tool %s failed: %s", name, exc)
            return f"Error in {name}: {exc}"

        return f"Unknown tool: {name}"

    async def process_message(self, user_id: int, message: str) -> str:
        """Run a free-text message through Gemini and return the final reply."""
        try:
            client = self._get_client()
        except RuntimeError as exc:
            return str(exc)

        system_text = _SYSTEM_PROMPT.format(now=datetime.now(tz=UTC).isoformat())
        gen_config = types.GenerateContentConfig(
            system_instruction=system_text,
            tools=_tool_config(),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )

        chat = client.aio.chats.create(model=settings.gemini_model, config=gen_config)

        try:
            response = await chat.send_message(message)
        except Exception as exc:
            logger.error("Gemini send_message error: %s", exc)
            return "Sorry, I couldn't reach the AI service right now."

        for _ in range(_MAX_TOOL_ROUNDS):
            fcs = response.function_calls
            if not fcs:
                break

            result_parts: list[types.Part] = []
            for fc in fcs:
                if fc.name is None:
                    continue
                tool_result = await self._execute_tool(user_id, fc.name, dict(fc.args or {}))
                result_parts.append(
                    types.Part(
                        function_response=types.FunctionResponse(
                            name=fc.name,
                            response={"result": tool_result},
                        )
                    )
                )

            try:
                response = await chat.send_message(result_parts)
            except Exception as exc:
                logger.error("Gemini tool-result error: %s", exc)
                return "Sorry, something went wrong while processing the response."

        try:
            return response.text or "I couldn't generate a response."
        except (ValueError, AttributeError):
            return "I couldn't generate a response."


gemini_agent = GeminiAgent()
