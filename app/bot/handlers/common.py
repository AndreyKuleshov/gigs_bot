"""/start, /auth, /menu handlers."""

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.keyboards import main_menu_kb, menu_reply_kb
from app.services.auth_service import auth_service

router = Router(name="common")


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    user = message.from_user
    if user is None:
        return
    await auth_service.get_or_create_user(
        telegram_user_id=user.id,
        username=user.username,
        full_name=user.full_name,
    )
    await message.answer(
        f"👋 Hello, {user.first_name}!\n\n"
        "I can help you manage your Google Calendar.\n"
        "Use /auth to connect your Google account.\n\n"
        "You can use the buttons below or just type in plain text.",
        reply_markup=menu_reply_kb(),
    )
    await message.answer("📋 Main menu:", reply_markup=main_menu_kb())


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
    await message.answer("📋 Main menu:", reply_markup=main_menu_kb())


@router.message(F.text == "📋 Menu")
async def reply_menu_button(message: Message, state: FSMContext) -> None:
    """Handle the persistent reply-keyboard 'Menu' button."""
    if message.from_user is None:
        return
    await state.clear()
    await message.answer("📋 Main menu:", reply_markup=main_menu_kb())


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
    await callback.message.edit_text("📋 Main menu:", reply_markup=main_menu_kb())
    await callback.answer()
