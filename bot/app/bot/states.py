from aiogram.fsm.state import State, StatesGroup


class BookingSG(StatesGroup):
    waiting_contact = State()
    waiting_receipt = State()


class CheckInSG(StatesGroup):
    waiting_code = State()


class EventCreateSG(StatesGroup):
    form = State()  # waiting for the filled one-message form
    poster = State()
    confirm = State()


class EventEditSG(StatesGroup):
    form = State()
    value = State()


class TicketTypeSG(StatesGroup):
    name = State()
    price = State()
    capacity = State()


class CardSG(StatesGroup):
    number = State()
    holder = State()
    bank = State()


class BroadcastSG(StatesGroup):
    text = State()
    confirm = State()


class IssueSG(StatesGroup):
    recipient = State()
    confirm = State()


class GSheetSG(StatesGroup):
    link = State()


class StaffSG(StatesGroup):
    pick = State()
