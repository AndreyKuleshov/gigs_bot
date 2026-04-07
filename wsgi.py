"""PythonAnywhere WSGI entry point.

In the PythonAnywhere web app config, set the WSGI file to this file and
the working directory to the project root.

Environment variables to set in the PythonAnywhere web app:
  TELEGRAM_BOT_TOKEN, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET,
  GOOGLE_REDIRECT_URI, OPENAI_API_KEY, FERNET_KEY, WEBHOOK_URL, WEBHOOK_SECRET
"""

import asyncio

from a2wsgi import ASGIMiddleware

from app.api.app import create_app
from app.bot.setup import create_bot, create_dispatcher
from app.db.base import create_tables

# ── Eager initialisation ───────────────────────────────────────────────────────
# Run all async startup (DB table creation) at import time so the first
# WSGI request is not burdened with a 60-second lifespan cold-start.
asyncio.run(create_tables())
_bot = create_bot()
_dp = create_dispatcher()

# ── Application ────────────────────────────────────────────────────────────────
application = ASGIMiddleware(create_app(preloaded_bot=_bot, preloaded_dp=_dp))  # type: ignore[arg-type]
