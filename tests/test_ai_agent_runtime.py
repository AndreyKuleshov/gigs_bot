"""Coverage for ai_agent runtime: web search, OpenAI plumbing, _execute_tool,
_run_calendar_tool, process_message."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from google.auth.exceptions import RefreshError

from app.services.ai_agent import (
    AIAgent,
    _ddgs_images_sync,
    _ddgs_proxy,
    _ddgs_text_sync,
    _discover_local_events,
    _extract_candidate_urls,
    _fetch_url,
    _find_event_image,
    _find_fabricated_event_urls,
    _interleave_by_host,
    _web_search,
    ai_agent,
)

# ── _ddgs_proxy / sync helpers ────────────────────────────────────────────────


def test_ddgs_proxy_returns_none_when_unset():
    with patch("app.services.ai_agent.settings") as s:
        s.proxy_url = ""
        assert _ddgs_proxy() is None


def test_ddgs_proxy_returns_value_when_set():
    with patch("app.services.ai_agent.settings") as s:
        s.proxy_url = "http://proxy:3128"
        assert _ddgs_proxy() == "http://proxy:3128"


def test_ddgs_text_sync_calls_underlying_lib():
    fake_results = [{"title": "x", "href": "u", "body": "b"}]
    fake_ddgs = MagicMock()
    fake_ddgs.__enter__.return_value.text.return_value = iter(fake_results)
    with patch("ddgs.DDGS", return_value=fake_ddgs):
        out = _ddgs_text_sync("q", 5)
    assert out == fake_results


def test_ddgs_images_sync_returns_first_round_results():
    fake_results = [{"image": "a"}, {"image": "b"}]
    fake_ddgs = MagicMock()
    fake_ddgs.__enter__.return_value.images.return_value = iter(fake_results)
    with patch("ddgs.DDGS", return_value=fake_ddgs):
        out = _ddgs_images_sync("query")
    assert out == fake_results


def test_ddgs_images_sync_retries_with_simpler_query_when_first_fails():
    """First DDGS attempt raises; the function retries with the first word."""
    second_results = [{"image": "z"}]
    call_count = {"n": 0}

    def fake_factory(**_):
        call_count["n"] += 1
        ctx = MagicMock()
        if call_count["n"] == 1:
            ctx.__enter__.return_value.images.side_effect = RuntimeError("boom")
        else:
            ctx.__enter__.return_value.images.return_value = iter(second_results)
        return ctx

    with patch("ddgs.DDGS", side_effect=fake_factory):
        out = _ddgs_images_sync("multi word query")
    assert out == second_results


def test_ddgs_images_sync_returns_empty_when_both_attempts_fail():
    fake_ddgs = MagicMock()
    fake_ddgs.__enter__.return_value.images.side_effect = RuntimeError("nope")
    with patch("ddgs.DDGS", return_value=fake_ddgs):
        assert _ddgs_images_sync("q") == []


# ── _web_search / _find_event_image ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_web_search_formats_results():
    fake = [
        {"title": "Title", "href": "https://x", "body": "Snippet"},
    ]
    with patch(
        "app.services.ai_agent.asyncio.to_thread",
        new=AsyncMock(return_value=fake),
    ):
        out = await _web_search("q")
    assert "Title" in out and "https://x" in out and "Snippet" in out


@pytest.mark.asyncio
async def test_web_search_returns_no_results_message_for_empty():
    with patch("app.services.ai_agent.asyncio.to_thread", new=AsyncMock(return_value=[])):
        out = await _web_search("q")
    assert "No results" in out


@pytest.mark.asyncio
async def test_web_search_returns_unavailable_on_failure():
    with patch(
        "app.services.ai_agent.asyncio.to_thread",
        new=AsyncMock(side_effect=RuntimeError("ddg down")),
    ):
        out = await _web_search("q")
    assert "unavailable" in out.lower()


# ── _fetch_url ────────────────────────────────────────────────────────────────


def _fake_httpx_client(
    html: str | None = None,
    *,
    raise_on_get: Exception | None = None,
    final_url: str = "https://x.test",
):
    """Build a context-managed httpx.AsyncClient mock that returns *html*."""
    response = MagicMock()
    response.text = html or ""
    response.url = final_url
    response.raise_for_status = MagicMock()
    client = MagicMock()
    if raise_on_get is not None:
        client.get = AsyncMock(side_effect=raise_on_get)
    else:
        client.get = AsyncMock(return_value=response)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


@pytest.mark.asyncio
async def test_fetch_url_strips_html_tags():
    html = "<html><body><h1>Concerts</h1><p>Belgrade May 2026</p></body></html>"
    with patch("httpx.AsyncClient", return_value=_fake_httpx_client(html)):
        out = await _fetch_url("https://bandsintown.com/c/belgrade-rs")
    assert "Concerts" in out and "Belgrade May 2026" in out
    assert "<" not in out and ">" not in out


@pytest.mark.asyncio
async def test_fetch_url_drops_script_and_style():
    html = (
        "<html><head><style>.x{}</style><script>alert(1)</script></head>"
        "<body>real content</body></html>"
    )
    with patch("httpx.AsyncClient", return_value=_fake_httpx_client(html)):
        out = await _fetch_url("https://x.test")
    assert "alert" not in out and ".x{" not in out
    assert "real content" in out


@pytest.mark.asyncio
async def test_fetch_url_truncates_long_text():
    html = "<p>" + ("X" * 10000) + "</p>"
    with patch("httpx.AsyncClient", return_value=_fake_httpx_client(html)):
        out = await _fetch_url("https://x.test")
    assert "[truncated]" in out
    assert len(out) < 4000


@pytest.mark.asyncio
async def test_fetch_url_rejects_non_http_url():
    out = await _fetch_url("file:///etc/passwd")
    assert out.startswith("Error: invalid URL")


@pytest.mark.asyncio
async def test_fetch_url_handles_network_error():
    with patch(
        "httpx.AsyncClient",
        return_value=_fake_httpx_client(raise_on_get=ConnectionError("dns")),
    ):
        out = await _fetch_url("https://x.test")
    assert out.startswith("Error: could not fetch")


@pytest.mark.asyncio
async def test_fetch_url_preserves_anchor_links_in_brackets():
    """Anchors should survive tag-strip as 'text [URL]'."""
    html = (
        "<html><body>"
        '<a href="https://example.com/event/42">Billy Idol — Belgrade</a>'
        " on July 9.</body></html>"
    )
    with patch("httpx.AsyncClient", return_value=_fake_httpx_client(html)):
        out = await _fetch_url("https://example.com/listing")
    assert "Billy Idol — Belgrade" in out
    assert "[https://example.com/event/42]" in out


@pytest.mark.asyncio
async def test_fetch_url_absolutizes_relative_links():
    """Relative hrefs are joined against the page URL."""
    html = '<a href="/concerts/123">Show</a>'
    with patch(
        "httpx.AsyncClient",
        return_value=_fake_httpx_client(html, final_url="https://bandsintown.com/c/belgrade"),
    ):
        out = await _fetch_url("https://bandsintown.com/c/belgrade")
    assert "[https://bandsintown.com/concerts/123]" in out


@pytest.mark.asyncio
async def test_fetch_url_returns_empty_marker_when_page_is_blank():
    with patch("httpx.AsyncClient", return_value=_fake_httpx_client("")):
        out = await _fetch_url("https://x.test")
    assert out == "(empty page)"


@pytest.mark.asyncio
async def test_execute_tool_dispatches_fetch_url():
    agent = AIAgent()
    with patch("app.services.ai_agent._fetch_url", new=AsyncMock(return_value="page text")) as fu:
        out = await agent._execute_tool(
            user_id=1,
            name="fetch_url",
            args={"url": "https://x.test"},
            image_holder=[],
            pending_holder=[],
        )
    assert out == "page text"
    fu.assert_awaited_once_with("https://x.test")


@pytest.mark.asyncio
async def test_find_event_image_returns_first_url():
    with patch(
        "app.services.ai_agent.asyncio.to_thread",
        new=AsyncMock(return_value=[{"image": "http://img/1"}]),
    ):
        url = await _find_event_image("q")
    assert url == "http://img/1"


@pytest.mark.asyncio
async def test_find_event_image_returns_none_when_no_results():
    with patch("app.services.ai_agent.asyncio.to_thread", new=AsyncMock(return_value=[])):
        url = await _find_event_image("q")
    assert url is None


@pytest.mark.asyncio
async def test_find_event_image_swallows_exception():
    with patch(
        "app.services.ai_agent.asyncio.to_thread",
        new=AsyncMock(side_effect=RuntimeError("ddg")),
    ):
        url = await _find_event_image("q")
    assert url is None


# ── _get_client ───────────────────────────────────────────────────────────────


def test_get_client_raises_when_key_missing():
    agent = AIAgent()
    with patch("app.services.ai_agent.settings") as s:
        s.openai_api_key = ""
        with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
            agent._get_client()


def test_get_client_caches_instance():
    agent = AIAgent()
    with patch("app.services.ai_agent.settings") as s:
        s.openai_api_key = "sk-test"
        c1 = agent._get_client()
        c2 = agent._get_client()
    assert c1 is c2


# ── _execute_tool branches ────────────────────────────────────────────────────


@pytest.fixture
def fresh_agent():
    return AIAgent()


@pytest.mark.asyncio
async def test_execute_tool_get_date_range_returns_iso_bounds(fresh_agent):
    with patch("app.services.ai_agent.auth_service") as auth:
        auth.get_user_timezone = AsyncMock(return_value="Europe/Belgrade")
        out = await fresh_agent._execute_tool(
            user_id=1,
            name="get_date_range",
            args={"period": "this_weekend"},
            image_holder=[],
            pending_holder=[],
        )
    payload = json.loads(out)
    assert "time_min" in payload and "time_max" in payload


@pytest.mark.asyncio
async def test_execute_tool_get_date_range_invalid_period(fresh_agent):
    with patch("app.services.ai_agent.auth_service") as auth:
        auth.get_user_timezone = AsyncMock(return_value="Europe/Belgrade")
        out = await fresh_agent._execute_tool(
            user_id=1, name="get_date_range", args={}, image_holder=[], pending_holder=[]
        )
    assert out.lower().startswith("error")


@pytest.mark.asyncio
async def test_execute_tool_web_search_proxies(fresh_agent):
    with patch("app.services.ai_agent._web_search", new=AsyncMock(return_value="results")) as ws:
        out = await fresh_agent._execute_tool(
            user_id=1, name="web_search", args={"query": "q"}, image_holder=[], pending_holder=[]
        )
    assert out == "results"
    ws.assert_awaited_once()


@pytest.mark.asyncio
async def test_execute_tool_find_event_image_appends_url(fresh_agent):
    holder: list[str] = []
    with patch(
        "app.services.ai_agent._find_event_image",
        new=AsyncMock(return_value="http://img/x"),
    ):
        out = await fresh_agent._execute_tool(
            user_id=1,
            name="find_event_image",
            args={"query": "q"},
            image_holder=holder,
            pending_holder=[],
        )
    assert "Image found" in out
    assert holder == ["http://img/x"]


def test_extract_candidate_urls_keeps_whitelisted_event_sites():
    web_search_text = (
        "Hood Vibes Xzibit\n"
        "https://new.gigstix.com/event/hood-vibes-special-xzibit-beograd-29-maj-2026/\n"
        "Snippet about Xzibit show\n\n"
        "AllEvents.in Belgrade\n"
        "https://allevents.in/belgrade/concerts\n"
        "Concerts in Belgrade\n\n"
        "Random wikipedia entry\n"
        "https://en.wikipedia.org/wiki/Xzibit\n"
        "Biography\n\n"
        "Bandsintown (blocked from fetch)\n"
        "https://www.bandsintown.com/c/belgrade-rs\n"
        "Should not be in candidates\n"
    )
    urls = _extract_candidate_urls(web_search_text)
    assert "https://new.gigstix.com/event/hood-vibes-special-xzibit-beograd-29-maj-2026/" in urls
    assert "https://allevents.in/belgrade/concerts" in urls
    assert all("wikipedia" not in u for u in urls)
    # Bandsintown 403s our prod IP — excluded from the whitelist on purpose.
    assert all("bandsintown" not in u for u in urls)


def test_extract_candidate_urls_returns_empty_on_no_matches():
    assert _extract_candidate_urls("just some text\nhttps://example.com\nfoo") == []


def test_interleave_by_host_round_robins_dominant_source():
    """When one host (e.g. bandsintown) dominates candidates, interleaving
    must give smaller sources a slot in the top-N so they aren't starved."""
    urls = [
        "https://www.bandsintown.com/e/1",
        "https://www.bandsintown.com/e/2",
        "https://www.bandsintown.com/e/3",
        "https://www.bandsintown.com/e/4",
        "https://www.bandsintown.com/e/5",
        "https://new.gigstix.com/event/a",
        "https://www.songkick.com/concerts/x",
        "https://ra.co/events/9",
    ]
    out = _interleave_by_host(urls)
    # First 4 must cover all 4 distinct hosts before bandsintown takes another slot.
    first4_hosts = {host_for(u) for u in out[:4]}
    assert first4_hosts == {"bandsintown.com", "gigstix.com", "songkick.com", "ra.co"}
    # The full set is preserved.
    assert sorted(out) == sorted(urls)


def host_for(url: str) -> str:
    from urllib.parse import urlparse

    h = urlparse(url).netloc.lower()
    return h.removeprefix("www.").removeprefix("new.")


def test_find_fabricated_event_urls_flags_unseen_event_urls():
    """URLs to known event hosts that weren't in any tool output are
    flagged. This is the exact failure mode we observed in prod: the
    model invented last.fm event URLs for artists it remembered from
    training, and those URLs resolved to events in other cities."""
    response = (
        "Here are concerts:\n"
        "• Blitzkid <a href='https://www.last.fm/event/5032702-blitzkid'>tickets</a>\n"
        "• Real Show <a href='https://allevents.in/belgrade/real-show'>tickets</a>\n"
    )
    tool_output = (
        "...Real Show on June 5...\n[https://allevents.in/belgrade/real-show]\n...other events...\n"
    )
    fabricated = _find_fabricated_event_urls(response, tool_output)
    assert fabricated == ["https://www.last.fm/event/5032702-blitzkid"]


def test_find_fabricated_event_urls_ignores_non_event_urls():
    """Non-event URLs (wikipedia, t.me, etc.) shouldn't be policed —
    they may legitimately appear in the model's reply without being in
    a tool output."""
    response = "See <a href='https://en.wikipedia.org/wiki/Xzibit'>wiki</a>."
    tool_output = "no urls here"
    assert _find_fabricated_event_urls(response, tool_output) == []


def test_find_fabricated_event_urls_strips_trailing_punctuation():
    """Inline links often end with sentence punctuation; we must match
    against the bare URL, not the URL+'.'."""
    response = "More info at https://allevents.in/belgrade/show-123."
    tool_output = "...event link [https://allevents.in/belgrade/show-123]..."
    assert _find_fabricated_event_urls(response, tool_output) == []


@pytest.mark.asyncio
async def test_discover_local_events_runs_pipeline_and_concatenates(fresh_agent):
    """Pipeline runs searches in parallel, dedupes URLs, fetches matches,
    and returns one blob with each fetched page prefixed by its URL."""

    async def fake_search(query: str, max_results: int = 5) -> str:
        if "gigstix" in query:
            return "Xzibit Belgrade\nhttps://new.gigstix.com/event/hood-vibes-xzibit/\nMay 29\n"
        if "allevents.in" in query and "metal" in query:
            return "Metal night\nhttps://allevents.in/belgrade/metal-night-123\nSome metal show\n"
        return "no relevant hits\nhttps://example.com\njunk"

    async def fake_fetch(url: str) -> str:
        return f"<page content for {url}>"

    with (
        patch("app.services.ai_agent._web_search", new=AsyncMock(side_effect=fake_search)),
        patch("app.services.ai_agent._fetch_url", new=AsyncMock(side_effect=fake_fetch)),
    ):
        out = await _discover_local_events(
            city="Belgrade",
            tz_name="Europe/Belgrade",
            date_min="2026-05-25",
            date_max="2026-06-01",
        )

    assert "Belgrade, 2026-05-25 to 2026-06-01" in out
    # Search-discovered URLs from whitelisted hosts are fetched.
    assert "https://new.gigstix.com/event/hood-vibes-xzibit/" in out
    assert "https://allevents.in/belgrade/metal-night-123" in out
    assert "<page content for https://new.gigstix.com/event/hood-vibes-xzibit/>" in out
    # Direct-fetch listing URLs for Belgrade are always probed.
    assert "https://allevents.in/belgrade/all" in out
    assert "https://www.last.fm/events?location=Belgrade" in out


@pytest.mark.asyncio
async def test_discover_local_events_still_probes_direct_urls_when_search_empty():
    """When search yields no whitelisted URLs, the city-specific direct
    listing URLs (AllEvents.in, Last.fm) are still fetched so the model
    never gets back a literal 'nothing found' for known cities."""

    async def fake_search(query: str, max_results: int = 5) -> str:
        return "junk\nhttps://example.com\nno event sites here"

    async def fake_fetch(url: str) -> str:
        return f"<fetched {url}>"

    with (
        patch("app.services.ai_agent._web_search", new=AsyncMock(side_effect=fake_search)),
        patch("app.services.ai_agent._fetch_url", new=AsyncMock(side_effect=fake_fetch)),
    ):
        out = await _discover_local_events(
            city="Belgrade",
            tz_name="Europe/Belgrade",
            date_min="2026-05-25",
            date_max="2026-06-01",
        )
    assert "https://allevents.in/belgrade/all" in out
    assert "https://www.last.fm/events?location=Belgrade" in out


@pytest.mark.asyncio
async def test_execute_tool_dispatches_discover_local_events(fresh_agent):
    with (
        patch("app.services.ai_agent.auth_service") as auth,
        patch(
            "app.services.ai_agent._discover_local_events",
            new=AsyncMock(return_value="discovery results"),
        ) as discover,
    ):
        auth.get_user_timezone = AsyncMock(return_value="Europe/Belgrade")
        out = await fresh_agent._execute_tool(
            user_id=1,
            name="discover_local_events",
            args={
                "date_min": "2026-05-25",
                "date_max": "2026-06-01",
                "genres": ["rock", "metal"],
            },
            image_holder=[],
            pending_holder=[],
        )
    assert out == "discovery results"
    discover.assert_awaited_once()
    assert discover.await_args is not None
    kwargs = discover.await_args.kwargs
    assert kwargs["city"] == "Belgrade"
    assert kwargs["tz_name"] == "Europe/Belgrade"
    assert kwargs["genres"] == ["rock", "metal"]


@pytest.mark.asyncio
async def test_execute_tool_find_event_image_no_image(fresh_agent):
    with patch(
        "app.services.ai_agent._find_event_image",
        new=AsyncMock(return_value=None),
    ):
        out = await fresh_agent._execute_tool(
            user_id=1,
            name="find_event_image",
            args={"query": "q"},
            image_holder=[],
            pending_holder=[],
        )
    assert "No suitable image" in out


# ── _run_calendar_tool happy paths + errors ───────────────────────────────────


@pytest.mark.asyncio
async def test_run_calendar_tool_no_credentials_returns_error(fresh_agent):
    with patch("app.services.ai_agent.auth_service") as auth:
        auth.get_credentials = AsyncMock(return_value=None)
        out = await fresh_agent._run_calendar_tool(1, "read_events", {})
    assert "/auth" in out


@pytest.mark.asyncio
async def test_run_calendar_tool_read_events_no_results(fresh_agent):
    with (
        patch("app.services.ai_agent.auth_service") as auth,
        patch("app.services.ai_agent.calendar_service") as cal,
    ):
        auth.get_credentials = AsyncMock(return_value=object())
        auth.get_calendar_id = AsyncMock(return_value="primary")
        auth.get_user_timezone = AsyncMock(return_value="Europe/Belgrade")
        cal.list_events = AsyncMock(return_value=[])
        out = await fresh_agent._run_calendar_tool(1, "read_events", {})
    assert "No upcoming" in out


@pytest.mark.asyncio
async def test_run_calendar_tool_read_events_formats_list(fresh_agent):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app.services.calendar_service import EventRead

    tz = ZoneInfo("Europe/Belgrade")
    with (
        patch("app.services.ai_agent.auth_service") as auth,
        patch("app.services.ai_agent.calendar_service") as cal,
    ):
        auth.get_credentials = AsyncMock(return_value=object())
        auth.get_calendar_id = AsyncMock(return_value="primary")
        auth.get_user_timezone = AsyncMock(return_value="Europe/Belgrade")
        cal.list_events = AsyncMock(
            return_value=[
                EventRead(
                    event_id="e1",
                    summary="Meet",
                    start=datetime(2099, 1, 1, 10, tzinfo=tz),
                    end=datetime(2099, 1, 1, 11, tzinfo=tz),
                    location="Office",
                )
            ]
        )
        out = await fresh_agent._run_calendar_tool(1, "read_events", {})
    assert "ID:e1" in out and "Meet" in out and "Office" in out


@pytest.mark.asyncio
async def test_run_calendar_tool_create_event_overnight_fix(fresh_agent):
    """end_time <= start_time → end gets +1 day."""
    from app.services.calendar_service import EventRead

    captured: dict = {}

    async def fake_create(_creds, ev, calendar_id):
        captured["start"] = ev.start
        captured["end"] = ev.end
        return EventRead(
            event_id="new",
            summary=ev.summary,
            start=ev.start,
            end=ev.end,
        )

    with (
        patch("app.services.ai_agent.auth_service") as auth,
        patch("app.services.ai_agent.calendar_service") as cal,
    ):
        auth.get_credentials = AsyncMock(return_value=object())
        auth.get_calendar_id = AsyncMock(return_value="primary")
        auth.get_user_timezone = AsyncMock(return_value="Europe/Belgrade")
        cal.create_event = AsyncMock(side_effect=fake_create)
        out = await fresh_agent._run_calendar_tool(
            1,
            "create_event",
            {
                "summary": "Late event",
                "start_time": "2099-01-01T20:00:00",
                "end_time": "2099-01-01T00:00:00",  # next-day end implied
            },
        )
    assert out.startswith("Created:")
    assert captured["end"] > captured["start"]


@pytest.mark.asyncio
async def test_run_calendar_tool_update_event_passes_through(fresh_agent):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app.services.calendar_service import EventRead

    tz = ZoneInfo("Europe/Belgrade")
    with (
        patch("app.services.ai_agent.auth_service") as auth,
        patch("app.services.ai_agent.calendar_service") as cal,
    ):
        auth.get_credentials = AsyncMock(return_value=object())
        auth.get_calendar_id = AsyncMock(return_value="primary")
        auth.get_user_timezone = AsyncMock(return_value="Europe/Belgrade")
        cal.update_event = AsyncMock(
            return_value=EventRead(
                event_id="e1",
                summary="Updated",
                start=datetime(2099, 1, 1, tzinfo=tz),
                end=datetime(2099, 1, 1, 1, tzinfo=tz),
            )
        )
        out = await fresh_agent._run_calendar_tool(
            1, "update_event", {"event_id": "e1", "summary": "Updated"}
        )
    assert "Updated" in out


@pytest.mark.asyncio
async def test_run_calendar_tool_delete_event(fresh_agent):
    with (
        patch("app.services.ai_agent.auth_service") as auth,
        patch("app.services.ai_agent.calendar_service") as cal,
    ):
        auth.get_credentials = AsyncMock(return_value=object())
        auth.get_calendar_id = AsyncMock(return_value="primary")
        auth.get_user_timezone = AsyncMock(return_value="Europe/Belgrade")
        cal.delete_event = AsyncMock()
        out = await fresh_agent._run_calendar_tool(1, "delete_event", {"event_id": "e1"})
    assert "Deleted" in out and "e1" in out


@pytest.mark.asyncio
async def test_run_calendar_tool_unknown_name_returns_message(fresh_agent):
    with patch("app.services.ai_agent.auth_service") as auth:
        auth.get_credentials = AsyncMock(return_value=object())
        auth.get_calendar_id = AsyncMock(return_value="primary")
        auth.get_user_timezone = AsyncMock(return_value="Europe/Belgrade")
        out = await fresh_agent._run_calendar_tool(1, "no_such_tool", {})
    assert "Unknown tool" in out


@pytest.mark.asyncio
async def test_run_calendar_tool_refresh_error_revokes_tokens(fresh_agent):
    with (
        patch("app.services.ai_agent.auth_service") as auth,
        patch("app.services.ai_agent.calendar_service") as cal,
    ):
        auth.get_credentials = AsyncMock(return_value=object())
        auth.get_calendar_id = AsyncMock(return_value="primary")
        auth.get_user_timezone = AsyncMock(return_value="Europe/Belgrade")
        auth.revoke_tokens = AsyncMock()
        cal.list_events = AsyncMock(side_effect=RefreshError("revoked"))
        out = await fresh_agent._run_calendar_tool(1, "read_events", {})
    auth.revoke_tokens.assert_awaited_once_with(1)
    assert "revoked" in out.lower() or "expired" in out.lower()


@pytest.mark.asyncio
async def test_run_calendar_tool_runtime_404_returns_friendly_msg(fresh_agent):
    with (
        patch("app.services.ai_agent.auth_service") as auth,
        patch("app.services.ai_agent.calendar_service") as cal,
    ):
        auth.get_credentials = AsyncMock(return_value=object())
        auth.get_calendar_id = AsyncMock(return_value="primary")
        auth.get_user_timezone = AsyncMock(return_value="Europe/Belgrade")
        cal.delete_event = AsyncMock(side_effect=RuntimeError("Calendar 404"))
        out = await fresh_agent._run_calendar_tool(1, "delete_event", {"event_id": "x"})
    assert "not found" in out.lower()


@pytest.mark.asyncio
async def test_run_calendar_tool_other_runtime_error_propagates_text(fresh_agent):
    with (
        patch("app.services.ai_agent.auth_service") as auth,
        patch("app.services.ai_agent.calendar_service") as cal,
    ):
        auth.get_credentials = AsyncMock(return_value=object())
        auth.get_calendar_id = AsyncMock(return_value="primary")
        auth.get_user_timezone = AsyncMock(return_value="Europe/Belgrade")
        cal.list_events = AsyncMock(side_effect=RuntimeError("500 oops"))
        out = await fresh_agent._run_calendar_tool(1, "read_events", {})
    assert "Error in" in out


# ── execute_confirmed_action ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_confirmed_action_404_returns_user_friendly_ru(fresh_agent):
    """Confirm flow remaps "404" RuntimeError to a Russian "event not found" string."""
    with patch.object(fresh_agent, "_run_calendar_tool", side_effect=RuntimeError("404 missing")):
        out = await fresh_agent.execute_confirmed_action(1, "delete_event", {"event_id": "x"})
    assert "не найдено" in out.lower()


@pytest.mark.asyncio
async def test_execute_confirmed_action_other_error_reraises(fresh_agent):
    with (
        patch.object(fresh_agent, "_run_calendar_tool", side_effect=RuntimeError("500 server")),
        pytest.raises(RuntimeError, match="500"),
    ):
        await fresh_agent.execute_confirmed_action(1, "delete_event", {"event_id": "x"})


# ── process_message ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_process_message_returns_error_when_no_api_key(fresh_agent):
    with patch("app.services.ai_agent.settings") as s:
        s.openai_api_key = ""
        result = await fresh_agent.process_message(1, "hi")
    assert "OPENAI_API_KEY" in result.text


def _fake_completion(text: str, tool_calls=None, finish_reason="stop"):
    """Build a MagicMock that quacks like an OpenAI ChatCompletion."""
    msg = MagicMock()
    msg.content = text
    msg.tool_calls = tool_calls or []
    msg.model_dump.return_value = {"role": "assistant", "content": text}
    choice = MagicMock(message=msg, finish_reason=finish_reason)
    return MagicMock(choices=[choice])


@pytest.mark.asyncio
async def test_process_message_simple_response_recorded_in_history(fresh_agent):
    fake_resp = _fake_completion("Hello!")
    fake_client = MagicMock()
    fake_client.chat.completions.create = AsyncMock(return_value=fake_resp)
    with (
        patch.object(fresh_agent, "_get_client", return_value=fake_client),
        patch("app.services.ai_agent.auth_service") as auth,
    ):
        auth.get_user_timezone = AsyncMock(return_value="Europe/Belgrade")
        result = await fresh_agent.process_message(42, "hi")
    assert result.text == "Hello!"
    hist = list(fresh_agent._get_history(42))
    assert any(m.get("content") == "hi" for m in hist)
    assert any(m.get("content") == "Hello!" for m in hist)


@pytest.mark.asyncio
async def test_process_message_runs_one_tool_call(fresh_agent):
    """First completion → tool_calls; second → final text."""
    from openai.types.chat import ChatCompletionMessageToolCall

    tool_call = ChatCompletionMessageToolCall.model_validate(
        {
            "id": "call1",
            "type": "function",
            "function": {"name": "web_search", "arguments": json.dumps({"query": "q"})},
        }
    )
    first = _fake_completion("", tool_calls=[tool_call], finish_reason="tool_calls")
    second = _fake_completion("Final answer")
    fake_client = MagicMock()
    fake_client.chat.completions.create = AsyncMock(side_effect=[first, second])

    with (
        patch.object(fresh_agent, "_get_client", return_value=fake_client),
        patch("app.services.ai_agent.auth_service") as auth,
        patch("app.services.ai_agent._web_search", new=AsyncMock(return_value="search-result")),
    ):
        auth.get_user_timezone = AsyncMock(return_value="Europe/Belgrade")
        result = await fresh_agent.process_message(7, "what's on?")
    assert result.text == "Final answer"
    assert fake_client.chat.completions.create.await_count == 2


@pytest.mark.asyncio
async def test_process_message_returns_friendly_error_on_openai_failure(fresh_agent):
    fake_client = MagicMock()
    fake_client.chat.completions.create = AsyncMock(side_effect=RuntimeError("api down"))
    with (
        patch.object(fresh_agent, "_get_client", return_value=fake_client),
        patch("app.services.ai_agent.auth_service") as auth,
    ):
        auth.get_user_timezone = AsyncMock(return_value="Europe/Belgrade")
        result = await fresh_agent.process_message(1, "hi")
    assert "couldn't reach" in result.text.lower()


@pytest.mark.asyncio
async def test_process_message_invalid_tool_args_falls_back_to_empty_dict(fresh_agent):
    """Malformed JSON in tool args → handler is still invoked with {}."""
    from openai.types.chat import ChatCompletionMessageToolCall

    bad_call = ChatCompletionMessageToolCall.model_validate(
        {
            "id": "c1",
            "type": "function",
            "function": {"name": "web_search", "arguments": "{ this is not json"},
        }
    )
    first = _fake_completion("", tool_calls=[bad_call], finish_reason="tool_calls")
    second = _fake_completion("done")
    fake_client = MagicMock()
    fake_client.chat.completions.create = AsyncMock(side_effect=[first, second])
    with (
        patch.object(fresh_agent, "_get_client", return_value=fake_client),
        patch("app.services.ai_agent.auth_service") as auth,
        patch("app.services.ai_agent._web_search", new=AsyncMock(return_value="r")) as ws,
    ):
        auth.get_user_timezone = AsyncMock(return_value="Europe/Belgrade")
        await fresh_agent.process_message(2, "go")
    # First positional arg to _web_search is `query=""` — never crashed
    ws.assert_awaited()


def test_singleton_instance():
    from app.services.ai_agent import ai_agent as exported

    assert exported is ai_agent
    _ = SimpleNamespace  # silence unused-import lint
