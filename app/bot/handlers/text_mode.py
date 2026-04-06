"""Free-text mode: route plain messages through the AI agent."""

from aiogram import F, Router
from aiogram.types import Message

from app.services.ai_agent import ai_agent
from app.services.auth_service import auth_service

router = Router(name="text_mode")


@router.message(F.text)
async def handle_free_text(message: Message) -> None:
    # Ignore commands – let other routers handle them first
    if not message.text or message.text.startswith("/"):
        return
    if message.from_user is None:
        return

    user_id = message.from_user.id
    mode = await auth_service.get_user_mode(user_id)

    if mode != "text":
        # Button mode: silently ignore plain text (user should press buttons)
        return

    if not await auth_service.is_authenticated(user_id):
        await message.answer("⚠️ Please connect your Google account first. Use /auth")
        return

    thinking = await message.answer("🤔 Thinking…")
    reply = await ai_agent.process_message(user_id, message.text)
    await thinking.edit_text(reply)
