"""FastAPI application factory."""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core.config import settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    from app.cache.redis_client import close_redis
    from app.db.base import close_engine, create_tables

    await create_tables()
    yield
    # Shutdown
    await close_engine()
    await close_redis()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Gigs Bot API",
        description="Backend for the Telegram Google Calendar bot",
        version="0.1.0",
        lifespan=lifespan,
        debug=settings.debug,
    )

    from app.api.routers.auth import router as auth_router
    from app.api.routers.events import router as events_router

    app.include_router(auth_router)
    app.include_router(events_router)

    @app.get("/health", tags=["ops"])
    async def health() -> dict:
        return {"status": "ok"}

    return app
