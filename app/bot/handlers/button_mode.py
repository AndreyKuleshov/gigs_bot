"""Button mode: FSM-driven calendar CRUD via inline keyboards."""

from datetime import date as date_type
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.keyboards import (
    back_kb,
    calendars_kb,
    confirm_kb,
    events_kb,
    main_menu_kb,
    start_time_kb,
    update_field_kb,
)
from app.bot.states import CreateEventFSM, DeleteEventFSM, SelectCalendarFSM, UpdateEventFSM
from app.services.auth_service import auth_service
from app.services.calendar_service import EventCreate, EventUpdate, calendar_service

router = Router(name="button_mode")


async def _menu_kb(user_id: int):
    """Build main menu keyboard with current calendar and timezone."""
    cal = await auth_service.get_calendar_name(user_id)
    tz = await auth_service.get_user_timezone(user_id)
    return main_menu_kb(cal, tz if tz != "UTC" else None)


# ── Type-narrowing helpers ────────────────────────────────────────────────────


def _ctx(callback: CallbackQuery) -> tuple[int, Message] | None:
    """Return (user_id, message) narrowed to concrete types, or None.

    Handles two Optional fields on CallbackQuery:
    - ``from_user`` may be None for anonymous channel buttons
    - ``message`` may be an InaccessibleMessage when > 48 h old
    """
    if callback.from_user is None:
        return None
    if not isinstance(callback.message, Message):
        return None
    return callback.from_user.id, callback.message


# ── Auth & calendar guards ────────────────────────────────────────────────────


async def _check_auth(callback: CallbackQuery) -> bool:
    """Show an alert and return False when the user is not authenticated."""
    if callback.from_user is None:
        return False
    if not await auth_service.is_authenticated(callback.from_user.id):
        await callback.answer("⚠️ Connect your Google account first. Use /auth", show_alert=True)
        return False
    return True


async def _check_calendar(callback: CallbackQuery) -> bool:
    """Show an alert and return False when no calendar is selected."""
    if callback.from_user is None:
        return False
    cal_id = await auth_service.get_calendar_id(callback.from_user.id)
    if cal_id is None:
        await callback.answer("⚠️ Select a calendar first!", show_alert=True)
        return False
    return True


# ── Select calendar ───────────────────────────────────────────────────────────


@router.callback_query(F.data == "select_calendar")
async def cb_select_calendar(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _check_auth(callback):
        return
    ctx = _ctx(callback)
    if ctx is None:
        return
    user_id, msg = ctx

    creds = await auth_service.get_credentials(user_id)
    if creds is None:
        await callback.answer("Authentication error", show_alert=True)
        return

    try:
        calendars = await calendar_service.list_calendars(creds)
    except RuntimeError as exc:
        await callback.answer(str(exc), show_alert=True)
        return

    # Store calendar list in FSM so the pick handler can resolve index → id
    await state.set_state(SelectCalendarFSM.selecting)
    await state.update_data(
        cal_ids=[c.calendar_id for c in calendars],
        cal_names=[c.name for c in calendars],
    )
    await msg.edit_text("📆 Choose a calendar:", reply_markup=calendars_kb(calendars))
    await callback.answer()


@router.callback_query(SelectCalendarFSM.selecting, F.data.startswith("cal_pick:"))
async def fsm_cal_pick(callback: CallbackQuery, state: FSMContext) -> None:
    ctx = _ctx(callback)
    if ctx is None or callback.data is None:
        return
    user_id, msg = ctx

    index = int(callback.data.split(":", 1)[1])
    data = await state.get_data()
    cal_ids: list[str] = data.get("cal_ids", [])
    cal_names: list[str] = data.get("cal_names", [])
    if index >= len(cal_ids):
        await callback.answer("Invalid selection", show_alert=True)
        return

    calendar_id = cal_ids[index]
    display = cal_names[index] if index < len(cal_names) else calendar_id
    await auth_service.set_calendar_id(user_id, calendar_id, display)
    await state.clear()

    await msg.edit_text(
        f"✅ Calendar set to <b>{display}</b>",
        reply_markup=await _menu_kb(user_id),
        parse_mode="HTML",
    )
    await callback.answer()


# ── List events ───────────────────────────────────────────────────────────────


@router.callback_query(F.data == "list_events")
async def cb_list_events(callback: CallbackQuery) -> None:
    if not await _check_auth(callback) or not await _check_calendar(callback):
        return
    ctx = _ctx(callback)
    if ctx is None:
        return
    user_id, msg = ctx

    creds = await auth_service.get_credentials(user_id)
    if creds is None:
        await callback.answer("Authentication error", show_alert=True)
        return

    calendar_id = await auth_service.get_calendar_id(user_id) or "primary"

    try:
        events = await calendar_service.list_events(creds, calendar_id=calendar_id, max_results=10)
    except RuntimeError as exc:
        await callback.answer(str(exc), show_alert=True)
        return

    if not events:
        await msg.edit_text("📭 No upcoming events.", reply_markup=back_kb())
        await callback.answer()
        return

    lines = ["📅 Upcoming events:\n"]
    for e in events:
        lines.append(
            f"• <b>{e.summary}</b>\n"
            f"  {e.start.strftime('%d.%m.%Y %H:%M')} – {e.end.strftime('%H:%M')}"
            + (f"\n  📍 {e.location}" if e.location else "")
        )
    await msg.edit_text("\n".join(lines)[:4096], reply_markup=back_kb(), parse_mode="HTML")
    await callback.answer()


# ── Create event FSM ──────────────────────────────────────────────────────────


@router.callback_query(F.data == "create_event")
async def cb_create_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _check_auth(callback) or not await _check_calendar(callback):
        return
    ctx = _ctx(callback)
    if ctx is None:
        return
    _, msg = ctx
    await state.set_state(CreateEventFSM.waiting_for_title)
    await msg.edit_text("📝 Enter the event title:", reply_markup=back_kb())
    await callback.answer()


@router.message(CreateEventFSM.waiting_for_title)
async def fsm_create_title(message: Message, state: FSMContext) -> None:
    if message.text is None:
        return
    await state.update_data(summary=message.text.strip())
    await state.set_state(CreateEventFSM.waiting_for_start_date)
    await message.answer("📅 Start date (DD.MM.YYYY):", reply_markup=back_kb())


@router.message(CreateEventFSM.waiting_for_start_date)
async def fsm_create_start_date(message: Message, state: FSMContext) -> None:
    if message.text is None:
        return
    try:
        date = datetime.strptime(message.text.strip(), "%d.%m.%Y").date()
    except ValueError:
        await message.answer("⚠️ Use format DD.MM.YYYY (e.g. 25.12.2025):", reply_markup=back_kb())
        return
    await state.update_data(start_date=date.isoformat())
    await state.set_state(CreateEventFSM.waiting_for_start_time)
    await message.answer("🕐 Start time (HH:MM) or press All day:", reply_markup=start_time_kb())


@router.callback_query(CreateEventFSM.waiting_for_start_time, F.data == "create_all_day")
async def fsm_create_all_day(callback: CallbackQuery, state: FSMContext) -> None:
    ctx = _ctx(callback)
    if ctx is None:
        return
    _, msg = ctx
    await state.update_data(all_day=True)
    await state.set_state(CreateEventFSM.waiting_for_end_date)
    await msg.edit_text(
        "📅 End date (DD.MM.YYYY).\nFor a single day, enter the same date:",
        reply_markup=back_kb(),
    )
    await callback.answer()


@router.message(CreateEventFSM.waiting_for_end_date)
async def fsm_create_end_date(message: Message, state: FSMContext) -> None:
    if message.text is None:
        return
    try:
        end_date = datetime.strptime(message.text.strip(), "%d.%m.%Y").date()
    except ValueError:
        await message.answer("⚠️ Use format DD.MM.YYYY:", reply_markup=back_kb())
        return
    data = await state.get_data()
    start_date = date_type.fromisoformat(data["start_date"])
    if end_date < start_date:
        await message.answer("⚠️ End date must be on or after start date:", reply_markup=back_kb())
        return
    # Google Calendar end date is exclusive — add one day
    await state.update_data(end_date=(end_date + timedelta(days=1)).isoformat())
    await state.set_state(CreateEventFSM.waiting_for_description)
    await message.answer(
        "📋 Description (optional – send /skip to leave empty):", reply_markup=back_kb()
    )


@router.message(CreateEventFSM.waiting_for_start_time)
async def fsm_create_start_time(message: Message, state: FSMContext) -> None:
    if message.text is None:
        return
    try:
        t = datetime.strptime(message.text.strip(), "%H:%M").time()
    except ValueError:
        await message.answer("⚠️ Use format HH:MM (e.g. 14:30):", reply_markup=back_kb())
        return
    user_id = message.from_user.id  # type: ignore[union-attr]
    tz = ZoneInfo(await auth_service.get_user_timezone(user_id))
    data = await state.get_data()
    start = datetime.fromisoformat(data["start_date"]).replace(
        hour=t.hour, minute=t.minute, tzinfo=tz
    )
    await state.update_data(start=start.isoformat())
    await state.set_state(CreateEventFSM.waiting_for_end_time)
    await message.answer("🕑 End time (HH:MM):", reply_markup=back_kb())


@router.message(CreateEventFSM.waiting_for_end_time)
async def fsm_create_end_time(message: Message, state: FSMContext) -> None:
    if message.text is None:
        return
    try:
        t = datetime.strptime(message.text.strip(), "%H:%M").time()
    except ValueError:
        await message.answer("⚠️ Use format HH:MM:", reply_markup=back_kb())
        return
    data = await state.get_data()
    start = datetime.fromisoformat(data["start"])
    end = start.replace(hour=t.hour, minute=t.minute)
    if end <= start:
        await message.answer("⚠️ End must be after start. Try again:", reply_markup=back_kb())
        return
    await state.update_data(end=end.isoformat())
    await state.set_state(CreateEventFSM.waiting_for_description)
    await message.answer(
        "📋 Description (optional – send /skip to leave empty):", reply_markup=back_kb()
    )


@router.message(CreateEventFSM.waiting_for_description)
async def fsm_create_description(message: Message, state: FSMContext) -> None:
    if message.text is None:
        return
    text = message.text.strip()
    desc = None if text == "/skip" else text
    await state.update_data(description=desc)

    data = await state.get_data()
    if data.get("all_day"):
        s = date_type.fromisoformat(data["start_date"])
        # end_date is stored as exclusive; show the real last day
        e = date_type.fromisoformat(data["end_date"]) - timedelta(days=1)
        if s == e:
            date_line = f"Date:  {s.strftime('%d.%m.%Y')} (all day)"
        else:
            date_line = f"Dates: {s.strftime('%d.%m.%Y')} – {e.strftime('%d.%m.%Y')} (all day)"
        preview = f"<b>New event</b>\nTitle: {data['summary']}\n{date_line}\n"
    else:
        start = datetime.fromisoformat(data["start"])
        end = datetime.fromisoformat(data["end"])
        preview = (
            f"<b>New event</b>\n"
            f"Title: {data['summary']}\n"
            f"Start: {start.strftime('%d.%m.%Y %H:%M')}\n"
            f"End:   {end.strftime('%H:%M')}\n"
        )
    if desc:
        preview += f"Desc:  {desc}\n"
    preview += "\nCreate this event?"

    await state.set_state(CreateEventFSM.confirm)
    await message.answer(preview, reply_markup=confirm_kb("create"), parse_mode="HTML")


@router.callback_query(CreateEventFSM.confirm, F.data.startswith("create:"))
async def fsm_create_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    ctx = _ctx(callback)
    if ctx is None or callback.data is None:
        return
    user_id, msg = ctx

    choice = callback.data.split(":", 1)[1]
    if choice == "no":
        await state.clear()
        await msg.edit_text("❌ Cancelled.", reply_markup=await _menu_kb(user_id))
        await callback.answer()
        return

    creds = await auth_service.get_credentials(user_id)
    if creds is None:
        await callback.answer("Authentication error", show_alert=True)
        await state.clear()
        return

    calendar_id = await auth_service.get_calendar_id(user_id) or "primary"
    data = await state.get_data()
    if data.get("all_day"):
        event = EventCreate(
            summary=data["summary"],
            start_date=date_type.fromisoformat(data["start_date"]),
            end_date=date_type.fromisoformat(data["end_date"]),
            description=data.get("description"),
        )
    else:
        event = EventCreate(
            summary=data["summary"],
            start=datetime.fromisoformat(data["start"]),
            end=datetime.fromisoformat(data["end"]),
            description=data.get("description"),
        )
    try:
        created = await calendar_service.create_event(creds, event, calendar_id=calendar_id)
        await msg.edit_text(
            f"✅ Event created!\n<b>{created.summary}</b>\n{created.html_link or ''}",
            parse_mode="HTML",
        )
    except RuntimeError as exc:
        await msg.edit_text(f"❌ Error: {exc}")
    finally:
        await state.clear()
    await callback.answer()


# ── Delete event FSM ──────────────────────────────────────────────────────────


@router.callback_query(F.data == "delete_event")
async def cb_delete_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _check_auth(callback) or not await _check_calendar(callback):
        return
    ctx = _ctx(callback)
    if ctx is None:
        return
    user_id, msg = ctx

    creds = await auth_service.get_credentials(user_id)
    if creds is None:
        await callback.answer("Authentication error", show_alert=True)
        return

    calendar_id = await auth_service.get_calendar_id(user_id) or "primary"

    try:
        events = await calendar_service.list_events(creds, calendar_id=calendar_id, max_results=10)
    except RuntimeError as exc:
        await callback.answer(str(exc), show_alert=True)
        return

    if not events:
        await msg.edit_text("📭 No events to delete.", reply_markup=back_kb())
        await callback.answer()
        return

    await state.set_state(DeleteEventFSM.selecting_event)
    await msg.edit_text("🗑 Select event to delete:", reply_markup=events_kb(events, "del_pick"))
    await callback.answer()


@router.callback_query(DeleteEventFSM.selecting_event, F.data.startswith("del_pick:"))
async def fsm_delete_pick(callback: CallbackQuery, state: FSMContext) -> None:
    ctx = _ctx(callback)
    if ctx is None or callback.data is None:
        return
    _, msg = ctx

    event_id = callback.data.split(":", 1)[1]
    await state.update_data(event_id=event_id)
    await state.set_state(DeleteEventFSM.confirm)
    await msg.edit_text(
        f"🗑 Delete this event?\n<code>{event_id}</code>\n\nThis cannot be undone.",
        reply_markup=confirm_kb("del"),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(DeleteEventFSM.confirm, F.data.startswith("del:"))
async def fsm_delete_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    ctx = _ctx(callback)
    if ctx is None or callback.data is None:
        return
    user_id, msg = ctx

    choice = callback.data.split(":", 1)[1]
    if choice == "no":
        await state.clear()
        await msg.edit_text("❌ Cancelled.", reply_markup=await _menu_kb(user_id))
        await callback.answer()
        return

    creds = await auth_service.get_credentials(user_id)
    if creds is None:
        await callback.answer("Authentication error", show_alert=True)
        await state.clear()
        return

    calendar_id = await auth_service.get_calendar_id(user_id) or "primary"
    data = await state.get_data()
    try:
        await calendar_service.delete_event(creds, data["event_id"], calendar_id=calendar_id)
        await msg.edit_text("✅ Event deleted.")
    except RuntimeError as exc:
        await msg.edit_text(f"❌ Error: {exc}")
    finally:
        await state.clear()
    await callback.answer()


# ── Update event FSM ──────────────────────────────────────────────────────────


@router.callback_query(F.data == "update_event")
async def cb_update_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _check_auth(callback) or not await _check_calendar(callback):
        return
    ctx = _ctx(callback)
    if ctx is None:
        return
    user_id, msg = ctx

    creds = await auth_service.get_credentials(user_id)
    if creds is None:
        await callback.answer("Authentication error", show_alert=True)
        return

    calendar_id = await auth_service.get_calendar_id(user_id) or "primary"

    try:
        events = await calendar_service.list_events(creds, calendar_id=calendar_id, max_results=10)
    except RuntimeError as exc:
        await callback.answer(str(exc), show_alert=True)
        return

    if not events:
        await msg.edit_text("📭 No events to update.", reply_markup=back_kb())
        await callback.answer()
        return

    await state.set_state(UpdateEventFSM.selecting_event)
    await msg.edit_text("✏️ Select event to update:", reply_markup=events_kb(events, "upd_pick"))
    await callback.answer()


@router.callback_query(UpdateEventFSM.selecting_event, F.data.startswith("upd_pick:"))
async def fsm_update_pick(callback: CallbackQuery, state: FSMContext) -> None:
    ctx = _ctx(callback)
    if ctx is None or callback.data is None:
        return
    _, msg = ctx

    event_id = callback.data.split(":", 1)[1]
    await state.update_data(event_id=event_id)
    await state.set_state(UpdateEventFSM.selecting_field)
    await msg.edit_text("✏️ Which field to update?", reply_markup=update_field_kb())
    await callback.answer()


@router.callback_query(UpdateEventFSM.selecting_field, F.data.startswith("update_field:"))
async def fsm_update_field_pick(callback: CallbackQuery, state: FSMContext) -> None:
    ctx = _ctx(callback)
    if ctx is None or callback.data is None:
        return
    _, msg = ctx

    field = callback.data.split(":", 1)[1]
    await state.update_data(field=field)
    await state.set_state(UpdateEventFSM.waiting_for_value)
    prompts = {
        "summary": "📝 Enter new title:",
        "start": "🕐 New start time (DD.MM.YYYY HH:MM):",
        "end": "🕑 New end time (DD.MM.YYYY HH:MM):",
        "description": "📋 New description:",
        "location": "📍 New location:",
    }
    await msg.edit_text(prompts.get(field, "Enter new value:"), reply_markup=back_kb())
    await callback.answer()


@router.message(UpdateEventFSM.waiting_for_value)
async def fsm_update_value(message: Message, state: FSMContext) -> None:
    if message.text is None:
        return
    data = await state.get_data()
    field = data["field"]
    raw = message.text.strip()

    if field in ("start", "end"):
        try:
            user_id = message.from_user.id  # type: ignore[union-attr]
            tz = ZoneInfo(await auth_service.get_user_timezone(user_id))
            parsed = datetime.strptime(raw, "%d.%m.%Y %H:%M").replace(tzinfo=tz)
        except ValueError:
            await message.answer(
                "⚠️ Use format DD.MM.YYYY HH:MM (e.g. 25.12.2025 14:30):",
                reply_markup=back_kb(),
            )
            return
        await state.update_data({field: parsed.isoformat()})
    else:
        await state.update_data({field: raw})

    await state.set_state(UpdateEventFSM.confirm)
    await message.answer(
        f"✏️ Set <b>{field}</b> to: {raw}?",
        reply_markup=confirm_kb("upd"),
        parse_mode="HTML",
    )


@router.callback_query(UpdateEventFSM.confirm, F.data.startswith("upd:"))
async def fsm_update_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    ctx = _ctx(callback)
    if ctx is None or callback.data is None:
        return
    user_id, msg = ctx

    choice = callback.data.split(":", 1)[1]
    if choice == "no":
        await state.clear()
        await msg.edit_text("❌ Cancelled.", reply_markup=await _menu_kb(user_id))
        await callback.answer()
        return

    creds = await auth_service.get_credentials(user_id)
    if creds is None:
        await callback.answer("Authentication error", show_alert=True)
        await state.clear()
        return

    calendar_id = await auth_service.get_calendar_id(user_id) or "primary"
    data = await state.get_data()
    field = data["field"]
    kwargs: dict = {"event_id": data["event_id"]}
    if field in ("start", "end"):
        kwargs[field] = datetime.fromisoformat(data[field])
    else:
        kwargs[field] = data[field]

    try:
        updated = await calendar_service.update_event(
            creds, EventUpdate(**kwargs), calendar_id=calendar_id
        )
        await msg.edit_text(f"✅ Updated: <b>{updated.summary}</b>", parse_mode="HTML")
    except RuntimeError as exc:
        await msg.edit_text(f"❌ Error: {exc}")
    finally:
        await state.clear()
    await callback.answer()
