"""Reverse geocoding via OpenStreetMap Nominatim.

Used by the /events flow to translate a Telegram-shared location into a
human-readable city name that the AI agent / web search can use.

Nominatim is free but requires a meaningful User-Agent and rate-limits to
1 req/sec. For our use case (one call per user-initiated /events flow)
that's well within budget.
"""

import logging

import httpx

logger = logging.getLogger(__name__)

_NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
_USER_AGENT = "gigs-bot/0.1 (https://github.com/AndreyKuleshov/gigs_bot)"
_TIMEOUT = httpx.Timeout(10.0)


# A "district / municipality / urban area" is usually a sub-unit of a real
# city (e.g. "Stari Grad Urban Municipality" is the centre of Belgrade).
# When pick returns one of those, retry the lookup with a wider zoom so
# Nominatim aggregates up to the city level.
_DISTRICT_MARKERS = ("district", "municipality", "urban area", "borough")


def _pick_locality(addr: dict) -> str | None:
    """Choose the best human-readable place name from a Nominatim address dict.

    Order matches Nominatim's typical hierarchy: city → town → village →
    municipality → county → state. For event search the bigger the cluster
    the better, so we prefer city/town over neighbourhood-level names.
    """
    for key in ("city", "town", "village", "municipality", "county", "state"):
        val = addr.get(key)
        if val:
            return val
    return None


def _looks_like_subunit(name: str) -> bool:
    lowered = name.lower()
    return any(marker in lowered for marker in _DISTRICT_MARKERS)


async def _query_nominatim(lat: float, lon: float, zoom: int) -> dict | None:
    """One Nominatim reverse call. Returns the raw `address` dict or None."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                _NOMINATIM_URL,
                params={
                    "lat": lat,
                    "lon": lon,
                    "format": "jsonv2",
                    "zoom": zoom,
                    "addressdetails": 1,
                },
                headers={"User-Agent": _USER_AGENT, "Accept-Language": "en"},
            )
            resp.raise_for_status()
            return resp.json().get("address", {})
    except Exception as exc:
        logger.warning(
            "Nominatim reverse failed (zoom=%d) for %.4f,%.4f: %s: %s",
            zoom,
            lat,
            lon,
            type(exc).__name__,
            exc,
        )
        return None


async def reverse_geocode_locality(lat: float, lon: float) -> tuple[str | None, str | None]:
    """Return (locality, country) for *lat*/*lon*. Either or both can be None.

    Strategy: zoom=10 first; if the locality looks like a sub-unit of a
    bigger city ("Stari Grad Urban Municipality" et al.), retry with
    zoom=8 to aggregate up. The country field comes from whichever call
    succeeded last.
    """
    addr = await _query_nominatim(lat, lon, zoom=10)
    if addr is None:
        return None, None
    locality = _pick_locality(addr)
    country = addr.get("country")

    if locality and _looks_like_subunit(locality):
        wider = await _query_nominatim(lat, lon, zoom=8)
        if wider is not None:
            wider_locality = _pick_locality(wider)
            if wider_locality and not _looks_like_subunit(wider_locality):
                locality = wider_locality
                country = wider.get("country") or country
    return locality, country


async def reverse_geocode_city(lat: float, lon: float) -> str | None:
    """Backwards-compatible wrapper: just return the city name."""
    locality, _ = await reverse_geocode_locality(lat, lon)
    return locality
