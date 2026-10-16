from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import BookCb, EventsCb, OrderCb, QtyCb, TypeCb
from app.bot.keyboards import confirm_booking_kb, contact_kb, main_menu, pending_order_kb, review_kb
from app.bot.states import BookingSG
from app.config import Settings
from app.db.models import Event, EventStatus, Order, OrderStatus, TicketType, User
from app.formatting import esc, fmt_card, fmt_dt, fmt_money
from app.i18n import Translator
from app.services import app_settings
from app.services import events as events_svc
from app.services.booking import (
    BookingError,
    accepts_late_receipt,
    cancel_order,
    create_order,
    get_active_order,
    now_utc,
    submit_proof,
)
from app.services.notify import deliver_tickets, post_for_review, review_caption

router = Router(name="booking")
router.message.filter(F.chat.type == "private")

MAX_RECEIPT_BYTES = 20 * 1024 * 1024
# Photos and PDF receipts from banking apps; no SVG/HTML or other active content.
RECEIPT_MIME_TYPES = {"image/jpeg", "image/png", "image/webp", "image/heic", "application/pdf"}
Receipt = tuple[str, str, str]  # file_id, file_unique_id, kind


def is_bookable(event: Event | None) -> bool:
    return event is not None and event.status == EventStatus.PUBLISHED and event.starts_at > now_utc()


async def send_payment_instructions(message: Message, order: Order, tr: Translator, session: AsyncSession, settings: Settings) -> None:
    card = await app_settings.get_card(session, settings)
    if card is None:
        await message.answer(tr("err_payments_not_configured"))
        return
    extra = []
    if card.holder:
        extra.append(tr("pay_card_holder", holder=esc(card.holder)))
    if card.bank:
        extra.append(tr("pay_card_bank", bank=esc(card.bank)))
    until = order.expires_at.astimezone(settings.tz).strftime("%H:%M") if order.expires_at else "—"
    text = tr(
        "pay_instructions",
        order_id=order.id,
        title=esc(order.event.loc("title", tr.lang)),
        type=esc(order.ticket_type.loc("name", tr.lang)),
        qty=order.quantity,
        amount=fmt_money(order.amount_tiyin, tr.lang),
        card=fmt_card(card.number),
        card_extra="\n".join(extra),
        until=until,
    )
    await message.answer(text, reply_markup=pending_order_kb(tr, order.id))


async def show_order(message: Message, order: Order, *, tr: Translator, session: AsyncSession, settings: Settings, state: FSMContext) -> None:
    if order.status == OrderStatus.PENDING:
        await send_payment_instructions(message, order, tr, session, settings)
        await state.set_state(BookingSG.waiting_receipt)
        await state.update_data(order_id=order.id)
        return
    key = {
        OrderStatus.UNDER_REVIEW: "order_under_review",
        OrderStatus.PAID: "order_paid",
        OrderStatus.REFUND_REQUIRED: "order_refund_required",
        OrderStatus.REFUNDED: "order_refunded",
    }.get(OrderStatus(order.status), "order_closed")
    await message.answer(tr(key, order_id=order.id, title=esc(order.event.loc("title", tr.lang))))


async def ask_quantity(message: Message, tt: TicketType, *, user: User, tr: Translator, settings: Settings, state: FSMContext) -> None:
    max_qty = min(settings.max_tickets_per_order, tt.available)
    if max_qty <= 1:
        await proceed_to_summary(message, tt, 1, user=user, tr=tr, settings=settings, state=state)
        return
    b = InlineKeyboardBuilder()
    for n in range(1, max_qty + 1):
        b.button(text=str(n), callback_data=QtyCb(ticket_type_id=tt.id, qty=n))
    b.adjust(5)
    price = fmt_money(tt.price_tiyin, tr.lang) if tt.price_tiyin else tr("price_free")
    await message.answer(tr("ask_quantity", type=esc(tt.loc("name", tr.lang)), price=price), reply_markup=b.as_markup())


async def proceed_to_summary(message: Message, tt: TicketType, qty: int, *, user: User, tr: Translator, settings: Settings, state: FSMContext) -> None:
    if not user.phone:
        await state.set_state(BookingSG.waiting_contact)
        await state.update_data(tt=tt.id, qty=qty)
        await message.answer(tr("ask_contact"), reply_markup=contact_kb(tr))
        return
    event = tt.event
    common = dict(
        title=esc(event.loc("title", tr.lang)),
        when=fmt_dt(event.starts_at, settings.tz, tr.lang),
        type=esc(tt.loc("name", tr.lang)),
        qty=qty,
    )
    if tt.price_tiyin:
        text = tr("booking_summary", amount=fmt_money(tt.price_tiyin * qty, tr.lang), minutes=settings.hold_minutes, **common)
    else:
        text = tr("booking_summary_free", **common)
    await message.answer(text, reply_markup=confirm_booking_kb(tr, tt.id, qty))


@router.callback_query(EventsCb.filter(F.action == "buy"))
async def cb_buy(
    cb: CallbackQuery, callback_data: EventsCb, user: User, tr: Translator, session: AsyncSession, settings: Settings, state: FSMContext
) -> None:
    event = await session.get(Event, callback_data.event_id)
    if not is_bookable(event):
        await cb.answer(tr("err_event_unavailable"), show_alert=True)
        return
    active = await get_active_order(session, user.id, event.id)
    if active is not None:
        await cb.answer()
        await show_order(cb.message, active, tr=tr, session=session, settings=settings, state=state)
        return
    types = [tt for tt in event.ticket_types if tt.is_active and tt.available > 0]
    if not types:
        await cb.answer(tr("sold_out"), show_alert=True)
        return
    if any(tt.price_tiyin for tt in types) and await app_settings.get_card(session, settings) is None:
        await cb.answer(tr("err_payments_not_configured"), show_alert=True)
        return
    await cb.answer()
    if len(types) == 1:
        await ask_quantity(cb.message, types[0], user=user, tr=tr, settings=settings, state=state)
        return
    b = InlineKeyboardBuilder()
    for tt in types:
        price = fmt_money(tt.price_tiyin, tr.lang) if tt.price_tiyin else tr("price_free")
        b.button(text=f"{tt.loc('name', tr.lang)} — {price} · {tr('left_short', n=tt.available)}"[:64], callback_data=TypeCb(ticket_type_id=tt.id))
    b.adjust(1)
    await cb.message.answer(tr("choose_ticket_type"), reply_markup=b.as_markup())


@router.callback_query(TypeCb.filter())
async def cb_type(cb: CallbackQuery, callback_data: TypeCb, user: User, tr: Translator, session: AsyncSession, settings: Settings, state: FSMContext) -> None:
    tt = await session.get(TicketType, callback_data.ticket_type_id)
    if tt is None or not is_bookable(tt.event):
        await cb.answer(tr("err_event_unavailable"), show_alert=True)
        return
    if not tt.is_active or tt.available <= 0:
        await cb.answer(tr("sold_out"), show_alert=True)
        return
    await cb.answer()
    await ask_quantity(cb.message, tt, user=user, tr=tr, settings=settings, state=state)


@router.callback_query(QtyCb.filter())
async def cb_qty(cb: CallbackQuery, callback_data: QtyCb, user: User, tr: Translator, session: AsyncSession, settings: Settings, state: FSMContext) -> None:
    tt = await session.get(TicketType, callback_data.ticket_type_id)
    if tt is None or not is_bookable(tt.event):
        await cb.answer(tr("err_event_unavailable"), show_alert=True)
        return
    if not tt.is_active or tt.available < callback_data.qty:
        await cb.answer(tr("err_not_enough_seats"), show_alert=True)
        return
    await cb.answer()
    await cb.message.edit_reply_markup(reply_markup=None)
    await proceed_to_summary(cb.message, tt, callback_data.qty, user=user, tr=tr, settings=settings, state=state)


@router.message(BookingSG.waiting_contact, F.contact)
async def on_contact(message: Message, user: User, tr: Translator, session: AsyncSession, settings: Settings, state: FSMContext) -> None:
    if message.contact.user_id != message.from_user.id:
        await message.answer(tr("err_foreign_contact"))
        return
    phone = message.contact.phone_number
    user.phone = phone if phone.startswith("+") else f"+{phone}"
    await session.commit()
    data = await state.get_data()
    await state.clear()
    await message.answer(tr("contact_saved"), reply_markup=main_menu(tr, user))
    tt = await session.get(TicketType, data.get("tt", 0))
    if tt is None:
        return
    await proceed_to_summary(message, tt, data.get("qty", 1), user=user, tr=tr, settings=settings, state=state)


@router.message(BookingSG.waiting_contact)
async def on_contact_other(message: Message, tr: Translator) -> None:
    await message.answer(tr("ask_contact"), reply_markup=contact_kb(tr))


@router.callback_query(BookCb.filter(F.action == "abort"))
async def cb_abort(cb: CallbackQuery, tr: Translator) -> None:
    await cb.answer()
    await cb.message.edit_text(tr("booking_aborted"))


@router.callback_query(BookCb.filter(F.action == "confirm"))
async def cb_confirm(
    cb: CallbackQuery,
    callback_data: BookCb,
    user: User,
    tr: Translator,
    session: AsyncSession,
    settings: Settings,
    state: FSMContext,
    bot: Bot,
) -> None:
    tt = await session.get(TicketType, callback_data.ticket_type_id)
    card = await app_settings.get_card(session, settings)
    if tt is not None and tt.price_tiyin and card is None:
        await cb.answer(tr("err_payments_not_configured"), show_alert=True)
        return
    try:
        order, tickets = await create_order(
            session,
            user=user,
            ticket_type_id=callback_data.ticket_type_id,
            quantity=callback_data.qty,
            hold_minutes=settings.hold_minutes,
            max_per_order=settings.max_tickets_per_order,
            card_last4=card.number[-4:] if card else None,
            unique_amounts=settings.unique_amounts,
        )
    except BookingError as e:
        if e.key == "err_active_order_exists" and e.order is not None:
            await cb.answer()
            await show_order(cb.message, e.order, tr=tr, session=session, settings=settings, state=state)
        else:
            await cb.answer(tr(e.key, **e.kwargs), show_alert=True)
        return
    await session.commit()
    await cb.answer()
    await cb.message.edit_reply_markup(reply_markup=None)
    if tickets:
        await cb.message.answer(tr("registered_free", title=esc(order.event.loc("title", tr.lang))))
        await deliver_tickets(bot, session, settings, tickets)
        return
    await show_order(cb.message, order, tr=tr, session=session, settings=settings, state=state)


@router.callback_query(OrderCb.filter())
async def cb_order(
    cb: CallbackQuery,
    callback_data: OrderCb,
    user: User,
    tr: Translator,
    session: AsyncSession,
    settings: Settings,
    state: FSMContext,
    bot: Bot,
) -> None:
    order = await session.get(Order, callback_data.order_id)
    if order is None or order.user_id != user.id:
        await cb.answer(tr("err_order_not_found"), show_alert=True)
        return
    if callback_data.action == "cancel":
        try:
            order = await cancel_order(session, order_id=order.id, user_id=user.id)
        except BookingError as e:
            await cb.answer(tr(e.key, **e.kwargs), show_alert=True)
            return
        await session.commit()
        await state.clear()
        await cb.answer()
        await cb.message.edit_text(tr("booking_cancelled", order_id=order.id))
        return
    await cb.answer()
    action = callback_data.action
    if action == "attach":
        # The user confirmed which booking a loose screenshot belongs to.
        receipt = (await state.get_data()).get("loose_receipt")
        await cb.message.edit_reply_markup(reply_markup=None)
        if receipt:
            await process_receipt(cb.message, order.id, tuple(receipt), user=user, tr=tr, session=session, settings=settings, state=state, bot=bot)
            return
        action = "proof"  # the stored screenshot is gone (e.g. restart): ask for it again
    if action == "proof" and (order.status == OrderStatus.PENDING or accepts_late_receipt(order)):
        await state.set_state(BookingSG.waiting_receipt)
        await state.update_data(order_id=order.id)
        await cb.message.answer(tr("send_receipt_now", order_id=order.id))
        return
    await show_order(cb.message, order, tr=tr, session=session, settings=settings, state=state)


async def extract_receipt(message: Message, tr: Translator) -> Receipt | None:
    if message.photo:
        photo = message.photo[-1]
        return photo.file_id, photo.file_unique_id, "photo"
    doc = message.document
    if doc is None or (doc.mime_type or "") not in RECEIPT_MIME_TYPES or (doc.file_size or 0) > MAX_RECEIPT_BYTES:
        await message.answer(tr("err_receipt_format"))
        return None
    return doc.file_id, doc.file_unique_id, "document"


async def process_receipt(
    message: Message,
    order_id: int,
    receipt: Receipt,
    *,
    user: User,
    tr: Translator,
    session: AsyncSession,
    settings: Settings,
    state: FSMContext,
    bot: Bot,
) -> None:
    file_id, unique_id, kind = receipt
    try:
        order, payment, duplicates = await submit_proof(
            session, order_id=order_id, user_id=user.id, file_id=file_id, file_unique_id=unique_id, kind=kind
        )
    except BookingError as e:
        await state.clear()
        await message.answer(tr(e.key, **{**e.kwargs, "order_id": e.order.id if e.order else order_id}))
        return
    await session.commit()
    await state.clear()
    await message.answer(tr("receipt_received", order_id=order.id), reply_markup=main_menu(tr, user))
    caption = review_caption(order, payment, settings, duplicates, settings.max_proof_attempts)
    await post_for_review(bot, session, settings, payment, caption, review_kb(payment.id, settings.default_lang))


@router.message(BookingSG.waiting_receipt, F.photo | F.document)
async def on_receipt(message: Message, user: User, tr: Translator, session: AsyncSession, settings: Settings, state: FSMContext, bot: Bot) -> None:
    order_id = (await state.get_data()).get("order_id")
    if not order_id:
        await on_receipt_without_state(message, user, tr, session, settings, state, bot)
        return
    receipt = await extract_receipt(message, tr)
    if receipt:
        await process_receipt(message, order_id, receipt, user=user, tr=tr, session=session, settings=settings, state=state, bot=bot)


@router.message(StateFilter(None), F.photo | F.document)
async def on_receipt_without_state(message: Message, user: User, tr: Translator, session: AsyncSession, settings: Settings, state: FSMContext, bot: Bot) -> None:
    """A screenshot arrived outside the flow. It is attached automatically only to the single
    booking waiting for payment; otherwise the user picks the booking with a button."""
    receipt = await extract_receipt(message, tr)
    if receipt is None:
        return
    open_orders = await events_svc.user_open_orders(session, user.id)
    pending = [o for o in open_orders if o.status == OrderStatus.PENDING]
    if len(pending) == 1:
        await process_receipt(message, pending[0].id, receipt, user=user, tr=tr, session=session, settings=settings, state=state, bot=bot)
        return
    # Paid after the hold expired or after cancelling: accepted, but only once the user confirms.
    candidates = pending or await events_svc.recent_closed_orders(session, user.id)
    if not candidates:
        reviewing = [o for o in open_orders if o.status == OrderStatus.UNDER_REVIEW]
        if reviewing:
            await message.answer(tr("order_under_review", order_id=reviewing[0].id))
        else:
            await message.answer(tr("receipt_no_order"))
        return
    await state.update_data(loose_receipt=list(receipt))
    b = InlineKeyboardBuilder()
    for o in candidates[:10]:
        b.button(text=f"№{o.id} · {o.event.loc('title', tr.lang)}"[:60], callback_data=OrderCb(action="attach", order_id=o.id))
    b.adjust(1)
    await message.answer(tr("receipt_choose_order"), reply_markup=b.as_markup())
