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
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from google.auth.exceptions import RefreshError
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
_HISTORY_TURNS = 10  # pairs of (user, assistant) messages retained per user


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


def _resolve_date_range(period: str, now: datetime) -> tuple[datetime, datetime]:
    """Return ``(time_min, time_max)`` for a relative period in *now*'s timezone.

    ``time_max`` is exclusive (midnight of the day AFTER the last day of the period).
    Weekend = Saturday–Sunday; week = Monday–Sunday.
    """
    tz = now.tzinfo
    today = now.date()
    weekday = today.weekday()  # Mon=0 .. Sun=6

    if period in ("this_weekend", "next_weekend"):
        # Saturday of the current/upcoming weekend:
        # Mon–Sat → upcoming (or same) Saturday; Sun → previous day's Saturday.
        if weekday <= 5:
            this_sat = today + timedelta(days=5 - weekday)
        else:
            this_sat = today - timedelta(days=1)
        sat = this_sat + (timedelta(days=7) if period == "next_weekend" else timedelta(0))
        mon = sat + timedelta(days=2)
        start = datetime(sat.year, sat.month, sat.day, tzinfo=tz)
        end = datetime(mon.year, mon.month, mon.day, tzinfo=tz)
        return start, end

    if period in ("this_week", "next_week"):
        mon = today - timedelta(days=weekday)
        if period == "next_week":
            mon = mon + timedelta(days=7)
        next_mon = mon + timedelta(days=7)
        start = datetime(mon.year, mon.month, mon.day, tzinfo=tz)
        end = datetime(next_mon.year, next_mon.month, next_mon.day, tzinfo=tz)
        return start, end

    raise ValueError(f"Unknown period: {period!r}")


def _city_from_tz(tz_name: str) -> str:
    """Extract a human-readable city from an IANA tz name for search queries.

    ``Europe/Belgrade`` → ``Belgrade``; ``America/New_York`` → ``New York``.
    Falls back to the raw tz name if there's no ``/``.
    """
    last = tz_name.rsplit("/", 1)[-1]
    return last.replace("_", " ")


_REGIONAL_SOURCES: dict[str, list[str]] = {
    "Europe/Belgrade": ["gigstix.com", "eventim.rs", "tickets.rs"],
}


def _regional_sources_str(tz_name: str) -> str:
    """Human-readable list of regional ticket sites for the user's tz.

    Returned string is interpolated into the system prompt to nudge the model
    toward local promoters that the global aggregators (Bandsintown, Songkick,
    Ticketmaster, RA) miss — e.g. Serbian shows are routinely listed only on
    gigstix.com / eventim.rs / tickets.rs.
    """
    sources = _REGIONAL_SOURCES.get(tz_name, [])
    if not sources:
        return "(none configured — rely on the global sources below)"
    return ", ".join(sources)


def _detect_language(text: str) -> str:
    """Detect language from user message. Simple heuristic based on character ranges."""
    cyrillic = sum(1 for c in text if "\u0400" <= c <= "\u04ff")
    latin = sum(1 for c in text if "a" <= c.lower() <= "z")
    if cyrillic > latin:
        return "Russian"
    if latin > cyrillic:
        return "English"
    return "Russian"


_SYSTEM_PROMPT = (
    "You are a calendar assistant. Today is {now}.\n"
    "The user's timezone is {timezone}. The user's city is {city}. "
    "Always use this timezone for dates and times, and this city as the default "
    "location when searching for local events.\n"
    "You ONLY manage the user's Google Calendar through the provided tools.\n"
    "You must REFUSE any questions or requests not related to calendar events "
    "(e.g. general knowledge, chitchat, jokes). Politely reply that you can only "
    "help with calendar management.\n"
    "However, simple date/time questions (day of the week, how many days until a date, "
    "etc.) ARE within your scope — you are a calendar assistant and should answer them. "
    "DISCOVERING things to do (concerts, parties, stand-up, festivals, exhibitions, etc.) "
    "in the user's city is ALSO within scope, because the goal is to help plan the "
    "calendar — see the 'event discovery' rules below.\n"
    "Rules:\n"
    "- You do NOT know event IDs. Always call read_events first before "
    "update_event or delete_event.\n"
    "- READ_EVENTS RULES:\n"
    "  • If the user asks about a SPECIFIC DATE: set BOTH time_min and time_max "
    "to exactly that day (e.g. time_min='2026-07-19T00:00:00' time_max='2026-07-20T00:00:00').\n"
    "  • If no specific date: do NOT set time_min — the system defaults to now, "
    "showing only future events. NEVER set time_min to a past date.\n"
    "  • For RELATIVE PERIODS (weekend / week in any language — "
    "'выходные', 'ближайшие выходные', 'этих выходных', 'следующие выходные', "
    "'this week', 'next week', 'на этой неделе', etc.): FIRST call get_date_range "
    "to resolve time_min/time_max, THEN call read_events with those bounds. "
    "Do NOT compute weekend/week dates yourself — always use get_date_range.\n"
    "- When creating events, always ask for both start and end times if not given.\n"
    "- ALL-DAY / MULTI-DAY EVENTS: when the user says 'с X по Y', 'X-Y июня', "
    "'from X through Y', 'X to Y inclusive', etc., set start_date=X and "
    "end_date=Y (the LITERAL last day). DO NOT shift end_date by +1; the "
    "server converts to Google Calendar's exclusive end internally. "
    "Example: 'поездка с 18 по 21 июня 2026' → start_date=2026-06-18, "
    "end_date=2026-06-21 (not 2026-06-22).\n"
    "- LANGUAGE RULE: You MUST reply in {language}. Every single word of your response "
    "must be in {language}. NEVER use Serbian, even if location data is in Serbian. "
    "Translate ALL foreign text (addresses, venue names, search results) into {language}. "
    "For example: 'Žorža Klemansoa 37, Beograd' → 'ул. Жоржа Клемансо 37, Белград' in Russian.\n"
    "- Be concise.\n"
    "- FORMATTING: You output for Telegram. Use ONLY Telegram HTML tags:\n"
    '  <b>bold</b>, <i>italic</i>, <code>code</code>, <a href="URL">link text</a>.\n'
    "  NEVER use Markdown: no **, no ### headers, no [text](url), no ![image](url).\n"
    '  For links: <a href="https://example.com">Click here</a>.\n'
    "  For lists: use • bullet character.\n"
    "  For event titles and dates: use <b>.\n"
    "  NEVER include images in your text. No ![alt](url), no <img> tags. "
    "Images are handled automatically by the system via find_event_image tool.\n"
    "- SCHEDULE QUERIES — when the user asks what's on their calendar today / "
    "tomorrow / this week / a specific date (e.g. 'что сегодня?', 'что в "
    "календаре?', 'покажи события', 'what's on today?', 'what do I have "
    "tomorrow?', 'мои планы на неделю'), ALWAYS call read_events with the "
    "appropriate time bounds. Do NOT just answer with the calendar date — "
    "the user is asking about events, not asking what day it is.\n"
    "- DO NOT REPEAT COMPLETED ACTIONS — if recent conversation history "
    "contains '✅ Action complete: …' / 'Created: …' / 'Updated: …' / "
    "'Deleted: …' / '[Confirmed action …]', that mutation has ALREADY been "
    "executed and persisted. Do not call create_event / update_event / "
    "delete_event again with the same data, and do not propose another "
    "confirmation. If the next user message is a NEW request, treat it as new.\n"
    "- WHEN-IS queries (e.g. 'когда skillet?', 'когда концерт moby?', "
    "'when is the concert?'):\n"
    "  1. FIRST call read_events to check the calendar.\n"
    "  2. If the event IS in the calendar, answer with the date/time from there.\n"
    "  3. If the event is NOT in the calendar, DO NOT search the web yet and DO NOT "
    "call any other tools. Instead, reply with ONE short message asking the user for "
    "permission to search the web, and include the user's city. Examples: «В "
    "календаре такого события нет. Хочешь, поищу в интернете, когда и где будет "
    "концерт <X> в {city}?» / 'I don't see it in your calendar. Want me to search "
    "the web for the <X> concert in {city}?'. Then STOP and wait for the user's "
    "next message — do not call any tool in this turn.\n"
    "  4. Only AFTER the user confirms in a follow-up turn (e.g. 'да', 'давай', "
    "'yes', 'sure'), run web_search with an English query that includes {city}. "
    "Then MANDATORY: call fetch_url on the most promising per-event URL "
    "(bandsintown.com, gigstix.com, eventim.rs, songkick.com, ticketmaster.com, "
    "ra.co, venue site, etc.) and read the actual page to extract the concrete "
    "date / venue. NEVER report a date, tour name, or venue based on the search "
    "snippet alone — snippets routinely contain old tour info or shows from other "
    "cities, and the model must not invent details. If fetch_url does not yield "
    "a verifiable date for {city}, say so honestly (e.g. «В {city} подтверждённых "
    "анонсов концерта <X> не нашёл») — do NOT fabricate a date, tour name, or "
    "venue, and do NOT propose events from other cities/countries. Only if a "
    "concrete date IS verified from the fetched page, call create_event with the "
    "verified date / venue / details so the confirmation system can ask the user "
    "to approve via buttons.\n"
    "- When the user asks to FIND INFORMATION about something (e.g. 'найди информацию', "
    "'find info about'), ALWAYS do ALL of these steps:\n"
    "  1. Call read_events to find the event in the calendar.\n"
    "  2. Call web_search with an ENGLISH query. ALWAYS include the EXACT DATE "
    "from the calendar event in your query "
    "(e.g. 'Skillet concert Belgrade May 28 2026 tickets venue'). "
    "If the first search returns little info, try a second search with different keywords.\n"
    "  3. Call find_event_image to find a relevant photo.\n"
    "  4. DATE VERIFICATION (CRITICAL): Before presenting results, CHECK that any dates, "
    "ticket links, or event pages from web search match the date in the user's calendar. "
    "Events often have multiple dates in the same city. If a search result is for a "
    "DIFFERENT DATE than the calendar event, DISCARD it and warn the user. "
    "NEVER present ticket links or event pages without confirming the date matches.\n"
    "  5. Present all verified info to the user.\n"
    "  6. IMMEDIATELY call a mutating tool to persist the found details:\n"
    "     - If step 1 found a matching calendar event, call update_event "
    "(set description with key info like time/tickets/links, set location with venue address).\n"
    "     - If there is NO matching calendar event, call create_event with the found "
    "date/venue/details so the user can save it.\n"
    "     Do NOT ask the user in plain text whether to create/update — always call the tool. "
    "The confirmation system will ask the user to approve via buttons.\n"
    "- Use web_search to look up additional info about events "
    "(e.g. venue details, artist info, ticket prices, setlists).\n"
    "- Use find_event_image when searching for event info or when it clearly adds value.\n"
    "- EVENT DISCOVERY queries (e.g. 'куда сходить на выходных?', 'что происходит в "
    "эту субботу?', 'where can I go tonight?', 'any stand-up this weekend?'):\n"
    "  1. If the dates are RELATIVE (this weekend, next week, tonight, tomorrow…) "
    "call get_date_range first; for a specific date use it directly.\n"
    "  2. Call discover_local_events with the resolved date_min / date_max. "
    "If the user asked for a specific genre, pass it (e.g. genres=['hip-hop'] "
    "for «хип-хоп концерты», genres=['metal','metalcore'] for «металкор»). "
    "The tool runs the full search + fetch pipeline in code "
    "(Bandsintown by genre, regional ticketing sources for {city} — "
    "{regional_sources}, plus Songkick / Ticketmaster / RA) and returns "
    "fetched event-page text. After it returns, DO NOT call web_search or "
    "fetch_url again for discovery — parse the returned text.\n"
    "  3. STRICT DATE FILTER — every option you present MUST include a "
    "CONCRETE DATE (day + month + year) that falls inside the requested "
    "date range. If you cannot extract a real date for a candidate, DROP "
    "that option — do NOT write 'дата не указана' or 'TBD'. If "
    "discover_local_events returned no usable dated events, say so "
    "honestly: «Не нашёл публичных анонсов с конкретными датами для "
    "{city} на этот период.», and stop.\n"
    "  4. STRICT GENRE — if the user asked specifically for rock / metal / "
    "electronic / stand-up / etc., do NOT include unrelated event types "
    "(conferences, exhibitions, classical, choir festivals) just to hit a "
    "3-5 count. Filter by the requested genre.\n"
    "  5. Present 3-5 concrete options. For EACH include ALL of: "
    "<b>name</b>, date/time, venue, and a clickable link "
    '(<a href="...">text</a>). The link is MANDATORY and must point to '
    "the INDIVIDUAL event, NOT a listing / metro-area / city page. The "
    "tool output puts URLs in [brackets] right after each anchor's text — "
    "use those per-event URLs. If a per-event URL is not available for an "
    "option, DROP it (do NOT substitute the listing URL). Better fewer "
    "options each with a real link than a long list with fake/listing "
    "links. Do not invent URLs.\n"
    "  6. At the end, ask the user which one(s) they'd like to add to the "
    "calendar. If they confirm, call create_event for each picked one.\n"
    "  7. NEVER fabricate events, ticket URLs, venues, or dates."
)

_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "read_events",
            "description": (
                "List calendar events. Call this first whenever you need an event_id. "
                "If no time_min is set, only FUTURE events are returned. "
                "For a specific date, set BOTH time_min and time_max to that day's boundaries."
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
                            "End date YYYY-MM-DD — the LAST day of the event "
                            "(INCLUSIVE). Just copy the date the user said. "
                            "Examples: 'с 18 по 21 июня 2026' / 'June 18 through 21, "
                            "2026' → end_date=2026-06-21. Single all-day event on "
                            "Apr 5 → start_date=end_date=2026-04-05. "
                            "DO NOT add +1 day — the server handles the Google "
                            "Calendar exclusive-end conversion internally."
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
                        "description": (
                            "New end date YYYY-MM-DD — LAST day of the event "
                            "(INCLUSIVE). DO NOT add +1 day; the server handles "
                            "the Google Calendar exclusive-end conversion."
                        ),
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
            "name": "get_date_range",
            "description": (
                "Resolve a relative date period (weekend / week) into ISO 8601 "
                "time_min/time_max bounds in the user's timezone. "
                "Call BEFORE read_events for queries like 'this weekend', "
                "'ближайшие выходные', 'следующие выходные', 'next week', etc. "
                "Do NOT use for specific calendar dates — pass those directly "
                "to read_events."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "period": {
                        "type": "string",
                        "enum": [
                            "this_weekend",
                            "next_weekend",
                            "this_week",
                            "next_week",
                        ],
                        "description": (
                            "this_weekend: upcoming Sat–Sun (or current, if today is Sat/Sun). "
                            "next_weekend: the Sat–Sun one week after this_weekend. "
                            "this_week: current Mon–Sun. "
                            "next_week: next Mon–Sun."
                        ),
                    },
                },
                "required": ["period"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the web for current information about events, venues, artists, "
                "tickets, prices, or any topic. ALWAYS use English queries for best "
                "coverage. Use whenever the user asks to find information, even if "
                "the event is already in the calendar. If results are sparse, call "
                "again with different keywords."
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
            "name": "fetch_url",
            "description": (
                "Download the readable text content of a web page. Use this AFTER "
                "web_search returned a URL to a specialised event database "
                "(bandsintown.com, gigstix.com, eventim.rs, songkick.com, "
                "ticketmaster.com, ra.co, venue site, etc.) so you can extract "
                "real event names + dates from the listing instead of guessing "
                "from the search snippet. Returns up to ~3500 characters of "
                "stripped text. If you need a second page, call fetch_url again "
                "with the next URL."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Absolute http(s) URL to fetch.",
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "discover_local_events",
            "description": (
                "Find concerts / parties / shows / stand-up happening in the "
                "user's city for a given date range. Use this FOR ALL event-"
                "discovery queries ('куда сходить', 'concerts this week', "
                "'что происходит', etc.) instead of running web_search "
                "manually. The tool queries Bandsintown by genre, regional "
                "ticketing for the user's region (gigstix.com / eventim.rs / "
                "tickets.rs for Belgrade), and Songkick / Ticketmaster / RA "
                "in parallel, fetches the most relevant event pages, and "
                "returns the raw text content for you to extract concrete "
                "event names, dates, venues, and per-event URLs from. After "
                "calling this tool, DO NOT call web_search again — parse the "
                "returned text and present 3-5 verified options with dates "
                "inside the requested range."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "date_min": {
                        "type": "string",
                        "description": "Start of date range, YYYY-MM-DD (inclusive).",
                    },
                    "date_max": {
                        "type": "string",
                        "description": "End of date range, YYYY-MM-DD (exclusive).",
                    },
                    "genres": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional genre filter — e.g. ['rock', 'metal'] "
                            "or ['hip-hop']. If omitted, queries a broad set: "
                            "rock / metal / metalcore / hip-hop / electronic."
                        ),
                    },
                },
                "required": ["date_min", "date_max"],
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


def _ddgs_proxy() -> str | None:
    return settings.proxy_url or None


def _ddgs_text_sync(query: str, max_results: int) -> list[dict]:
    from ddgs import DDGS

    with DDGS(proxy=_ddgs_proxy()) as ddgs:
        return list(ddgs.text(query, max_results=max_results))


def _ddgs_images_sync(query: str) -> list[dict]:
    from ddgs import DDGS

    # Try image API first
    try:
        with DDGS(proxy=_ddgs_proxy()) as ddgs:
            results = list(ddgs.images(query, type_image="photo", size="Large", max_results=5))
            if results:
                return results
    except Exception as exc:
        logger.info("ddgs.images failed (%s), trying simpler query", exc)

    # Retry with simpler query
    simple_query = query.split()[0] if query.split() else query
    try:
        with DDGS(proxy=_ddgs_proxy()) as ddgs:
            results = list(ddgs.images(simple_query, max_results=5))
            if results:
                return results
    except Exception as exc:
        logger.info("ddgs.images retry failed (%s)", exc)

    return []


async def _web_search(query: str, max_results: int = 5) -> str:
    logger.info("web_search query=%r max_results=%d", query, max_results)
    try:
        results = await asyncio.to_thread(_ddgs_text_sync, query, max_results)
    except Exception as exc:
        logger.warning("web_search failed: %s", exc)
        return "Search temporarily unavailable."
    if not results:
        logger.info("web_search returned 0 results")
        return "No results found."
    logger.info("web_search returned %d results", len(results))
    lines = [f"{r['title']}\n{r['href']}\n{r['body']}" for r in results]
    return "\n\n".join(lines)


_FETCH_URL_MAX_CHARS = 3500


def _absolutize(href: str, base_url: str) -> str:
    """Make a relative href absolute, given the page base URL."""
    from urllib.parse import urljoin

    return urljoin(base_url, href)


async def _fetch_url(url: str) -> str:
    """Download a page, preserve link URLs, strip remaining tags.

    The agent uses this to read event-listing pages (bandsintown, songkick,
    ticketmaster, ra.co, venue sites) when search snippets aren't enough.
    Anchor tags (`<a href=...>text</a>`) are converted to ``text [URL]``
    BEFORE the rest of the HTML is stripped, so the agent can quote the
    real per-event link instead of substituting the listing page URL.
    Returns ≤3500 chars of text.
    """
    import re

    import httpx

    if not url.lower().startswith(("http://", "https://")):
        return f"Error: invalid URL: {url}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/123.0.0.0 Safari/537.36"
        ),
        "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8"),
        "Accept-Language": "en-US,en;q=0.9,ru;q=0.7",
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0), follow_redirects=True) as c:
            resp = await c.get(url, headers=headers)
            resp.raise_for_status()
            html = resp.text
            base_url = str(resp.url)  # follow_redirects may have changed it
    except Exception as exc:
        logger.warning("fetch_url failed for %s: %s: %s", url, type(exc).__name__, exc)
        return f"Error: could not fetch {url} ({type(exc).__name__})"

    # Drop scripts/styles entirely.
    html = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.IGNORECASE)
    html = re.sub(r"<style[\s\S]*?</style>", " ", html, flags=re.IGNORECASE)

    # Convert <a href="X">label</a> → "label [X]" with absolutised X so the
    # agent can quote per-event URLs after the tag-strip below.
    def _replace_anchor(match: "re.Match[str]") -> str:
        href = _absolutize(match.group(1).strip(), base_url)
        inner = re.sub(r"<[^>]+>", " ", match.group(2))
        inner = re.sub(r"\s+", " ", inner).strip()
        if not inner:
            return f" [{href}] "
        return f" {inner} [{href}] "

    html = re.sub(
        r'<a\s+[^>]*?href=["\']([^"\']+)["\'][^>]*>([\s\S]*?)</a>',
        _replace_anchor,
        html,
        flags=re.IGNORECASE,
    )

    # Now strip all remaining tags and collapse whitespace.
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > _FETCH_URL_MAX_CHARS:
        text = text[:_FETCH_URL_MAX_CHARS] + "… [truncated]"
    return text or "(empty page)"


_EVENT_URL_PATTERNS: list = []


def _compile_event_url_patterns() -> list:
    import re

    return [
        re.compile(p, re.IGNORECASE)
        for p in (
            r"bandsintown\.com/e/",
            r"bandsintown\.com/c/",
            r"gigstix\.com/event/",
            r"new\.gigstix\.com/event/",
            r"eventim\.rs/",
            r"tickets\.rs/",
            r"songkick\.com/concerts/",
            r"songkick\.com/metro-areas/",
            r"ra\.co/events/",
            r"ticketmaster\.[a-z.]+/event/",
        )
    ]


_EVENT_URL_PATTERNS = _compile_event_url_patterns()
_DEFAULT_DISCOVERY_GENRES: tuple[str, ...] = (
    "rock",
    "metal",
    "metalcore",
    "hip-hop",
    "electronic",
)
_DISCOVER_FETCH_CAP = 6


def _extract_candidate_urls(search_result: str) -> list[str]:
    """Pick URLs from _web_search output that match known event-site patterns.

    _web_search returns ``<title>\\n<href>\\n<body>`` blocks separated by
    blank lines. We pull the href lines and keep only ones matching the
    whitelist so the discovery pipeline doesn't waste fetch_url budget on
    unrelated pages (e.g. wikipedia entries about the artist).
    """
    urls: list[str] = []
    for raw in search_result.splitlines():
        line = raw.strip()
        if line.startswith(("http://", "https://")) and any(
            p.search(line) for p in _EVENT_URL_PATTERNS
        ):
            urls.append(line)
    return urls


def _host_key(url: str) -> str:
    """Return a normalised hostname for grouping URLs by source."""
    from urllib.parse import urlparse

    host = urlparse(url).netloc.lower()
    return host.removeprefix("www.").removeprefix("new.")


def _interleave_by_host(urls: list[str]) -> list[str]:
    """Round-robin URLs so each hostname contributes one URL before any
    hostname contributes its second. Prevents a single dominant source
    (e.g. bandsintown's 5 genre searches) from monopolising the fetch budget,
    so smaller-but-critical regional sources still get a fetch attempt even
    if the dominant source ends up 403-ing us anyway.
    """
    buckets: dict[str, list[str]] = {}
    order: list[str] = []
    for u in urls:
        h = _host_key(u)
        if h not in buckets:
            buckets[h] = []
            order.append(h)
        buckets[h].append(u)
    out: list[str] = []
    while any(buckets[h] for h in order):
        for h in order:
            if buckets[h]:
                out.append(buckets[h].pop(0))
    return out


async def _discover_local_events(
    city: str,
    tz_name: str,
    date_min: str,
    date_max: str,
    genres: list[str] | None = None,
) -> str:
    """Run the full event-discovery pipeline and return a verifiable listing.

    Searches Bandsintown by genre, regional ticketing for the user's tz, and
    the global aggregators (Songkick / Ticketmaster / RA) in parallel, then
    fetches the top candidate URLs and concatenates their text content. The
    model receives one structured blob instead of choosing which searches to
    run — empirically the model was lazy about following multi-step search +
    fetch prompt instructions, so this encapsulates discovery in code.
    """
    genres_to_query = list(genres) if genres else list(_DEFAULT_DISCOVERY_GENRES)
    queries: list[str] = [f"site:bandsintown.com {city} {g}" for g in genres_to_query]
    for src in _REGIONAL_SOURCES.get(tz_name, []):
        queries.append(f"site:{src} {city}")
    queries.extend(
        [
            f"concerts {city} site:songkick.com",
            f"site:ra.co {city}",
            f"site:ticketmaster.com {city} concerts",
        ]
    )

    logger.info(
        "discover_local_events city=%s tz=%s dates=%s..%s queries=%d",
        city,
        tz_name,
        date_min,
        date_max,
        len(queries),
    )

    search_results = await asyncio.gather(
        *[_web_search(q, max_results=5) for q in queries],
        return_exceptions=True,
    )

    seen: set[str] = set()
    candidate_urls: list[str] = []
    for result in search_results:
        if not isinstance(result, str):
            continue
        for url in _extract_candidate_urls(result):
            if url not in seen:
                seen.add(url)
                candidate_urls.append(url)

    if not candidate_urls:
        logger.info("discover_local_events: 0 candidate URLs")
        return (
            f"No candidate event URLs found for {city} between {date_min} "
            f"and {date_max} after {len(queries)} searches. Tell the user "
            "you couldn't find verifiable listings — do NOT fabricate events."
        )

    interleaved = _interleave_by_host(candidate_urls)
    top_urls = interleaved[:_DISCOVER_FETCH_CAP]
    logger.info(
        "discover_local_events: %d candidates across %d hosts, fetching %d: %s",
        len(candidate_urls),
        len({_host_key(u) for u in candidate_urls}),
        len(top_urls),
        top_urls,
    )
    fetched = await asyncio.gather(
        *[_fetch_url(u) for u in top_urls],
        return_exceptions=True,
    )

    parts: list[str] = [
        f"Event-discovery results for {city}, {date_min} to {date_max}. "
        f"Below are {len(top_urls)} fetched event/listing pages — parse them "
        "for CONCRETE event names + dates + venues + per-event URLs (anchors "
        "are rendered as 'text [URL]'). Present 3-5 options whose dates fall "
        "inside the requested range; DROP any option without a verified date."
    ]
    for url, text in zip(top_urls, fetched, strict=True):
        if isinstance(text, BaseException):
            parts.append(f"=== {url}\n[fetch failed: {type(text).__name__}]")
            continue
        parts.append(f"=== {url}\n{text}")
    return "\n\n".join(parts)


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
        self._history: dict[int, deque[ChatCompletionMessageParam]] = {}

    def _get_history(self, user_id: int) -> deque[ChatCompletionMessageParam]:
        hist = self._history.get(user_id)
        if hist is None:
            hist = deque(maxlen=_HISTORY_TURNS * 2)
            self._history[user_id] = hist
        return hist

    def note_assistant(self, user_id: int, text: str) -> None:
        """Record a synthetic assistant message (e.g. confirmation outcome)
        so the next turn knows what just happened."""
        if not text:
            return
        self._get_history(user_id).append({"role": "assistant", "content": text})

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
        if name == "get_date_range":
            user_tz = ZoneInfo(await auth_service.get_user_timezone(user_id))
            try:
                start, end = _resolve_date_range(args.get("period", ""), datetime.now(tz=user_tz))
            except ValueError as exc:
                return f"Error: {exc}"
            return json.dumps({"time_min": start.isoformat(), "time_max": end.isoformat()})

        if name == "web_search":
            return await _web_search(
                query=args.get("query", ""),
                max_results=min(int(args.get("max_results", 5)), 10),
            )

        if name == "fetch_url":
            return await _fetch_url(args.get("url", ""))

        if name == "discover_local_events":
            user_tz_name = await auth_service.get_user_timezone(user_id)
            raw_genres = args.get("genres")
            genres = [str(g) for g in raw_genres] if isinstance(raw_genres, list) else None
            return await _discover_local_events(
                city=_city_from_tz(user_tz_name),
                tz_name=user_tz_name,
                date_min=str(args.get("date_min", "")),
                date_max=str(args.get("date_max", "")),
                genres=genres,
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
            """Parse ISO datetime and force the user's timezone.

            The model may return a wrong UTC offset or a naive datetime.
            We strip the offset and attach the real user timezone so that
            "15:00" always means 15:00 in the user's local time.
            """
            dt = datetime.fromisoformat(iso)
            return dt.replace(tzinfo=None).replace(tzinfo=user_tz)

        def _fix_end(start: datetime | None, end: datetime | None) -> datetime | None:
            """If end <= start (e.g. 20:00–00:00), push end to the next day."""
            if start and end and end <= start:
                end += timedelta(days=1)
            return end

        try:
            if name == "read_events":
                time_min: datetime | None = None
                time_max: datetime | None = None
                if args.get("time_min"):
                    time_min = _fix_tz(args["time_min"])
                if args.get("time_max"):
                    time_max = _fix_tz(args["time_max"])
                # Prevent searching the past when no specific date range is intended
                now = datetime.now(tz=user_tz)
                if time_min and time_min < now and not time_max:
                    time_min = None  # fall back to "from now"
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
                start = _fix_tz(args["start_time"]) if args.get("start_time") else None
                end = _fix_end(start, _fix_tz(args["end_time"]) if args.get("end_time") else None)
                # Tool contract: end_date is the INCLUSIVE last day. Google
                # Calendar wants an exclusive end, so add +1 day here.
                end_d_inclusive = (
                    date.fromisoformat(args["end_date"]) if args.get("end_date") else None
                )
                ev = EventCreate(
                    summary=args["summary"],
                    start=start,
                    end=end,
                    start_date=(
                        date.fromisoformat(args["start_date"]) if args.get("start_date") else None
                    ),
                    end_date=(end_d_inclusive + timedelta(days=1) if end_d_inclusive else None),
                    description=args.get("description"),
                    location=args.get("location"),
                )
                created = await calendar_service.create_event(
                    credentials, ev, calendar_id=calendar_id
                )
                return f"Created: {created.summary} (ID:{created.event_id})"

            if name == "update_event":
                u_start = _fix_tz(args["start_time"]) if args.get("start_time") else None
                u_end = _fix_end(
                    u_start, _fix_tz(args["end_time"]) if args.get("end_time") else None
                )
                # Tool contract: end_date is INCLUSIVE; Google needs exclusive.
                u_end_d_inclusive = (
                    date.fromisoformat(args["end_date"]) if args.get("end_date") else None
                )
                up = EventUpdate(
                    event_id=args["event_id"],
                    summary=args.get("summary"),
                    start=u_start,
                    end=u_end,
                    start_date=(
                        date.fromisoformat(args["start_date"]) if args.get("start_date") else None
                    ),
                    end_date=(u_end_d_inclusive + timedelta(days=1) if u_end_d_inclusive else None),
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

        except RefreshError:
            logger.warning("Refresh token revoked for user %d, clearing credentials", user_id)
            await auth_service.revoke_tokens(user_id)
            return (
                "Error: your Google authorization has expired or been revoked. "
                "Please run /auth to reconnect your Google account."
            )
        except (RuntimeError, ValueError) as exc:
            logger.error("Tool %s failed: %s", name, exc)
            if "404" in str(exc):
                return "Error: event not found (maybe deleted). Call read_events to get fresh IDs."
            return f"Error in {name}: {exc}"

        return f"Unknown tool: {name}"

    async def execute_confirmed_action(self, user_id: int, tool_name: str, args: dict) -> str:
        """Execute a mutating calendar action after user confirmation."""
        try:
            return await self._run_calendar_tool(user_id, tool_name, args)
        except RuntimeError as exc:
            msg = str(exc)
            if "404" in msg:
                return "Событие не найдено — возможно, оно было удалено. Попробуй ещё раз."
            raise

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
        language = _detect_language(message)
        system_text = _SYSTEM_PROMPT.format(
            now=datetime.now(tz=tz).isoformat(),
            timezone=user_tz,
            city=_city_from_tz(user_tz),
            language=language,
            regional_sources=_regional_sources_str(user_tz),
        )
        history = self._get_history(user_id)
        messages: list[ChatCompletionMessageParam] = [
            {"role": "system", "content": system_text},
            *history,
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
        if last:
            history.append({"role": "user", "content": message})
            history.append({"role": "assistant", "content": last})
        return AgentResponse(
            text=last or "I couldn't generate a response.",
            image_url=image_holder[0] if image_holder else None,
            pending_action=pending_holder[-1] if pending_holder else None,
        )


ai_agent = AIAgent()
