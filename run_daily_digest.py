#!/usr/bin/env python3
"""Standalone runner for the daily morning digest.

Usage:
  # Respect time gate + dedup (same behaviour as the background scheduler tick):
  python run_daily_digest.py

  # Send to ONE user right now, bypassing time gate + dedup:
  python run_daily_digest.py --user 12345 --force

  # Send to ALL authenticated users right now, bypassing time gate + dedup:
  python run_daily_digest.py --all --force

Without --force the script obeys DAILY_DIGEST_HOUR and the
``User.last_daily_sent_date`` guard, so calling it twice in the same
day is a no-op. Use --force when testing.
"""

import argparse
import asyncio
import logging

from sqlalchemy import select

from app.bot.setup import create_bot
from app.db.base import create_tables, get_session
from app.db.models import User
from app.services.reminder_service import (
    send_daily_digest_to_user,
    tick_daily_digests,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def _run_one_user(bot, user_id: int, force: bool) -> None:
    async with get_session() as session:
        row = await session.execute(
            select(User.timezone, User.full_name, User.last_daily_sent_date).where(
                User.id == user_id
            )
        )
        record = row.first()
    if record is None:
        logger.error("User %d not found", user_id)
        return
    tz_name, full_name, last_sent = record
    sent = await send_daily_digest_to_user(
        bot,
        user_id,
        tz_name or "UTC",
        force=force,
        last_sent=last_sent,
        full_name=full_name,
    )
    logger.info("User %d: %s", user_id, "sent" if sent else "skipped")


async def _run_all_force(bot) -> None:
    async with get_session() as session:
        result = await session.execute(
            select(
                User.id,
                User.timezone,
                User.username,
                User.full_name,
                User.last_daily_sent_date,
            ).where(User.google_tokens_encrypted.isnot(None))
        )
        users = result.all()
    total = 0
    for user_id, tz_name, username, full_name, last_sent in users:
        try:
            if await send_daily_digest_to_user(
                bot,
                user_id,
                tz_name or "UTC",
                force=True,
                last_sent=last_sent,
                full_name=full_name,
            ):
                total += 1
        except Exception:
            logger.exception(
                "Forced daily digest failed for user %d (@%s)", user_id, username or "—"
            )
    logger.info("Forced daily digest sent to %d user(s)", total)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", type=int, help="Telegram user id (single-user mode)")
    parser.add_argument("--all", action="store_true", help="Iterate over all users")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Bypass the time gate and the last-sent-today check",
    )
    args = parser.parse_args()

    if args.user is not None and args.all:
        parser.error("--user and --all are mutually exclusive")

    await create_tables()
    bot = create_bot()
    try:
        if args.user is not None:
            await _run_one_user(bot, args.user, args.force)
        elif args.all:
            if not args.force:
                parser.error("--all requires --force (otherwise use the tick form)")
            await _run_all_force(bot)
        else:
            sent = await tick_daily_digests(bot)
            logger.info("Tick complete, sent to %d user(s)", sent)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
