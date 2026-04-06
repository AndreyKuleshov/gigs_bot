from aiogram.fsm.state import State, StatesGroup


class CreateEventFSM(StatesGroup):
    waiting_for_title = State()
    waiting_for_start_date = State()
    waiting_for_start_time = State()
    waiting_for_end_time = State()
    waiting_for_description = State()
    confirm = State()


class UpdateEventFSM(StatesGroup):
    selecting_event = State()
    selecting_field = State()
    waiting_for_value = State()
    confirm = State()


class DeleteEventFSM(StatesGroup):
    selecting_event = State()
    confirm = State()
