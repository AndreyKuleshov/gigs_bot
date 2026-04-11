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


def _clean_response(text: str) -> str:
    """Remove markdown image/link syntax that Telegram can't render."""
    # Remove ![alt](url)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    # Remove <img ...> tags
    text = re.sub(r"<img[^>]*>", "", text)
    # Clean up leftover blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


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

    # "Thinking" is cosmetic — don't crash if proxy is temporarily down
    thinking = None
    try:
        thinking = await message.answer("🤔 Thinking…")
    except Exception:
        pass

    response = await ai_agent.process_message(user_id, message.text)
    response.text = _clean_response(response.text)

    if response.pending_action:
        await state.set_state(AIConfirmFSM.waiting)
        await state.update_data(
            pending_tool=response.pending_action.tool_name,
            pending_args=response.pending_action.args,
        )
        try:
            if thinking:
                await thinking.edit_text(
                    response.text, reply_markup=confirm_kb("ai_act"), parse_mode="HTML"
                )
            else:
                await message.answer(
                    response.text, reply_markup=confirm_kb("ai_act"), parse_mode="HTML"
                )
        except Exception:
            if thinking:
                await thinking.edit_text(response.text, reply_markup=confirm_kb("ai_act"))
            else:
                await message.answer(response.text, reply_markup=confirm_kb("ai_act"))
        return

    if response.image_url:
        if thinking:
            try:
                await thinking.delete()
            except Exception:
                pass
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
    elif thinking:
        try:
            await thinking.edit_text(response.text, parse_mode="HTML")
        except Exception:
            await thinking.edit_text(response.text)
    else:
        try:
            await message.answer(response.text, parse_mode="HTML")
        except Exception:
            await message.answer(response.text)


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

    pending_tool = data.get("pending_tool")
    pending_args = data.get("pending_args")
    if not pending_tool or pending_args is None:
        await msg.edit_text("❌ Ошибка: действие устарело. Попробуй ещё раз.")
        await callback.answer()
        return

    await msg.edit_text("⏳ Выполняю…")
    try:
        result = await ai_agent.execute_confirmed_action(user_id, pending_tool, pending_args)
        text = f"✅ {result}"
    except Exception as exc:
        text = f"❌ Ошибка: {exc}"
    try:
        await msg.edit_text(text, parse_mode="HTML")
    except Exception:
        await msg.edit_text(text)
    await callback.answer()
