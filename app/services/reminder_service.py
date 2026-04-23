"""Daily event reminder service.

Queries all authenticated users, fetches their upcoming events (next 24 h),
and sends a Telegram message for each user that has at least one event.
"""

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import Bot
from sqlalchemy import select

from app.db.base import get_session
from app.db.models import User
from app.services.auth_service import auth_service
from app.services.calendar_service import calendar_service

logger = logging.getLogger(__name__)


async def send_reminders(bot: Bot) -> int:
    """Send event reminders to all authenticated users.

    Returns the number of users who received a reminder.
    """
    async with get_session() as session:
        result = await session.execute(
            select(User.id, User.timezone, User.username).where(
                User.google_tokens_encrypted.isnot(None)
            )
        )
        users = result.all()

    sent = 0
    for user_id, tz_name, username in users:
        try:
            if await _remind_user(bot, user_id, tz_name or "UTC"):
                sent += 1
        except Exception:
            logger.exception("Failed to send reminder to user %d (@%s)", user_id, username or "—")
    return sent


async def _remind_user(bot: Bot, user_id: int, tz_name: str) -> bool:
    """Send a reminder to one user.  Returns True if a message was sent."""
    creds = await auth_service.get_credentials(user_id)
    if creds is None:
        return False

    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, KeyError):
        logger.warning("Invalid timezone for user %d: %s, using UTC", user_id, tz_name)
        tz = ZoneInfo("UTC")
    now = datetime.now(tz=tz)
    tomorrow = now + timedelta(hours=24)
    calendar_id = await auth_service.get_calendar_id(user_id) or "primary"

    events = await calendar_service.list_events(
        creds,
        calendar_id=calendar_id,
        max_results=25,
        time_min=now,
        time_max=tomorrow,
    )

    if not events:
        return False

    lines = ["📅 <b>Events in the next 24 hours:</b>\n"]
    for e in events:
        start_local = e.start.astimezone(tz)
        end_local = e.end.astimezone(tz)
        line = (
            f"• <b>{e.summary}</b>\n"
            f"  🕐 {start_local.strftime('%H:%M')} – {end_local.strftime('%H:%M')}"
        )
        if e.location:
            line += f"\n  📍 {e.location}"
        lines.append(line)

    await bot.send_message(user_id, "\n".join(lines), parse_mode="HTML")
    return True
