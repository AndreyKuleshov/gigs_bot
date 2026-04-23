"""PythonAnywhere WSGI entry point — no ASGI bridge needed.

Handles the four routes the bot uses:
  GET  /health
  POST /webhook/telegram
  GET  /auth/google
  GET  /auth/google/callback

Each async operation runs in its own asyncio.run() call so there is no
shared persistent event loop that can become stuck or corrupted between
requests.  The webhook handler returns 200 immediately and processes the
Telegram update in a daemon thread.
"""

import asyncio
import json
import logging
import os
import threading
from urllib.parse import parse_qs

from app.bot.setup import create_bot, create_dispatcher
from app.core.config import settings
from app.db.base import create_tables

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Eager initialisation ───────────────────────────────────────────────────────
asyncio.run(create_tables())
_dp = create_dispatcher()  # shared; stateless except MemoryStorage (asyncio-safe)


# ── Background schedulers ──────────────────────────────────────────────────────
# NOTE: under PythonAnywhere's single-worker uWSGI, a long-lived daemon thread
# running its own asyncio loop starves the request worker — /health and
# /webhook/telegram end up timing out. So we do NOT host schedulers in-process
# on WSGI. Instead, expose HTTP tick endpoints below that an external cron
# (e.g. cron-job.org) pings on a schedule. The idempotency guard
# (User.last_daily_sent_date) makes the HTTP path safe to call often.


# ── Async helpers ──────────────────────────────────────────────────────────────
async def _feed_update(body: bytes) -> None:
    """Parse and dispatch one Telegram update, then close the bot session."""
    from aiogram.types import Update

    bot = create_bot()
    try:
        update = Update.model_validate(json.loads(body))
        await _dp.feed_update(bot, update)
    finally:
        await bot.session.close()


# ── HTML templates ─────────────────────────────────────────────────────────────
_SUCCESS_HTML = b"""<!doctype html>
<html>
<head><meta charset="utf-8"><title>Authorised</title></head>
<body style="font-family:sans-serif;text-align:center;padding-top:4rem">
  <h1>&#x2705; Google Calendar connected!</h1>
  <p>You can close this tab and return to the Telegram bot.</p>
  <script>setTimeout(() => window.close(), 3000);</script>
</body>
</html>"""

_ERROR_HTML = """<!doctype html>
<html>
<head><meta charset="utf-8"><title>Error</title></head>
<body style="font-family:sans-serif;text-align:center;padding-top:4rem">
  <h1>&#x274C; Authorisation failed</h1>
  <p>{reason}</p>
</body>
</html>"""


# ── WSGI application ───────────────────────────────────────────────────────────
def application(environ, start_response):  # type: ignore[no-untyped-def]
    path = environ.get("PATH_INFO", "/")
    method = environ.get("REQUEST_METHOD", "GET")
    params = parse_qs(environ.get("QUERY_STRING", ""))

    def respond(status, body, content_type="application/json"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        start_response(
            status,
            [("Content-Type", content_type), ("Content-Length", str(len(body)))],
        )
        return [body]

    def redirect(url):
        start_response("302 Found", [("Location", url)])
        return [b""]

    # ── Health ─────────────────────────────────────────────────────────────────
    if path == "/health":
        return respond("200 OK", b'{"status":"ok"}')

    # ── Debug: connectivity test ───────────────────────────────────────────────
    if path == "/debug/aiohttp":

        async def _debug() -> dict:
            import aiohttp

            results: dict = {}
            proxy_url = os.environ.get("https_proxy") or os.environ.get("http_proxy")
            results["proxy_url"] = proxy_url
            token = settings.telegram_bot_token
            url = f"https://api.telegram.org/bot{token}/getMe"
            t = aiohttp.ClientTimeout(total=8)

            # Direct (no proxy)
            try:
                async with aiohttp.ClientSession(timeout=t) as s:
                    async with s.get(url) as r:
                        results["direct"] = f"HTTP {r.status}"
            except Exception as exc:
                results["direct"] = f"{type(exc).__name__}: {exc}"

            # Via proxy
            if proxy_url:
                try:
                    async with aiohttp.ClientSession(timeout=t) as s:
                        async with s.get(url, proxy=proxy_url) as r:
                            results["proxy"] = f"HTTP {r.status}"
                except Exception as exc:
                    results["proxy"] = f"{type(exc).__name__}: {exc}"

            return results

        try:
            results = asyncio.run(_debug())
            return respond("200 OK", json.dumps(results).encode())
        except Exception as exc:
            return respond("500 Internal Server Error", json.dumps({"error": str(exc)}).encode())

    # ── Telegram webhook ───────────────────────────────────────────────────────
    if method == "POST" and path == "/webhook/telegram":
        secret = environ.get("HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN", "")
        if settings.webhook_secret and secret != settings.webhook_secret:
            return respond("403 Forbidden", b'{"error":"forbidden"}')

        body_size = int(environ.get("CONTENT_LENGTH", 0) or 0)
        body = environ["wsgi.input"].read(body_size)

        def process():
            try:
                asyncio.run(_feed_update(body))
                logger.info("Update processed OK")
            except Exception:
                logger.exception("Webhook processing error")

        threading.Thread(target=process, daemon=True).start()
        return respond("200 OK", b'{"ok":true}')

    # ── External-cron scheduler ticks ──────────────────────────────────────────
    # Dedicated endpoints for an external cron (e.g. cron-job.org) to hit on a
    # schedule. Require WEBHOOK_SECRET in X-Webhook-Secret so they are not
    # public. Idempotent: digest is guarded by User.last_daily_sent_date.
    if method == "POST" and path in ("/internal/tick-digest", "/internal/tick-reminders"):
        secret = environ.get("HTTP_X_WEBHOOK_SECRET", "")
        if not settings.webhook_secret or secret != settings.webhook_secret:
            return respond("403 Forbidden", b'{"error":"forbidden"}')

        async def _tick() -> int:
            if path == "/internal/tick-digest":
                from app.services.reminder_service import tick_daily_digests

                fn = tick_daily_digests
            else:
                from app.services.reminder_service import send_reminders

                fn = send_reminders
            bot = create_bot()
            try:
                return await fn(bot)
            finally:
                await bot.session.close()

        try:
            sent = asyncio.run(_tick())
            return respond("200 OK", json.dumps({"sent": sent}).encode())
        except Exception as exc:
            logger.exception("Internal tick failed")
            return respond("500 Internal Server Error", json.dumps({"error": str(exc)}).encode())

    # ── Google OAuth start ─────────────────────────────────────────────────────
    if method == "GET" and path == "/auth/google":
        try:
            user_id = int(params.get("telegram_user_id", [None])[0])  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return respond("400 Bad Request", b'{"error":"missing telegram_user_id"}')
        try:
            from app.services.auth_service import auth_service

            auth_url = asyncio.run(auth_service.get_auth_url(user_id))
            return redirect(auth_url)
        except Exception as exc:
            logger.error("Auth start error: %s", exc)
            return respond("500 Internal Server Error", b'{"error":"internal error"}')

    # ── Google OAuth callback ──────────────────────────────────────────────────
    if method == "GET" and path == "/auth/google/callback":
        error = params.get("error", [None])[0]
        code = params.get("code", [None])[0]
        state = params.get("state", [None])[0]

        if error:
            html = _ERROR_HTML.format(reason=error).encode()
            return respond("400 Bad Request", html, "text/html; charset=utf-8")

        if not code or not state:
            html = _ERROR_HTML.format(reason="Missing code or state").encode()
            return respond("400 Bad Request", html, "text/html; charset=utf-8")

        try:
            from app.services.auth_service import auth_service

            asyncio.run(auth_service.handle_oauth_callback(code=code, state=state))
            return respond("200 OK", _SUCCESS_HTML, "text/html; charset=utf-8")
        except ValueError as exc:
            html = _ERROR_HTML.format(reason=str(exc)).encode()
            return respond("400 Bad Request", html, "text/html; charset=utf-8")
        except Exception as exc:
            logger.error("OAuth callback error: %s", exc)
            html = _ERROR_HTML.format(reason="Internal error").encode()
            return respond("500 Internal Server Error", html, "text/html; charset=utf-8")

    return respond("404 Not Found", b'{"error":"not found"}')
