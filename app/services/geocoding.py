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


async def reverse_geocode_city(lat: float, lon: float) -> str | None:
    """Return a city-level place name for *lat*/*lon*, or ``None`` on failure.

    Failure modes (network blip, Nominatim rate-limit, missing locality in
    the response) all return ``None`` so callers can fall back to the
    timezone-derived city.
    """
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                _NOMINATIM_URL,
                params={
                    "lat": lat,
                    "lon": lon,
                    "format": "jsonv2",
                    "zoom": 10,
                    "addressdetails": 1,
                },
                headers={"User-Agent": _USER_AGENT, "Accept-Language": "en"},
            )
            resp.raise_for_status()
            payload = resp.json()
    except Exception as exc:
        logger.warning(
            "reverse_geocode_city failed for %.4f,%.4f: %s: %s",
            lat,
            lon,
            type(exc).__name__,
            exc,
        )
        return None
    return _pick_locality(payload.get("address", {}))
