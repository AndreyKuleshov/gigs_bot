"""Free-text mode: route plain messages through the AI agent."""

import re

from aiogram import F, Router
from aiogram.types import Message, URLInputFile

from app.services.ai_agent import ai_agent
from app.services.auth_service import auth_service

router = Router(name="text_mode")

_MAX_CAPTION = 1024


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


@router.message(F.text)
async def handle_free_text(message: Message) -> None:
    # Ignore commands – let other routers handle them first
    if not message.text or message.text.startswith("/"):
        return
    if message.from_user is None:
        return

    user_id = message.from_user.id

    if not await auth_service.is_authenticated(user_id):
        await message.answer("⚠️ Please connect your Google account first. Use /auth")
        return

    thinking = await message.answer("🤔 Thinking…")
    response = await ai_agent.process_message(user_id, message.text)

    if response.image_url:
        await thinking.delete()
        caption = _strip_html(response.text)[:_MAX_CAPTION]
        try:
            await message.answer_photo(
                photo=URLInputFile(response.image_url),
                caption=caption,
            )
            # Send the full HTML-formatted text as a follow-up if it has formatting
            if "<" in response.text:
                try:
                    await message.answer(response.text, parse_mode="HTML")
                except Exception:
                    await message.answer(response.text)
        except Exception:
            # Image failed — fall back to text only
            try:
                await message.answer(response.text, parse_mode="HTML")
            except Exception:
                await message.answer(response.text)
    else:
        try:
            await thinking.edit_text(response.text, parse_mode="HTML")
        except Exception:
            await thinking.edit_text(response.text)
