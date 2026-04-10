"""Tests for FSM state groups."""

from app.bot.states import (
    AIConfirmFSM,
    CreateEventFSM,
    DeleteEventFSM,
    SelectCalendarFSM,
    SetTimezoneFSM,
    UpdateEventFSM,
)


class TestStatesExist:
    """Verify all FSM states are defined and accessible."""

    def test_create_event_states(self):
        assert CreateEventFSM.waiting_for_title is not None
        assert CreateEventFSM.waiting_for_start_date is not None
        assert CreateEventFSM.waiting_for_start_time is not None
        assert CreateEventFSM.waiting_for_end_time is not None
        assert CreateEventFSM.waiting_for_end_date is not None
        assert CreateEventFSM.waiting_for_description is not None
        assert CreateEventFSM.confirm is not None

    def test_update_event_states(self):
        assert UpdateEventFSM.selecting_event is not None
        assert UpdateEventFSM.selecting_field is not None
        assert UpdateEventFSM.waiting_for_value is not None
        assert UpdateEventFSM.confirm is not None

    def test_delete_event_states(self):
        assert DeleteEventFSM.selecting_event is not None
        assert DeleteEventFSM.confirm is not None

    def test_select_calendar_state(self):
        assert SelectCalendarFSM.selecting is not None

    def test_ai_confirm_state(self):
        assert AIConfirmFSM.waiting is not None

    def test_timezone_state(self):
        assert SetTimezoneFSM.waiting_for_input is not None
