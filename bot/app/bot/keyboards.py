from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot.callbacks import AdminCb, BookCb, EventsCb, LangCb, ModCb, OrderCb
from app.db.models import User, UserRole
from app.i18n import LANG_NAMES, Translator, t

REJECT_REASONS = ("amount", "unreadable", "not_found", "other")

remove_kb = ReplyKeyboardRemove()


def main_menu(tr: Translator, user: User | None) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=tr("btn_events")), KeyboardButton(text=tr("btn_my"))],
        [KeyboardButton(text=tr("btn_lang")), KeyboardButton(text=tr("btn_help"))],
    ]
    if user is not None and user.role in (UserRole.STAFF, UserRole.ADMIN):
        staff_row = [KeyboardButton(text=tr("btn_checkin"))]
        if user.role == UserRole.ADMIN:
            staff_row.insert(0, KeyboardButton(text=tr("btn_admin")))
        rows.append(staff_row)
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def lang_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for code, name in LANG_NAMES.items():
        b.button(text=name, callback_data=LangCb(lang=code))
    b.adjust(1)
    return b.as_markup()


def contact_kb(tr: Translator) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=tr("btn_share_contact"), request_contact=True)],
            [KeyboardButton(text=tr("btn_cancel"))],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def back_to_events(tr: Translator, page: int = 0) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=tr("btn_back"), callback_data=EventsCb(action="list", page=page).pack())


def confirm_booking_kb(tr: Translator, ticket_type_id: int, qty: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=tr("btn_confirm_booking"), callback_data=BookCb(action="confirm", ticket_type_id=ticket_type_id, qty=qty))
    b.button(text=tr("btn_cancel"), callback_data=BookCb(action="abort"))
    b.adjust(1)
    return b.as_markup()


def pending_order_kb(tr: Translator, order_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=tr("btn_send_receipt"), callback_data=OrderCb(action="proof", order_id=order_id))
    b.button(text=tr("btn_cancel_booking"), callback_data=OrderCb(action="cancel", order_id=order_id))
    b.adjust(1)
    return b.as_markup()


def expired_order_kb(tr: Translator, order_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=tr("btn_already_paid"), callback_data=OrderCb(action="proof", order_id=order_id))
    return b.as_markup()


def review_kb(payment_id: int, lang: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=t(lang, "mod_btn_approve"), callback_data=ModCb(action="ok", payment_id=payment_id))
    b.button(text=t(lang, "mod_btn_reject"), callback_data=ModCb(action="no", payment_id=payment_id))
    b.adjust(2)
    return b.as_markup()


def reject_reasons_kb(payment_id: int, lang: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for reason in REJECT_REASONS:
        b.button(text=t(lang, f"reject_btn_{reason}"), callback_data=ModCb(action="reason", payment_id=payment_id, reason=reason))
    b.button(text=t(lang, "btn_back"), callback_data=ModCb(action="back", payment_id=payment_id))
    b.adjust(1)
    return b.as_markup()


def refund_kb(payment_id: int, lang: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=t(lang, "mod_btn_refunded"), callback_data=ModCb(action="refunded", payment_id=payment_id))
    return b.as_markup()


def admin_menu_kb(tr: Translator) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=tr("adm_btn_events"), callback_data=AdminCb(action="events"))
    b.button(text=tr("adm_btn_new_event"), callback_data=AdminCb(action="new_event"))
    b.button(text=tr("adm_btn_reviews"), callback_data=AdminCb(action="reviews"))
    b.button(text=tr("adm_btn_refunds"), callback_data=AdminCb(action="refunds"))
    b.button(text=tr("adm_btn_card"), callback_data=AdminCb(action="card"))
    b.button(text=tr("adm_btn_modchat"), callback_data=AdminCb(action="modchat"))
    b.button(text=tr("adm_btn_staff"), callback_data=AdminCb(action="staff"))
    b.button(text=tr("adm_btn_gsheet"), callback_data=AdminCb(action="gsheet"))
    b.button(text=tr("adm_btn_stats"), callback_data=AdminCb(action="stats"))
    b.adjust(2)
    return b.as_markup()


def admin_back(tr: Translator, action: str = "menu", event_id: int = 0) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=tr("btn_back"), callback_data=AdminCb(action=action, event_id=event_id).pack())
