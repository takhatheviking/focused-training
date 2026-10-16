from aiogram.filters.callback_data import CallbackData


class LangCb(CallbackData, prefix="lg"):
    lang: str


class EventsCb(CallbackData, prefix="ev"):
    action: str  # list | open | buy
    event_id: int = 0
    page: int = 0


class TypeCb(CallbackData, prefix="tt"):
    ticket_type_id: int


class QtyCb(CallbackData, prefix="qt"):
    ticket_type_id: int
    qty: int


class BookCb(CallbackData, prefix="bk"):
    action: str  # confirm | abort
    ticket_type_id: int = 0
    qty: int = 0


class OrderCb(CallbackData, prefix="or"):
    action: str  # show | cancel | proof
    order_id: int


class MyCb(CallbackData, prefix="my"):
    action: str  # ticket | list
    item_id: int = 0


class ModCb(CallbackData, prefix="md"):
    action: str  # ok | no | reason | back | refunded
    payment_id: int
    reason: str = ""


class CheckInCb(CallbackData, prefix="ci"):
    token: str  # the QR secret, so staff cannot check in arbitrary tickets by id


class AdminCb(CallbackData, prefix="ad"):
    action: str
    event_id: int = 0
    item_id: int = 0
    value: str = ""
