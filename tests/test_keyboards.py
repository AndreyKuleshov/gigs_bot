"""Tests for keyboard builders."""

from datetime import UTC, datetime

from app.bot.keyboards import (
    back_kb,
    calendars_kb,
    confirm_kb,
    events_kb,
    main_menu_kb,
    start_time_kb,
    timezone_kb,
    update_field_kb,
)
from app.services.calendar_service import CalendarRead, EventRead


class TestMainMenuKb:
    def test_no_calendar_no_timezone_shows_only_select_buttons(self):
        kb = main_menu_kb()
        buttons = [btn.text for row in kb.inline_keyboard for btn in row]
        assert "Select calendar" in buttons[0]
        assert "Set timezone" in buttons[1]
        # Should NOT have action buttons
        assert not any("List events" in b for b in buttons)

    def test_with_calendar_shows_action_buttons(self):
        kb = main_menu_kb(calendar_name="My Cal")
        buttons = [btn.text for row in kb.inline_keyboard for btn in row]
        assert any("List events" in b for b in buttons)
        assert any("Create event" in b for b in buttons)
        assert any("Update event" in b for b in buttons)
        assert any("Delete event" in b for b in buttons)

    def test_calendar_name_displayed(self):
        kb = main_menu_kb(calendar_name="Work")
        buttons = [btn.text for row in kb.inline_keyboard for btn in row]
        assert any("Work" in b for b in buttons)

    def test_timezone_displayed(self):
        kb = main_menu_kb(timezone="Europe/Belgrade")
        buttons = [btn.text for row in kb.inline_keyboard for btn in row]
        assert any("Europe/Belgrade" in b for b in buttons)

    def test_no_timezone_shows_set_prompt(self):
        kb = main_menu_kb(calendar_name="Cal")
        buttons = [btn.text for row in kb.inline_keyboard for btn in row]
        assert any("Set timezone" in b for b in buttons)


class TestCalendarsKb:
    def test_buttons_per_calendar(self):
        cals = [
            CalendarRead(calendar_id="a@g.com", name="Cal A", primary=True),
            CalendarRead(calendar_id="b@g.com", name="Cal B"),
        ]
        kb = calendars_kb(cals)
        texts = [btn.text for row in kb.inline_keyboard for btn in row]
        assert texts[0].startswith("★")
        assert "Cal B" in texts[1]
        assert texts[-1] == "🔙 Back"

    def test_long_name_truncated(self):
        cals = [CalendarRead(calendar_id="x", name="A" * 100)]
        kb = calendars_kb(cals)
        label = kb.inline_keyboard[0][0].text
        assert len(label) <= 42  # 40 chars + possible "★ " prefix


class TestEventsKb:
    def test_event_buttons(self):
        events = [
            EventRead(
                event_id="e1",
                summary="Meeting",
                start=datetime(2025, 6, 1, 14, 0, tzinfo=UTC),
                end=datetime(2025, 6, 1, 15, 0, tzinfo=UTC),
            ),
        ]
        kb = events_kb(events, "del_pick")
        btn = kb.inline_keyboard[0][0]
        assert "Meeting" in btn.text
        assert btn.callback_data == "del_pick:e1"
        assert kb.inline_keyboard[-1][0].text == "🔙 Back"


class TestConfirmKb:
    def test_yes_no_buttons(self):
        kb = confirm_kb("create")
        texts = [btn.text for row in kb.inline_keyboard for btn in row]
        cbs = [btn.callback_data for row in kb.inline_keyboard for btn in row]
        assert "✅ Yes" in texts
        assert "❌ No" in texts
        assert "create:yes" in cbs
        assert "create:no" in cbs


class TestTimezoneKb:
    def test_has_popular_zones_and_custom(self):
        kb = timezone_kb()
        texts = [btn.text for row in kb.inline_keyboard for btn in row]
        cbs = [btn.callback_data for row in kb.inline_keyboard for btn in row]
        # Should have Moscow, Belgrade, custom option, and back
        assert any("Москва" in t for t in texts)
        assert any("Белград" in t for t in texts)
        assert any("tz_custom" in (c or "") for c in cbs)
        assert any("main_menu" in (c or "") for c in cbs)


class TestMiscKbs:
    def test_back_kb(self):
        kb = back_kb()
        assert kb.inline_keyboard[0][0].callback_data == "main_menu"

    def test_start_time_kb(self):
        kb = start_time_kb()
        cbs = [btn.callback_data for row in kb.inline_keyboard for btn in row]
        assert "create_all_day" in cbs
        assert "main_menu" in cbs

    def test_update_field_kb(self):
        kb = update_field_kb()
        cbs = [btn.callback_data for row in kb.inline_keyboard for btn in row]
        assert "update_field:summary" in cbs
        assert "update_field:start" in cbs
        assert "update_field:location" in cbs
