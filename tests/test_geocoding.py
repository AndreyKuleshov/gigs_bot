"""Tests for the reverse-geocoding helper."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.geocoding import (
    _looks_like_subunit,
    _pick_locality,
    reverse_geocode_city,
    reverse_geocode_locality,
)

# ── _pick_locality (pure function, no I/O) ────────────────────────────────────


def test_pick_locality_prefers_city():
    addr = {"city": "Belgrade", "town": "Some Town", "village": "X"}
    assert _pick_locality(addr) == "Belgrade"


def test_pick_locality_falls_back_to_town():
    addr = {"town": "Smaller Town", "village": "v"}
    assert _pick_locality(addr) == "Smaller Town"


def test_pick_locality_falls_back_through_hierarchy():
    addr = {"county": "Some County"}
    assert _pick_locality(addr) == "Some County"


def test_pick_locality_returns_none_when_no_locality():
    assert _pick_locality({}) is None
    assert _pick_locality({"country": "X", "country_code": "x"}) is None


# ── reverse_geocode_city (mock httpx) ────────────────────────────────────────


def _fake_async_client(json_payload, status: int = 200):
    """Build a context-manager that yields a fake httpx AsyncClient."""
    response = MagicMock()
    response.status_code = status
    response.raise_for_status = MagicMock()
    if status >= 400:
        import httpx

        response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "err", request=MagicMock(), response=response
        )
    response.json.return_value = json_payload

    client = MagicMock()
    client.get = AsyncMock(return_value=response)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


@pytest.mark.asyncio
async def test_reverse_geocode_returns_city_from_payload():
    payload = {"address": {"city": "Belgrade", "country": "Serbia"}}
    with patch("httpx.AsyncClient", return_value=_fake_async_client(payload)):
        out = await reverse_geocode_city(44.81, 20.46)
    assert out == "Belgrade"


@pytest.mark.asyncio
async def test_reverse_geocode_returns_town_when_no_city():
    payload = {"address": {"town": "Niška Banja"}}
    with patch("httpx.AsyncClient", return_value=_fake_async_client(payload)):
        out = await reverse_geocode_city(43.27, 22.0)
    assert out == "Niška Banja"


@pytest.mark.asyncio
async def test_reverse_geocode_returns_none_on_http_error():
    with patch("httpx.AsyncClient", return_value=_fake_async_client({}, status=500)):
        out = await reverse_geocode_city(0, 0)
    assert out is None


@pytest.mark.asyncio
async def test_reverse_geocode_returns_none_when_address_missing():
    with patch("httpx.AsyncClient", return_value=_fake_async_client({"licence": "OSM"})):
        out = await reverse_geocode_city(0, 0)
    assert out is None


@pytest.mark.asyncio
async def test_reverse_geocode_swallows_network_error():
    """Any exception from httpx → None, never raise."""
    bad_client = MagicMock()
    bad_client.__aenter__ = AsyncMock(side_effect=ConnectionError("dns down"))
    bad_client.__aexit__ = AsyncMock(return_value=None)
    with patch("httpx.AsyncClient", return_value=bad_client):
        out = await reverse_geocode_city(0, 0)
    assert out is None


# ── _looks_like_subunit + zoom-fallback ──────────────────────────────────────


def test_looks_like_subunit_district_and_municipality():
    assert _looks_like_subunit("Stari Grad Urban Municipality") is True
    assert _looks_like_subunit("Brooklyn Borough") is True
    assert _looks_like_subunit("Belgrade") is False
    assert _looks_like_subunit("Москва") is False


@pytest.mark.asyncio
async def test_locality_falls_back_to_wider_zoom_when_subunit():
    """First lookup returns "Stari Grad Urban Municipality" — helper retries
    and gets "Belgrade" from the wider zoom."""
    payload_zoom10 = {
        "address": {
            "city": "Stari Grad Urban Municipality",
            "country": "Serbia",
        }
    }
    payload_zoom8 = {"address": {"city": "Belgrade", "country": "Serbia"}}

    call_log: list[int] = []

    def make_client(payload):
        return _fake_async_client(payload)

    def httpx_factory(*args, **kwargs):
        # Match the order of get() invocations through call_log.
        # The geocode helper opens AsyncClient twice: zoom=10, then zoom=8.
        if not call_log:
            call_log.append(10)
            return make_client(payload_zoom10)
        call_log.append(8)
        return make_client(payload_zoom8)

    with patch("httpx.AsyncClient", side_effect=httpx_factory):
        locality, country = await reverse_geocode_locality(44.81, 20.46)
    assert locality == "Belgrade"
    assert country == "Serbia"
    assert call_log == [10, 8]


@pytest.mark.asyncio
async def test_locality_does_not_retry_when_first_pick_is_clean():
    """If zoom=10 already returns a real city, no second call is made."""
    payload = {"address": {"city": "Belgrade", "country": "Serbia"}}
    call_count = {"n": 0}

    def httpx_factory(*args, **kwargs):
        call_count["n"] += 1
        return _fake_async_client(payload)

    with patch("httpx.AsyncClient", side_effect=httpx_factory):
        locality, country = await reverse_geocode_locality(44.81, 20.46)
    assert locality == "Belgrade"
    assert country == "Serbia"
    assert call_count["n"] == 1


@pytest.mark.asyncio
async def test_locality_returns_subunit_when_wider_zoom_also_subunit():
    """If both zooms return sub-unit names, keep the first one (best we got)."""
    payload = {
        "address": {
            "city": "Some Urban Municipality",
            "country": "Serbia",
        }
    }
    with patch("httpx.AsyncClient", return_value=_fake_async_client(payload)):
        locality, country = await reverse_geocode_locality(0, 0)
    assert locality == "Some Urban Municipality"
    assert country == "Serbia"


@pytest.mark.asyncio
async def test_locality_handles_first_call_failure():
    """zoom=10 returns nothing → locality is None, no retry attempted."""
    bad_client = MagicMock()
    bad_client.__aenter__ = AsyncMock(side_effect=ConnectionError("net"))
    bad_client.__aexit__ = AsyncMock(return_value=None)
    with patch("httpx.AsyncClient", return_value=bad_client):
        locality, country = await reverse_geocode_locality(0, 0)
    assert locality is None
    assert country is None
