"""PythonAnywhere WSGI entry point.

In the PythonAnywhere web app config, set the WSGI file to this file and
the working directory to the project root.

Environment variables to set in the PythonAnywhere web app:
  TELEGRAM_BOT_TOKEN, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET,
  GOOGLE_REDIRECT_URI, OPENAI_API_KEY, FERNET_KEY, WEBHOOK_URL, WEBHOOK_SECRET
"""

from a2wsgi import ASGIMiddleware

from app.api.app import create_app

# PythonAnywhere uses WSGI — wrap the ASGI FastAPI app.
# type: ignore comment suppresses the minor ASGI Scope type mismatch between
# starlette and a2wsgi — both are structurally compatible at runtime.
application = ASGIMiddleware(create_app())  # type: ignore[arg-type]
