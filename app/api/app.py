"""FastAPI application factory."""

from contextlib import asynccontextmanager
from typing import Any

from aiogram.types import Update
from fastapi import FastAPI, HTTPException, Request

from app.core.config import settings


def _make_lifespan(preloaded_bot: Any = None, preloaded_dp: Any = None):
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        from app.bot.setup import create_bot, create_dispatcher
        from app.db.base import close_engine, create_tables

        if preloaded_bot is not None and preloaded_dp is not None:
            # Already initialised outside (e.g. wsgi.py eager startup).
            bot = preloaded_bot
            dp = preloaded_dp
        else:
            await create_tables()
            bot = create_bot()
            dp = create_dispatcher()

        app.state.bot = bot
        app.state.dp = dp

        if not settings.webhook_url:
            # Local dev: long-polling in background
            import asyncio

            from app.bot.polling import start_polling

            app.state.polling_task = asyncio.create_task(start_polling(bot, dp))

        # Reminder scheduler (runs in both webhook and polling modes)
        if settings.reminder_cron:
            import asyncio

            from app.bot.scheduler import start_scheduler

            app.state.scheduler_task = asyncio.create_task(start_scheduler(bot))

        # Daily morning digest scheduler (per-user local 9 AM, ~60s pulse loop).
        # PA free tier has no cron; this runs inside the webapp process.
        if settings.daily_digest_enabled:
            import asyncio

            from app.bot.scheduler import start_daily_digest_scheduler

            app.state.daily_digest_task = asyncio.create_task(start_daily_digest_scheduler(bot))

        yield

        if hasattr(app.state, "scheduler_task"):
            app.state.scheduler_task.cancel()
        if hasattr(app.state, "daily_digest_task"):
            app.state.daily_digest_task.cancel()
        if not settings.webhook_url:
            app.state.polling_task.cancel()

        await bot.session.close()
        await close_engine()

    return lifespan


def create_app(preloaded_bot: Any = None, preloaded_dp: Any = None) -> FastAPI:
    app = FastAPI(
        title="Gigs Bot API",
        description="Backend for the Telegram Google Calendar bot",
        version="0.1.0",
        lifespan=_make_lifespan(preloaded_bot, preloaded_dp),
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
