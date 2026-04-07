"""PythonAnywhere WSGI entry point — no ASGI bridge needed.

Handles the four routes the bot uses:
  GET  /health
  POST /webhook/telegram
  GET  /auth/google
  GET  /auth/google/callback

The webhook handler returns 200 immediately and processes the Telegram
update in a daemon thread so uWSGI is never blocked by bot I/O.
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

# ── Persistent event loop ──────────────────────────────────────────────────────
# A single event loop running in a background thread. All async calls go through
# run_coroutine_threadsafe so aiogram's aiohttp session is never reused across
# different loops (which causes silent 502s under uWSGI).
_loop = asyncio.new_event_loop()
threading.Thread(target=_loop.run_forever, daemon=True).start()


def _run(coro, timeout: int = 60):  # type: ignore[no-untyped-def]
    """Submit a coroutine to the persistent loop and block until done."""
    return asyncio.run_coroutine_threadsafe(coro, _loop).result(timeout=timeout)


# ── Eager initialisation ───────────────────────────────────────────────────────
_run(create_tables())
_bot = create_bot()
_dp = create_dispatcher()


# ── Connectivity check (bot getMe from within the persistent loop) ────────────
async def _check_bot() -> dict:
    import aiohttp

    results: dict = {}

    # 1. Raw aiohttp without proxy
    url = f"https://api.telegram.org/bot{_bot.token}/getMe"
    try:
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.get(url) as resp:
                results["direct"] = f"HTTP {resp.status}"
    except Exception as exc:
        results["direct"] = f"FAIL {type(exc).__name__}: {exc}"

    # 2. Raw aiohttp through proxy
    proxy_url = os.environ.get("https_proxy") or os.environ.get("http_proxy")
    if proxy_url:
        try:
            timeout = aiohttp.ClientTimeout(total=8)
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.get(url, proxy=proxy_url) as resp:
                    results["proxy_raw"] = f"HTTP {resp.status}"
        except Exception as exc:
            results["proxy_raw"] = f"FAIL {type(exc).__name__}: {exc}"

    # 3. Bot session (aiogram, uses AiohttpSession with proxy if configured)
    try:
        me = await _bot.get_me()
        results["bot"] = f"ok bot=@{me.username}"
    except Exception as exc:
        results["bot"] = f"FAIL {type(exc).__name__}: {exc}"

    return results


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

    # ── Debug: event loop + connectivity tests ────────────────────────────────
    if path == "/debug/aiohttp":
        import asyncio as _asyncio
        import json as _json

        results: dict = {}

        # 1. Is the event loop alive?
        async def _ping():
            await _asyncio.sleep(0)
            return "ok"

        try:
            results["loop"] = _run(_ping(), timeout=3)
        except Exception as exc:
            results["loop"] = f"DEAD: {type(exc).__name__}: {exc}"
            return respond("200 OK", _json.dumps(results).encode())

        # 2. aiohttp direct (no proxy, 8s)
        async def _direct():
            import aiohttp

            url = f"https://api.telegram.org/bot{_bot.token}/getMe"
            t = aiohttp.ClientTimeout(total=8)
            async with aiohttp.ClientSession(timeout=t) as s:
                async with s.get(url) as r:
                    return f"HTTP {r.status}"

        try:
            results["direct"] = _run(_direct(), timeout=10)
        except Exception as exc:
            results["direct"] = f"{type(exc).__name__}: {exc}"

        # 3. aiohttp via proxy (8s)
        proxy_url = os.environ.get("https_proxy") or os.environ.get("http_proxy")
        results["proxy_url"] = proxy_url

        async def _via_proxy():
            import aiohttp

            url = f"https://api.telegram.org/bot{_bot.token}/getMe"
            t = aiohttp.ClientTimeout(total=8)
            async with aiohttp.ClientSession(timeout=t) as s:
                async with s.get(url, proxy=proxy_url) as r:
                    return f"HTTP {r.status}"

        if proxy_url:
            try:
                results["proxy"] = _run(_via_proxy(), timeout=10)
            except Exception as exc:
                results["proxy"] = f"{type(exc).__name__}: {exc}"

        return respond("200 OK", _json.dumps(results).encode())

    # ── Telegram webhook ───────────────────────────────────────────────────────
    if method == "POST" and path == "/webhook/telegram":
        secret = environ.get("HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN", "")
        if settings.webhook_secret and secret != settings.webhook_secret:
            return respond("403 Forbidden", b'{"error":"forbidden"}')

        body_size = int(environ.get("CONTENT_LENGTH", 0) or 0)
        body = environ["wsgi.input"].read(body_size)

        def process():
            try:
                from aiogram.types import Update

                proxy_vars = {k: v for k, v in os.environ.items() if "proxy" in k.lower()}
                logger.error("PROXY ENV: %s", proxy_vars)
                update = Update.model_validate(json.loads(body))
                _run(_dp.feed_update(_bot, update))
                logger.info("Update %s processed OK", update.update_id)
            except Exception:
                logger.exception("Webhook processing error")

        threading.Thread(target=process, daemon=True).start()
        return respond("200 OK", b'{"ok":true}')

    # ── Google OAuth start ─────────────────────────────────────────────────────
    if method == "GET" and path == "/auth/google":
        try:
            user_id = int(params.get("telegram_user_id", [None])[0])  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return respond("400 Bad Request", b'{"error":"missing telegram_user_id"}')
        try:
            from app.services.auth_service import auth_service

            auth_url = _run(auth_service.get_auth_url(user_id))
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

            _run(auth_service.handle_oauth_callback(code=code, state=state))
            return respond("200 OK", _SUCCESS_HTML, "text/html; charset=utf-8")
        except ValueError as exc:
            html = _ERROR_HTML.format(reason=str(exc)).encode()
            return respond("400 Bad Request", html, "text/html; charset=utf-8")
        except Exception as exc:
            logger.error("OAuth callback error: %s", exc)
            html = _ERROR_HTML.format(reason="Internal error").encode()
            return respond("500 Internal Server Error", html, "text/html; charset=utf-8")

    return respond("404 Not Found", b'{"error":"not found"}')
