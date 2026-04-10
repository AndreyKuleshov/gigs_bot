"""Free-text mode: route plain messages through the AI agent."""

import re

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, URLInputFile

from app.bot.keyboards import confirm_kb
from app.bot.states import AIConfirmFSM
from app.services.ai_agent import ai_agent
from app.services.auth_service import auth_service

router = Router(name="text_mode")

_MAX_CAPTION = 1024


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


@router.message(F.text)
async def handle_free_text(message: Message, state: FSMContext) -> None:
    # Ignore commands – let other routers handle them first
    if not message.text or message.text.startswith("/"):
        return
    if message.from_user is None:
        return

    user_id = message.from_user.id

    if not await auth_service.is_authenticated(user_id):
        await message.answer("⚠️ Please connect your Google account first. Use /auth")
        return

    if await auth_service.get_calendar_id(user_id) is None:
        await message.answer("⚠️ Сначала выбери календарь — нажми 📋 Menu → 📆 Select calendar")
        return

    # New message cancels any pending AI confirmation
    await state.clear()

    thinking = await message.answer("🤔 Thinking…")
    response = await ai_agent.process_message(user_id, message.text)

    if response.pending_action:
        await state.set_state(AIConfirmFSM.waiting)
        await state.update_data(
            pending_tool=response.pending_action.tool_name,
            pending_args=response.pending_action.args,
        )
        try:
            await thinking.edit_text(
                response.text, reply_markup=confirm_kb("ai_act"), parse_mode="HTML"
            )
        except Exception:
            await thinking.edit_text(response.text, reply_markup=confirm_kb("ai_act"))
        return

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


@router.callback_query(AIConfirmFSM.waiting, F.data.startswith("ai_act:"))
async def ai_confirm_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user is None or not isinstance(callback.message, Message):
        return
    user_id = callback.from_user.id
    msg = callback.message

    choice = callback.data.split(":", 1)[1] if callback.data else ""
    if choice == "no":
        await state.clear()
        await msg.edit_text("❌ Отменено.")
        await callback.answer()
        return

    data = await state.get_data()
    await state.clear()

    await msg.edit_text("⏳ Выполняю…")
    result = await ai_agent.execute_confirmed_action(
        user_id, data["pending_tool"], data["pending_args"]
    )
    try:
        await msg.edit_text(f"✅ {result}", parse_mode="HTML")
    except Exception:
        await msg.edit_text(f"✅ {result}")
    await callback.answer()
