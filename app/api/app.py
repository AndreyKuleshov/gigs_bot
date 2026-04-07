"""FastAPI application factory."""

from contextlib import asynccontextmanager

from aiogram.types import Update
from fastapi import FastAPI, HTTPException, Request

from app.core.config import settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.bot.setup import create_bot, create_dispatcher
    from app.db.base import close_engine, create_tables

    await create_tables()

    bot = create_bot()
    dp = create_dispatcher()
    app.state.bot = bot
    app.state.dp = dp

    if settings.webhook_url:
        # Webhook is registered externally (e.g. via curl / CLI).
        # We skip set_webhook here to avoid a blocking outbound call during
        # WSGI startup (PythonAnywhere free tier times out on cold start).
        pass
    else:
        # Local dev: long-polling in background
        import asyncio

        from app.bot.polling import start_polling

        app.state.polling_task = asyncio.create_task(start_polling(bot, dp))

    yield

    # Shutdown
    if not settings.webhook_url:
        app.state.polling_task.cancel()

    await bot.session.close()
    await close_engine()


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

    @app.post("/webhook/telegram", tags=["ops"])
    async def telegram_webhook(request: Request) -> dict:
        import asyncio

        if settings.webhook_secret:
            token = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
            if token != settings.webhook_secret:
                raise HTTPException(status_code=403, detail="Invalid secret")

        data = await request.json()
        update = Update.model_validate(data)
        # Fire-and-forget: return 200 immediately so Telegram doesn't retry.
        # Processing (including outbound API calls) happens in the background.
        asyncio.create_task(request.app.state.dp.feed_update(request.app.state.bot, update))
        return {"ok": True}

    return app
