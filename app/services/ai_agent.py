"""OpenAI LLM agent with Google Calendar function calling and web search.

Design notes:
- Multi-turn conversation; function calls are executed locally and results
  are fed back to the model.
- The agent never assumes event IDs — it always calls ``read_events`` first
  before ``update_event`` or ``delete_event``.
- A hard cap of _MAX_TOOL_ROUNDS prevents runaway API calls.
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

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

_MAX_TOOL_ROUNDS = 8


@dataclass
class PendingAction:
    tool_name: str
    args: dict


@dataclass
class AgentResponse:
    text: str
    image_url: str | None = None
    pending_action: PendingAction | None = None


_MUTATING_TOOLS = {"create_event", "update_event", "delete_event"}


_SYSTEM_PROMPT = (
    "You are a calendar assistant. Today is {now}.\n"
    "The user's timezone is {timezone}. Always use this timezone for dates and times.\n"
    "You ONLY manage the user's Google Calendar through the provided tools.\n"
    "You must REFUSE any questions or requests not related to calendar events "
    "(e.g. general knowledge, chitchat, jokes). Politely reply that you can only "
    "help with calendar management.\n"
    "Rules:\n"
    "- You do NOT know event IDs. Always call read_events first before "
    "update_event or delete_event.\n"
    "- When creating events, always ask for both start and end times if not given.\n"
    "- Respond in the same language the user uses.\n"
    "- Be concise.\n"
    "- Format responses using Telegram HTML: <b>bold</b>, <i>italic</i>, "
    "<code>code</code>. Use <b> for event titles and dates. Use bullet lists with •.\n"
    "- When the user asks WHEN something is (e.g. 'когда skillet?', 'when is the concert?'), "
    "ALWAYS call read_events first to check their calendar before searching the web.\n"
    "- Use web_search ONLY to look up additional info about events already in the calendar "
    "(e.g. venue details, artist info).\n"
    "- Use find_event_image only when the user explicitly asks for a photo/image, "
    "or when it clearly adds value. Do not call it speculatively."
)

_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "read_events",
            "description": (
                "List calendar events. Call this first whenever you need an event_id. "
                "Use time_min/time_max to target a specific date or range."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum events to return (default 25).",
                    },
                    "query": {
                        "type": "string",
                        "description": "Optional free-text search query.",
                    },
                    "time_min": {
                        "type": "string",
                        "description": (
                            "Lower bound (inclusive) for event start time, ISO 8601. "
                            "Use to look up events on or after a specific date, "
                            "e.g. '2026-06-13T00:00:00+00:00'."
                        ),
                    },
                    "time_max": {
                        "type": "string",
                        "description": (
                            "Upper bound (exclusive) for event start time, ISO 8601. "
                            "Use together with time_min to scope a single day, "
                            "e.g. '2026-06-14T00:00:00+00:00'."
                        ),
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_event",
            "description": (
                "Create a new calendar event. "
                "For all-day or multi-day events use start_date/end_date (YYYY-MM-DD). "
                "For timed events use start_time/end_time (ISO 8601). "
                "Do not mix date and datetime fields."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "Event title."},
                    "start_time": {
                        "type": "string",
                        "description": (
                            "Start datetime ISO 8601 "
                            "(e.g. 2025-06-01T14:00:00+03:00). For timed events."
                        ),
                    },
                    "end_time": {
                        "type": "string",
                        "description": "End datetime ISO 8601. For timed events.",
                    },
                    "start_date": {
                        "type": "string",
                        "description": "Start date YYYY-MM-DD. For all-day/multi-day events.",
                    },
                    "end_date": {
                        "type": "string",
                        "description": (
                            "End date YYYY-MM-DD (exclusive — day AFTER the last day). "
                            "For all-day/multi-day events."
                        ),
                    },
                    "description": {
                        "type": "string",
                        "description": "Optional description.",
                    },
                    "location": {"type": "string", "description": "Optional location."},
                },
                "required": ["summary"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_event",
            "description": (
                "Update an existing calendar event. "
                "Requires event_id — obtain it from read_events first. "
                "Use start_date/end_date for all-day events, start_time/end_time for timed."
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
                    "start_date": {
                        "type": "string",
                        "description": "New start date YYYY-MM-DD (all-day).",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "New end date YYYY-MM-DD exclusive (all-day).",
                    },
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
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the web for current information about events, venues, artists, "
                "or any topic. Use when the user asks about something you don't know."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query."},
                    "max_results": {
                        "type": "integer",
                        "description": "Number of results to return (default 5, max 10).",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_event_image",
            "description": (
                "Find a photo image for an event, artist, or venue. "
                "Returns an image URL that will be displayed to the user. "
                "Use only when a photo clearly adds value."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Descriptive image search query, e.g. 'Skillet band concert'.",  # noqa: E501
                    },
                },
                "required": ["query"],
            },
        },
    },
]


def _ddgs_text_sync(query: str, max_results: int) -> list[dict]:
    from duckduckgo_search import DDGS

    with DDGS() as ddgs:
        return list(ddgs.text(query, max_results=max_results))


def _ddgs_images_sync(query: str) -> list[dict]:
    from duckduckgo_search import DDGS

    with DDGS() as ddgs:
        return list(ddgs.images(query, type_image="photo", size="Large", max_results=5))


async def _web_search(query: str, max_results: int = 5) -> str:
    try:
        results = await asyncio.to_thread(_ddgs_text_sync, query, max_results)
    except Exception as exc:
        logger.warning("web_search failed: %s", exc)
        return "Search temporarily unavailable."
    if not results:
        return "No results found."
    lines = [f"{r['title']}\n{r['href']}\n{r['body']}" for r in results]
    return "\n\n".join(lines)


async def _find_event_image(query: str) -> str | None:
    try:
        results = await asyncio.to_thread(_ddgs_images_sync, query)
    except Exception as exc:
        logger.warning("find_event_image failed: %s", exc)
        return None
    return results[0]["image"] if results else None


class AIAgent:
    def __init__(self) -> None:
        self._client: AsyncOpenAI | None = None

    def _get_client(self) -> AsyncOpenAI:
        if self._client is None:
            if not settings.openai_api_key:
                raise RuntimeError("OPENAI_API_KEY is not configured")
            self._client = AsyncOpenAI(api_key=settings.openai_api_key)
        return self._client

    async def _execute_tool(
        self,
        user_id: int,
        name: str,
        args: dict,
        image_holder: list[str],
        pending_holder: list[PendingAction],
    ) -> str:
        if name == "web_search":
            return await _web_search(
                query=args.get("query", ""),
                max_results=min(int(args.get("max_results", 5)), 10),
            )

        if name == "find_event_image":
            url = await _find_event_image(args.get("query", ""))
            if url:
                image_holder.append(url)
                return "Image found and will be displayed to the user."
            return "No suitable image found."

        if name in _MUTATING_TOOLS:
            pending_holder.append(PendingAction(tool_name=name, args=args))
            return (
                "This action requires user confirmation. "
                "Describe exactly what you will do and ask the user to confirm."
            )

        return await self._run_calendar_tool(user_id, name, args)

    async def _run_calendar_tool(self, user_id: int, name: str, args: dict) -> str:
        credentials = await auth_service.get_credentials(user_id)
        if credentials is None:
            return "Error: user is not authenticated with Google. Ask them to run /auth."

        calendar_id = await auth_service.get_calendar_id(user_id) or "primary"
        user_tz = ZoneInfo(await auth_service.get_user_timezone(user_id))

        def _fix_tz(iso: str) -> datetime:
            """Parse ISO datetime and force the user's timezone."""
            dt = datetime.fromisoformat(iso)
            # Replace whatever offset the model used with the real user tz
            return dt.replace(tzinfo=None).replace(tzinfo=user_tz)

        try:
            if name == "read_events":
                time_min: datetime | None = None
                time_max: datetime | None = None
                if args.get("time_min"):
                    time_min = datetime.fromisoformat(args["time_min"])
                if args.get("time_max"):
                    time_max = datetime.fromisoformat(args["time_max"])
                events = await calendar_service.list_events(
                    credentials,
                    calendar_id=calendar_id,
                    max_results=int(args.get("max_results", 25)),
                    time_min=time_min,
                    time_max=time_max,
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
                    start=_fix_tz(args["start_time"]) if args.get("start_time") else None,
                    end=_fix_tz(args["end_time"]) if args.get("end_time") else None,
                    start_date=(
                        date.fromisoformat(args["start_date"]) if args.get("start_date") else None
                    ),
                    end_date=(
                        date.fromisoformat(args["end_date"]) if args.get("end_date") else None
                    ),
                    description=args.get("description"),
                    location=args.get("location"),
                )
                created = await calendar_service.create_event(
                    credentials, ev, calendar_id=calendar_id
                )
                return f"Created: {created.summary} (ID:{created.event_id})"

            if name == "update_event":
                up = EventUpdate(
                    event_id=args["event_id"],
                    summary=args.get("summary"),
                    start=_fix_tz(args["start_time"]) if args.get("start_time") else None,
                    end=_fix_tz(args["end_time"]) if args.get("end_time") else None,
                    start_date=(
                        date.fromisoformat(args["start_date"]) if args.get("start_date") else None
                    ),
                    end_date=(
                        date.fromisoformat(args["end_date"]) if args.get("end_date") else None
                    ),
                    description=args.get("description"),
                    location=args.get("location"),
                )
                updated = await calendar_service.update_event(
                    credentials, up, calendar_id=calendar_id
                )
                return f"Updated: {updated.summary}"

            if name == "delete_event":
                await calendar_service.delete_event(
                    credentials, args["event_id"], calendar_id=calendar_id
                )
                return f"Deleted event {args['event_id']}."

        except (RuntimeError, ValueError) as exc:
            logger.error("Tool %s failed: %s", name, exc)
            return f"Error in {name}: {exc}"

        return f"Unknown tool: {name}"

    async def execute_confirmed_action(self, user_id: int, tool_name: str, args: dict) -> str:
        """Execute a mutating calendar action after user confirmation."""
        return await self._run_calendar_tool(user_id, tool_name, args)

    async def process_message(self, user_id: int, message: str) -> AgentResponse:
        """Run a free-text message through the AI model and return the final reply."""
        try:
            client = self._get_client()
        except RuntimeError as exc:
            return AgentResponse(text=str(exc))

        image_holder: list[str] = []
        pending_holder: list[PendingAction] = []

        user_tz = await auth_service.get_user_timezone(user_id)
        tz = ZoneInfo(user_tz)
        system_text = _SYSTEM_PROMPT.format(
            now=datetime.now(tz=tz).isoformat(),
            timezone=user_tz,
        )
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
                return AgentResponse(text="Sorry, I couldn't reach the AI service right now.")

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
                tool_result = await self._execute_tool(
                    user_id, tc.function.name, args, image_holder, pending_holder
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": tool_result,
                    }
                )

        last = response.choices[0].message.content  # type: ignore[possibly-undefined]
        return AgentResponse(
            text=last or "I couldn't generate a response.",
            image_url=image_holder[0] if image_holder else None,
            pending_action=pending_holder[-1] if pending_holder else None,
        )


ai_agent = AIAgent()
