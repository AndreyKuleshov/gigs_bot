"""Smoke tests for the FastAPI app factory."""

from fastapi import FastAPI

from app.api.app import create_app


def test_create_app_returns_fastapi_instance():
    app = create_app()
    assert isinstance(app, FastAPI)


def test_routes_registered():
    app = create_app()
    paths = {r.path for r in app.routes}  # type: ignore[attr-defined]
    assert "/health" in paths
    assert "/webhook/telegram" in paths
    # Auth + events routers contribute these
    assert "/auth/google" in paths
    assert "/auth/google/callback" in paths


def test_health_route_registered_exactly_once():
    """Lifespan can't run cleanly in tests (it would touch Telegram + DB), so
    we verify the route declaration without invoking the request handler."""
    app = create_app()
    health_routes = [r for r in app.routes if getattr(r, "path", None) == "/health"]
    assert len(health_routes) == 1
