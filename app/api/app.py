"""FastAPI application factory."""

import asyncio
from contextlib import asynccontextmanager

from aiogram.types import Update
from fastapi import FastAPI, HTTPException, Request

from app.core.config import settings

# Hold strong refs to fire-and-forget webhook tasks. Without this Python's GC
# can cancel them mid-flight (RUF006). Tasks remove themselves on completion.
_webhook_tasks: set[asyncio.Task] = set()


@asynccontextmanager
async def _lifespan(app: FastAPI):
    from app.bot.setup import create_bot, create_dispatcher, setup_bot_commands
    from app.db.base import close_engine, create_tables

    await create_tables()
    bot = create_bot()
    dp = create_dispatcher()

    # Register slash-command menu shown on "/" in Telegram. Best-effort: if
    # Telegram is unreachable at boot, log and move on — bot still works.
    try:
        await setup_bot_commands(bot)
    except Exception as exc:
        import logging

        logging.getLogger(__name__).warning(
            "set_my_commands failed at startup: %s: %s", type(exc).__name__, exc
        )

    app.state.bot = bot
    app.state.dp = dp

    if not settings.webhook_url:
        # Local dev: long-polling in background
        from app.bot.polling import start_polling

        app.state.polling_task = asyncio.create_task(start_polling(bot, dp))

    # Reminder scheduler (cron-driven via REMINDER_CRON).
    if settings.reminder_cron:
        from app.bot.scheduler import start_scheduler

        app.state.scheduler_task = asyncio.create_task(start_scheduler(bot))

    # Daily morning digest scheduler — cron-driven per DAILY_DIGEST_CRON.
    if settings.daily_digest_enabled:
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


def create_app() -> FastAPI:
    app = FastAPI(
        title="Gigs Bot API",
        description="Backend for the Telegram Google Calendar bot",
        version="0.1.0",
        lifespan=_lifespan,
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
        if settings.webhook_secret:
            token = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
            if token != settings.webhook_secret:
                raise HTTPException(status_code=403, detail="Invalid secret")

        data = await request.json()
        update = Update.model_validate(data)
        # Fire-and-forget: return 200 immediately so Telegram doesn't retry.
        # Processing (including outbound API calls) happens in the background.
        task = asyncio.create_task(request.app.state.dp.feed_update(request.app.state.bot, update))
        _webhook_tasks.add(task)
        task.add_done_callback(_webhook_tasks.discard)
        return {"ok": True}

    return app
