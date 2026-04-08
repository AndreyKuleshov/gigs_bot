"""Google OAuth2 endpoints."""

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from app.services.auth_service import auth_service
from app.services.calendar_service import calendar_service

router = APIRouter(prefix="/auth", tags=["auth"])

_SUCCESS_HTML = """
<!doctype html>
<html>
<head><meta charset="utf-8"><title>Authorised</title></head>
<body style="font-family:sans-serif;text-align:center;padding-top:4rem">
  <h1>✅ Google Calendar connected!</h1>
  <p>You can close this tab and return to the Telegram bot.</p>
  <script>setTimeout(() => window.close(), 3000);</script>
</body>
</html>
"""

_ERROR_HTML = """
<!doctype html>
<html>
<head><meta charset="utf-8"><title>Error</title></head>
<body style="font-family:sans-serif;text-align:center;padding-top:4rem">
  <h1>❌ Authorisation failed</h1>
  <p>{reason}</p>
</body>
</html>
"""


@router.get("/google")
async def google_auth_start(telegram_user_id: int) -> RedirectResponse:
    """Redirect to Google's OAuth2 consent screen.

    ``telegram_user_id`` must be passed as a query parameter so the
    callback can associate the tokens with the correct Telegram user.
    """
    auth_url = await auth_service.get_auth_url(telegram_user_id)
    return RedirectResponse(url=auth_url)


@router.get("/google/callback")
async def google_auth_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> HTMLResponse:
    """Handle the redirect back from Google after user consent."""
    if error:
        return HTMLResponse(content=_ERROR_HTML.format(reason=error), status_code=400)

    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing code or state")

    try:
        user_id = await auth_service.handle_oauth_callback(code=code, state=state)
    except ValueError as exc:
        return HTMLResponse(content=_ERROR_HTML.format(reason=str(exc)), status_code=400)

    # Fetch and store the user's Google Calendar timezone
    try:
        creds = await auth_service.get_credentials(user_id)
        if creds:
            tz = await calendar_service.get_user_timezone(creds)
            await auth_service.set_user_timezone(user_id, tz)
    except Exception:
        pass  # Non-critical, defaults to UTC

    return HTMLResponse(content=_SUCCESS_HTML, status_code=200)
