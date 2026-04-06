"""Keyboard builder helpers — both inline and reply keyboards."""

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.services.calendar_service import CalendarRead, EventRead


def menu_reply_kb() -> ReplyKeyboardMarkup:
    """Persistent reply keyboard with a single 'Menu' button.

    ``is_persistent=True`` keeps it visible at all times, regardless of
    inline keyboards being shown or FSM states changing.
    """
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📋 Menu")]],
        resize_keyboard=True,
        is_persistent=True,
    )


def main_menu_kb(mode: str = "button") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="📅 List events", callback_data="list_events"),
        InlineKeyboardButton(text="➕ Create event", callback_data="create_event"),
    )
    b.row(
        InlineKeyboardButton(text="✏️ Update event", callback_data="update_event"),
        InlineKeyboardButton(text="🗑 Delete event", callback_data="delete_event"),
    )
    b.row(InlineKeyboardButton(text="📆 Select calendar", callback_data="select_calendar"))
    if mode == "button":
        b.row(InlineKeyboardButton(text="Switch to 🤖 AI mode", callback_data="switch_to_text"))
    else:
        b.row(
            InlineKeyboardButton(text="Switch to 🔘 Button mode", callback_data="switch_to_button")
        )
    return b.as_markup()


def calendars_kb(calendars: list[CalendarRead]) -> InlineKeyboardMarkup:
    """One button per calendar; primary is marked with a star.

    Uses a numeric index as callback_data to stay within Telegram's 64-byte limit
    (calendar IDs are email-style strings that can easily exceed it).
    """
    b = InlineKeyboardBuilder()
    for i, cal in enumerate(calendars):
        label = f"{'★ ' if cal.primary else ''}{cal.name[:40]}"
        b.row(InlineKeyboardButton(text=label, callback_data=f"cal_pick:{i}"))
    b.row(InlineKeyboardButton(text="🔙 Back", callback_data="main_menu"))
    return b.as_markup()


def events_kb(events: list[EventRead], action: str) -> InlineKeyboardMarkup:
    """One button per event labelled with its title and date."""
    b = InlineKeyboardBuilder()
    for e in events:
        label = f"{e.summary[:28]} | {e.start.strftime('%d.%m %H:%M')}"
        b.row(InlineKeyboardButton(text=label, callback_data=f"{action}:{e.event_id}"))
    b.row(InlineKeyboardButton(text="🔙 Back", callback_data="main_menu"))
    return b.as_markup()


def confirm_kb(action: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="✅ Yes", callback_data=f"{action}:yes"),
        InlineKeyboardButton(text="❌ No", callback_data=f"{action}:no"),
    )
    return b.as_markup()


def back_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="🔙 Cancel", callback_data="main_menu"))
    return b.as_markup()


def update_field_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for field, label in [
        ("summary", "📝 Title"),
        ("start", "🕐 Start time"),
        ("end", "🕑 End time"),
        ("description", "📋 Description"),
        ("location", "📍 Location"),
    ]:
        b.row(InlineKeyboardButton(text=label, callback_data=f"update_field:{field}"))
    b.row(InlineKeyboardButton(text="🔙 Cancel", callback_data="main_menu"))
    return b.as_markup()
