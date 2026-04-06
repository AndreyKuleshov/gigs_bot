"""OpenAI LLM agent with Google Calendar function calling.

Design notes:
- Multi-turn conversation; function calls are executed locally and results
  are fed back to the model.
- The agent never assumes event IDs — it always calls ``read_events`` first
  before ``update_event`` or ``delete_event``.
- A hard cap of _MAX_TOOL_ROUNDS prevents runaway API calls.
"""

import json
import logging
from datetime import UTC, datetime

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam, ChatCompletionMessageToolCall

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

_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "read_events",
            "description": (
                "List upcoming calendar events. Call this first whenever you need an event_id."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum events to return (default 10).",
                    },
                    "query": {
                        "type": "string",
                        "description": "Optional free-text search query.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_event",
            "description": "Create a new calendar event.",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "Event title."},
                    "start_time": {
                        "type": "string",
                        "description": "Start datetime ISO 8601 (e.g. 2025-06-01T14:00:00+03:00).",
                    },
                    "end_time": {
                        "type": "string",
                        "description": "End datetime ISO 8601.",
                    },
                    "description": {
                        "type": "string",
                        "description": "Optional description.",
                    },
                    "location": {"type": "string", "description": "Optional location."},
                },
                "required": ["summary", "start_time", "end_time"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_event",
            "description": (
                "Update an existing calendar event. "
                "Requires event_id — obtain it from read_events first."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id": {
                        "type": "string",
                        "description": "Google Calendar event ID.",
                    },
                    "summary": {"type": "string", "description": "New title."},
                    "start_time": {"type": "string", "description": "New start ISO 8601."},
                    "end_time": {"type": "string", "description": "New end ISO 8601."},
                    "description": {"type": "string", "description": "New description."},
                    "location": {"type": "string", "description": "New location."},
                },
                "required": ["event_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_event",
            "description": (
                "Delete a calendar event. Requires event_id — obtain it from read_events first."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id": {
                        "type": "string",
                        "description": "Google Calendar event ID.",
                    },
                },
                "required": ["event_id"],
            },
        },
    },
]


class AIAgent:
    def __init__(self) -> None:
        self._client: AsyncOpenAI | None = None

    def _get_client(self) -> AsyncOpenAI:
        if self._client is None:
            if not settings.openai_api_key:
                raise RuntimeError("OPENAI_API_KEY is not configured")
            self._client = AsyncOpenAI(api_key=settings.openai_api_key)
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
        """Run a free-text message through the AI model and return the final reply."""
        try:
            client = self._get_client()
        except RuntimeError as exc:
            return str(exc)

        system_text = _SYSTEM_PROMPT.format(now=datetime.now(tz=UTC).isoformat())
        messages: list[ChatCompletionMessageParam] = [
            {"role": "system", "content": system_text},
            {"role": "user", "content": message},
        ]

        for _ in range(_MAX_TOOL_ROUNDS):
            try:
                response = await client.chat.completions.create(
                    model=settings.openai_model,
                    messages=messages,
                    tools=_TOOLS,  # type: ignore[arg-type]
                    tool_choice="auto",
                )
            except Exception as exc:
                logger.error("OpenAI chat.completions error: %s", exc)
                return "Sorry, I couldn't reach the AI service right now."

            choice = response.choices[0]
            messages.append(choice.message.model_dump(exclude_unset=True))  # type: ignore[arg-type]

            if choice.finish_reason != "tool_calls" or not choice.message.tool_calls:
                break

            for tc in choice.message.tool_calls:
                if not isinstance(tc, ChatCompletionMessageToolCall):
                    continue
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    args = {}
                tool_result = await self._execute_tool(user_id, tc.function.name, args)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": tool_result,
                    }
                )

        last = response.choices[0].message.content  # type: ignore[possibly-undefined]
        return last or "I couldn't generate a response."


ai_agent = AIAgent()
