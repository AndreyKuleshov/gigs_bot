"""Daily event reminder service.

Queries all authenticated users, fetches their upcoming events (next 24 h),
and sends a Telegram message for each user that has at least one event.
"""

import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import Bot
from sqlalchemy import select, update

from app.core.config import settings
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


def _resolve_tz(tz_name: str, user_id: int) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, KeyError):
        logger.warning("Invalid TZ for user %d: %s, using UTC", user_id, tz_name)
        return ZoneInfo("UTC")


async def send_daily_digest_to_user(
    bot: Bot,
    user_id: int,
    tz_name: str,
    *,
    force: bool = False,
    last_sent: date | None = None,
) -> bool:
    """Send today's events digest to one user.

    If *force* is False, sends only when the user's local clock is past
    ``DAILY_DIGEST_HOUR`` and *last_sent* is not today (in the user's tz).
    If *force* is True, sends unconditionally.

    On successful send, persists ``User.last_daily_sent_date = today_local``.
    Returns True if a message was actually sent.
    """
    creds = await auth_service.get_credentials(user_id)
    if creds is None:
        return False

    tz = _resolve_tz(tz_name or "UTC", user_id)
    now = datetime.now(tz=tz)
    today_local = now.date()

    if not force:
        if now.hour < settings.daily_digest_hour:
            return False
        if last_sent == today_local:
            return False

    day_start = datetime(today_local.year, today_local.month, today_local.day, tzinfo=tz)
    day_end = day_start + timedelta(days=1)

    calendar_id = await auth_service.get_calendar_id(user_id) or "primary"

    try:
        events = await calendar_service.list_events(
            creds,
            calendar_id=calendar_id,
            max_results=50,
            time_min=day_start,
            time_max=day_end,
        )
    except Exception:
        logger.exception("Failed to fetch events for daily digest, user %d", user_id)
        return False

    if events:
        lines = ["📅 <b>События на сегодня:</b>\n"]
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
        text = "\n".join(lines)
    else:
        text = "📅 <b>На сегодня ничего не запланировано.</b>"

    await bot.send_message(user_id, text, parse_mode="HTML")

    async with get_session() as session:
        await session.execute(
            update(User).where(User.id == user_id).values(last_daily_sent_date=today_local)
        )
    return True


async def tick_daily_digests(bot: Bot) -> int:
    """One pass over all authenticated users: send today's digest to those
    whose local clock has passed ``DAILY_DIGEST_HOUR`` and who haven't been
    sent today. Returns the number of digests sent this tick."""
    async with get_session() as session:
        result = await session.execute(
            select(User.id, User.timezone, User.username, User.last_daily_sent_date).where(
                User.google_tokens_encrypted.isnot(None)
            )
        )
        users = result.all()

    sent = 0
    for user_id, tz_name, username, last_sent in users:
        try:
            if await send_daily_digest_to_user(
                bot, user_id, tz_name or "UTC", force=False, last_sent=last_sent
            ):
                sent += 1
        except Exception:
            logger.exception("Daily digest failed for user %d (@%s)", user_id, username or "—")
    return sent
