"""/start, /auth, /menu, timezone handlers."""

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app.bot.keyboards import back_kb, main_menu_kb, menu_reply_kb, timezone_kb
from app.bot.states import SetTimezoneFSM
from app.services.auth_service import auth_service

router = Router(name="common")


async def _menu_kb(user_id: int) -> InlineKeyboardMarkup:
    """Build main menu keyboard with current calendar and timezone."""
    cal = await auth_service.get_calendar_name(user_id)
    tz = await auth_service.get_user_timezone(user_id)
    return main_menu_kb(cal, tz if tz != "UTC" else None)


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    user = message.from_user
    if user is None:
        return
    # User row is already ensured by UserSyncMiddleware on every update.
    await message.answer(
        f"👋 Hello, {user.first_name}!\n\n"
        "I can help you manage your Google Calendar.\n"
        "Use /auth to connect your Google account.\n\n"
        "You can use the buttons below or just type in plain text.",
        reply_markup=menu_reply_kb(),
    )
    await message.answer("📋 Main menu:", reply_markup=await _menu_kb(user.id))


@router.message(Command("auth"))
async def cmd_auth(message: Message) -> None:
    if message.from_user is None:
        return
    user_id = message.from_user.id
    if await auth_service.is_authenticated(user_id):
        await message.answer("✅ Your Google account is already connected.")
        return
    auth_url = await auth_service.get_auth_url(user_id)
    await message.answer(
        "🔐 Connect your Google Calendar:\n\n"
        f"{auth_url}\n\n"
        "After authorisation you will be redirected back automatically."
    )


@router.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext) -> None:
    if message.from_user is None:
        return
    await state.clear()
    await message.answer("📋 Main menu:", reply_markup=await _menu_kb(message.from_user.id))


@router.message(F.text == "📋 Menu")
async def reply_menu_button(message: Message, state: FSMContext) -> None:
    """Handle the persistent reply-keyboard 'Menu' button."""
    if message.from_user is None:
        return
    await state.clear()
    await message.answer("📋 Main menu:", reply_markup=await _menu_kb(message.from_user.id))


@router.message(Command("disconnect"))
async def cmd_disconnect(message: Message) -> None:
    if message.from_user is None:
        return
    await auth_service.revoke_tokens(message.from_user.id)
    await message.answer("🔓 Google Calendar disconnected.")


@router.callback_query(F.data == "main_menu")
async def cb_main_menu(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user is None or not isinstance(callback.message, Message):
        return
    await state.clear()
    kb = await _menu_kb(callback.from_user.id)
    await callback.message.edit_text("📋 Main menu:", reply_markup=kb)
    await callback.answer()


# ── Timezone selection ───────────────────────────────────────────────────────


@router.callback_query(F.data == "set_timezone")
async def cb_set_timezone(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user is None or not isinstance(callback.message, Message):
        return
    await state.set_state(SetTimezoneFSM.waiting_for_input)
    await callback.message.edit_text("🕐 Выбери часовой пояс:", reply_markup=timezone_kb())
    await callback.answer()


@router.callback_query(SetTimezoneFSM.waiting_for_input, F.data.startswith("tz_pick:"))
async def cb_tz_pick(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user is None or not isinstance(callback.message, Message):
        return
    tz_id = callback.data.split(":", 1)[1] if callback.data else ""
    await auth_service.set_user_timezone(callback.from_user.id, tz_id)
    await state.clear()
    kb = await _menu_kb(callback.from_user.id)
    await callback.message.edit_text(
        f"✅ Часовой пояс: <b>{tz_id}</b>", reply_markup=kb, parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(SetTimezoneFSM.waiting_for_input, F.data == "tz_custom")
async def cb_tz_custom(callback: CallbackQuery) -> None:
    if callback.from_user is None or not isinstance(callback.message, Message):
        return
    await callback.message.edit_text(
        "⌨️ Введи IANA timezone, например:\n"
        "<code>Europe/Belgrade</code>, <code>Asia/Tokyo</code>, "
        "<code>America/New_York</code>\n\n"
        "Полный список: en.wikipedia.org/wiki/List_of_tz_database_time_zones",
        reply_markup=back_kb(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(SetTimezoneFSM.waiting_for_input)
async def fsm_tz_input(message: Message, state: FSMContext) -> None:
    if message.from_user is None or not message.text:
        return
    tz_input = message.text.strip()
    try:
        ZoneInfo(tz_input)
    except (ZoneInfoNotFoundError, KeyError):
        await message.answer(
            f"⚠️ <code>{tz_input}</code> — неизвестный часовой пояс. Попробуй ещё раз.",
            reply_markup=back_kb(),
            parse_mode="HTML",
        )
        return
    await auth_service.set_user_timezone(message.from_user.id, tz_input)
    await state.clear()
    kb = await _menu_kb(message.from_user.id)
    await message.answer(f"✅ Часовой пояс: <b>{tz_input}</b>", reply_markup=kb, parse_mode="HTML")
