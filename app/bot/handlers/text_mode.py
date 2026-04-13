"""Free-text mode: route plain messages through the AI agent."""

import logging
import re

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, URLInputFile

from app.bot.keyboards import confirm_kb
from app.bot.states import AIConfirmFSM
from app.core.config import settings
from app.services.ai_agent import ai_agent
from app.services.auth_service import auth_service

router = Router(name="text_mode")
logger = logging.getLogger(__name__)

_MAX_CAPTION = 1024


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


def _clean_response(text: str) -> str:
    """Convert markdown remnants to Telegram HTML."""
    # ![alt](url) → remove entire line
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)\s*", "", text)
    # <img> → remove
    text = re.sub(r"<img[^>]*>", "", text)
    # Lines with only [text] (orphan image/alt references) → remove
    text = re.sub(r"^\[[^\]]*\]\s*$", "", text, flags=re.MULTILINE)
    # Lines about images → remove
    text = re.sub(
        r"^.*(?:вот изображение|here is (?:an |the )?image|вот фото|here is (?:a |the )?photo).*$",
        "",
        text,
        flags=re.MULTILINE | re.IGNORECASE,
    )
    # [text](url) → <a href="url">text</a> (only if not already inside an <a> tag)
    text = re.sub(r"(?<!href=\")\[([^\]]+)\]\((https?://[^)]+)\)", r'<a href="\2">\1</a>', text)
    # "phrase (https://url)" → <a href="url">phrase</a>
    text = re.sub(r"(.+?)\s+\((https?://[^)]+)\)", r'<a href="\2">\1</a>', text)
    # **bold** → <b>bold</b>
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    # *italic* → <i>italic</i> (but not inside URLs)
    text = re.sub(r"(?<![/\w])\*(.+?)\*(?![/\w])", r"<i>\1</i>", text)
    # ### headers → <b>header</b>
    text = re.sub(r"^#{1,6}\s+(.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)
    # - list items → • bullet
    text = re.sub(r"^- ", "• ", text, flags=re.MULTILINE)
    # Clean up blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


async def _send_text(target: Message, text: str, **kwargs) -> None:
    """Send text with HTML, fallback to plain, swallow proxy errors."""
    try:
        await target.answer(text, parse_mode="HTML", **kwargs)
    except Exception:
        try:
            await target.answer(text, **kwargs)
        except Exception:
            logger.warning("Failed to send response (proxy down?)")


async def _edit_or_send(thinking: Message | None, fallback: Message, text: str, **kwargs) -> None:
    """Edit the thinking message or send a new one. Swallow proxy errors."""
    if thinking:
        try:
            await thinking.edit_text(text, parse_mode="HTML", **kwargs)
            return
        except Exception:
            try:
                await thinking.edit_text(text, **kwargs)
                return
            except Exception:
                pass  # Fall through to send new message
    try:
        await fallback.answer(text, parse_mode="HTML", **kwargs)
    except Exception:
        try:
            await fallback.answer(text, **kwargs)
        except Exception:
            logger.warning("Failed to send response (proxy down?)")


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
        await _edit_or_send(thinking, message, response.text, reply_markup=confirm_kb("ai_act"))
        return

    # Embed image as link when proxy is set (sendPhoto fails through proxy)
    if response.image_url:
        if settings.proxy_url:
            response.text += f'\n\n🖼 <a href="{response.image_url}">Фото</a>'
            response.image_url = None
        else:
            if thinking:
                try:
                    await thinking.delete()
                except Exception:
                    pass
                thinking = None
            caption = _strip_html(response.text)[:_MAX_CAPTION]
            try:
                await message.answer_photo(photo=URLInputFile(response.image_url), caption=caption)
                if "<" in response.text:
                    await _send_text(message, response.text)
                return
            except Exception:
                pass  # Fall through to text-only

    await _edit_or_send(thinking, message, response.text)


@router.callback_query(AIConfirmFSM.waiting, F.data.startswith("ai_act:"))
async def ai_confirm_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user is None or not isinstance(callback.message, Message):
        return
    user_id = callback.from_user.id
    msg = callback.message

    choice = callback.data.split(":", 1)[1] if callback.data else ""
    if choice == "no":
        await state.clear()
        try:
            await msg.edit_text("❌ Отменено.")
        except Exception:
            pass
        await callback.answer()
        return

    data = await state.get_data()
    await state.clear()

    pending_tool = data.get("pending_tool")
    pending_args = data.get("pending_args")
    if not pending_tool or pending_args is None:
        try:
            await msg.edit_text("❌ Ошибка: действие устарело. Попробуй ещё раз.")
        except Exception:
            pass
        await callback.answer()
        return

    try:
        await msg.edit_text("⏳ Выполняю…")
    except Exception:
        pass
    try:
        result = await ai_agent.execute_confirmed_action(user_id, pending_tool, pending_args)
        text = f"✅ {result}"
    except Exception as exc:
        text = f"❌ Ошибка: {exc}"
    try:
        await msg.edit_text(text, parse_mode="HTML")
    except Exception:
        try:
            await msg.edit_text(text)
        except Exception:
            logger.warning("Failed to send confirm result (proxy down?)")
    await callback.answer()
