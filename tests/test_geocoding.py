"""Tests for the reverse-geocoding helper."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.geocoding import _pick_locality, reverse_geocode_city

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
