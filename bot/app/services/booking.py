r"""Order lifecycle: hold seats -> receipt -> moderator decision -> tickets.

State machine (orders.status):

    pending --receipt--> under_review --approve--> paid
       |                     |  \--approve, no seats / event gone--> refund_required --> refunded
       |                     \--reject--> pending (new hold) | cancelled (attempts used up)
       |--hold timeout--> expired ------late receipt--> under_review
       \--user cancels--> cancelled ----late receipt--> under_review

Late receipts are accepted on purpose: the buyer may have paid before the hold ran out
or before pressing "cancel", and money must never be silently dropped.

Seats are counted in ticket_types.reserved. Every change goes through _acquire_seats /
_release_seats, guarded by orders.seats_held, so a seat is never taken or returned twice.
All functions expect the caller to commit. Lock order is always: event (FOR SHARE, or
FOR UPDATE in cancel_event) -> order -> payment -> ticket_types, so they cannot deadlock.
A BookingError is raised before anything is written, so callers need no rollback
(a rollback would expire every loaded object, and lazy loads fail under asyncio).
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    ACTIVE_ORDER_STATUSES,
    CancelReason,
    Event,
    EventStatus,
    Order,
    OrderStatus,
    Payment,
    PaymentMethod,
    PaymentStatus,
    Ticket,
    TicketStatus,
    TicketType,
    User,
)
from app.services.tickets import issue_tickets

# Unique amounts start from this price: below it a discount of up to 99 sum is too visible.
UNIQUE_AMOUNT_MIN_TIYIN = 10_000 * 100
LATE_RECEIPT_CANCEL_REASONS = (CancelReason.USER, CancelReason.EVENT)


class BookingError(Exception):
    """Business rule violation. `key` is an i18n key shown to the user."""

    def __init__(self, key: str, order: Order | None = None, **params):
        super().__init__(key)
        self.key = key
        self.order = order
        self.params = params

    @property
    def kwargs(self) -> dict:
        """Placeholders for the i18n text."""
        return {"order_id": self.order.id if self.order else "", **self.params}


@dataclass
class ReviewResult:
    order: Order
    payment: Payment
    tickets: list[Ticket] = field(default_factory=list)
    final: bool = False  # reject: no more attempts, order cancelled


@dataclass
class CancelledEvent:
    refund_orders: list[Order]  # paid money that has to go back
    notify_orders: list[Order]  # everyone with tickets or an open booking
    review_orders: list[Order]  # receipts still waiting for moderators


def now_utc() -> datetime:
    return datetime.now(UTC)


async def _acquire_seats(session: AsyncSession, order: Order) -> bool:
    if order.seats_held:
        return True
    result = await session.execute(
        update(TicketType)
        .where(TicketType.id == order.ticket_type_id, TicketType.reserved + order.quantity <= TicketType.capacity)
        .values(reserved=TicketType.reserved + order.quantity)
        .returning(TicketType.id)
    )
    if result.scalar_one_or_none() is None:
        return False
    order.seats_held = True
    return True


async def _release_seats(session: AsyncSession, order: Order) -> None:
    if not order.seats_held:
        return
    await session.execute(
        update(TicketType)
        .where(TicketType.id == order.ticket_type_id)
        .values(reserved=TicketType.reserved - order.quantity)
    )
    order.seats_held = False


async def lock_order(session: AsyncSession, order_id: int) -> Order | None:
    stmt = select(Order).where(Order.id == order_id).with_for_update(of=Order).execution_options(populate_existing=True)
    return (await session.execute(stmt)).scalar_one_or_none()


async def _share_lock_event(session: AsyncSession, event_id: int) -> Event | None:
    return await session.get(Event, event_id, with_for_update={"read": True}, populate_existing=True)


async def get_active_order(session: AsyncSession, user_id: int, event_id: int) -> Order | None:
    stmt = select(Order).where(
        Order.user_id == user_id, Order.event_id == event_id, Order.status.in_(ACTIVE_ORDER_STATUSES)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_free_registration(session: AsyncSession, user_id: int, event_id: int) -> Order | None:
    stmt = select(Order).where(
        Order.user_id == user_id,
        Order.event_id == event_id,
        Order.payment_method == PaymentMethod.FREE,
        Order.status == OrderStatus.PAID,
    ).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none()


async def _user_quantity(session: AsyncSession, user_id: int, event_id: int) -> int:
    stmt = select(func.coalesce(func.sum(Order.quantity), 0)).where(
        Order.user_id == user_id,
        Order.event_id == event_id,
        Order.status.in_((OrderStatus.PENDING, OrderStatus.UNDER_REVIEW, OrderStatus.PAID)),
    )
    return int(await session.scalar(stmt) or 0)


def unique_amount(base_tiyin: int, order_id: int) -> int:
    """Shave 1..99 sum off the price so each transfer can be told apart in the bank app."""
    if base_tiyin < UNIQUE_AMOUNT_MIN_TIYIN:
        return base_tiyin
    return base_tiyin - (1 + order_id % 99) * 100


async def create_order(
    session: AsyncSession,
    *,
    user: User,
    ticket_type_id: int,
    quantity: int,
    hold_minutes: int,
    max_per_order: int,
    card_last4: str | None = None,
    unique_amounts: bool = True,
) -> tuple[Order, list[Ticket]]:
    """Hold seats for a new order. Free tickets are issued at once (second item of the result)."""
    tt = await session.get(TicketType, ticket_type_id)
    if tt is None or not tt.is_active:
        raise BookingError("err_ticket_type_unavailable")
    event = await _share_lock_event(session, tt.event_id)
    if event is None or event.status != EventStatus.PUBLISHED or event.starts_at <= now_utc():
        raise BookingError("err_event_unavailable")
    if not 1 <= quantity <= max_per_order:
        raise BookingError("err_bad_quantity")

    # Serialize one user's bookings so double taps cannot slip past the checks below.
    await session.execute(select(User.id).where(User.id == user.id).with_for_update())
    existing = await get_active_order(session, user.id, event.id)
    if existing is not None:
        raise BookingError("err_active_order_exists", existing)
    is_free = tt.price_tiyin == 0
    if is_free and (registered := await get_free_registration(session, user.id, event.id)) is not None:
        raise BookingError("err_already_registered", registered)
    # A per-person cap across all orders, otherwise free events can be emptied by one account.
    if await _user_quantity(session, user.id, event.id) + quantity > max_per_order:
        raise BookingError("err_user_limit", max=max_per_order)

    order = Order(
        user_id=user.id,
        event_id=event.id,
        ticket_type_id=tt.id,
        quantity=quantity,
        unit_price_tiyin=tt.price_tiyin,
        amount_tiyin=tt.price_tiyin * quantity,
        status=OrderStatus.PAID if is_free else OrderStatus.PENDING,
        payment_method=PaymentMethod.FREE if is_free else PaymentMethod.CARD_TRANSFER,
        seats_held=False,
        expires_at=None if is_free else now_utc() + timedelta(minutes=hold_minutes),
        paid_at=now_utc() if is_free else None,
        card_last4=None if is_free else card_last4,
    )
    user_id, event_id = user.id, event.id
    try:
        # Savepoint: a failure undoes the seat hold but leaves the caller's session usable.
        async with session.begin_nested():
            if not await _acquire_seats(session, order):
                raise BookingError("err_sold_out")
            session.add(order)
            await session.flush()
    except IntegrityError:
        # Two taps raced past the check above; the unique partial index stopped the second one.
        raise BookingError("err_active_order_exists", await get_active_order(session, user_id, event_id))

    if not is_free and unique_amounts:
        order.amount_tiyin = unique_amount(order.amount_tiyin, order.id)

    tickets: list[Ticket] = []
    if is_free:
        await session.refresh(order)
        tickets = await issue_tickets(session, order)
    return order, tickets


async def cancel_order(session: AsyncSession, *, order_id: int, user_id: int) -> Order:
    order = await lock_order(session, order_id)
    if order is None or order.user_id != user_id:
        raise BookingError("err_order_not_found")
    if order.status != OrderStatus.PENDING:
        # Once a receipt is sent money may have moved, so only moderators decide.
        raise BookingError("err_cannot_cancel", order)
    order.status = OrderStatus.CANCELLED
    order.cancel_reason = CancelReason.USER
    await _release_seats(session, order)
    return order


async def expire_due_orders(session: AsyncSession, *, limit: int = 100) -> list[Order]:
    stmt = (
        select(Order)
        .where(Order.status == OrderStatus.PENDING, Order.expires_at < now_utc())
        .order_by(Order.expires_at)
        .limit(limit)
        .with_for_update(of=Order, skip_locked=True)
    )
    orders = list((await session.execute(stmt)).scalars())
    for order in orders:
        order.status = OrderStatus.EXPIRED
        await _release_seats(session, order)
    return orders


def accepts_late_receipt(order: Order) -> bool:
    return order.status == OrderStatus.EXPIRED or (
        order.status == OrderStatus.CANCELLED and order.cancel_reason in LATE_RECEIPT_CANCEL_REASONS
    )


async def find_duplicate_orders(session: AsyncSession, file_unique_id: str | None, order_id: int) -> list[int]:
    if not file_unique_id:
        return []
    stmt = (
        select(Payment.order_id)
        .where(Payment.proof_file_unique_id == file_unique_id, Payment.order_id != order_id)
        .distinct()
    )
    return list((await session.execute(stmt)).scalars())


async def submit_proof(
    session: AsyncSession,
    *,
    order_id: int,
    user_id: int,
    file_id: str,
    file_unique_id: str,
    kind: str,
) -> tuple[Order, Payment, list[int]]:
    """Attach a transfer receipt and send the order to moderation.

    Returns the order, the new payment and ids of other orders that used the same file
    (a cheap signal of a reused screenshot).
    """
    peek = await session.get(Order, order_id)
    if peek is None or peek.user_id != user_id:
        raise BookingError("err_order_not_found")
    event = await _share_lock_event(session, peek.event_id)
    order = await lock_order(session, order_id)
    if order.status == OrderStatus.UNDER_REVIEW:
        raise BookingError("err_already_under_review", order)
    if order.status == OrderStatus.PAID:
        raise BookingError("err_already_paid", order)
    late = accepts_late_receipt(order)
    if order.status != OrderStatus.PENDING and not late:
        raise BookingError("err_order_closed", order)

    if late:
        other = await get_active_order(session, user_id, order.event_id)
        if other is not None:
            raise BookingError("err_active_order_exists", other)

    try:
        async with session.begin_nested():
            if late and event.status != EventStatus.CANCELLED:
                await _acquire_seats(session, order)  # may fail; moderators see "no seats" and refund
            order.status = OrderStatus.UNDER_REVIEW
            order.proof_attempts += 1
            payment = Payment(
                order_id=order.id,
                method=PaymentMethod.CARD_TRANSFER,
                status=PaymentStatus.SUBMITTED,
                amount_tiyin=order.amount_tiyin,
                proof_file_id=file_id,
                proof_file_unique_id=file_unique_id,
                proof_kind=kind,
            )
            session.add(payment)
            await session.flush()
    except IntegrityError:
        # A new booking for the same event appeared at the same moment.
        raise BookingError("err_active_order_exists", await get_active_order(session, user_id, order.event_id))

    return order, payment, await find_duplicate_orders(session, file_unique_id, order.id)


async def _lock_for_review(session: AsyncSession, payment_id: int) -> tuple[Order, Payment, Event]:
    payment = await session.get(Payment, payment_id)
    if payment is None:
        raise BookingError("err_payment_not_found")
    event = await _share_lock_event(session, payment.order.event_id)
    order = await lock_order(session, payment.order_id)
    # OF payments: without it Postgres would also lock every eagerly joined row.
    stmt = select(Payment).where(Payment.id == payment_id).with_for_update(of=Payment).execution_options(populate_existing=True)
    payment = (await session.execute(stmt)).scalar_one()
    if payment.status != PaymentStatus.SUBMITTED or order.status != OrderStatus.UNDER_REVIEW:
        raise BookingError("err_already_reviewed", order)
    return order, payment, event


def _mark_reviewed(payment: Payment, status: PaymentStatus, moderator_tg_id: int, moderator_name: str) -> None:
    payment.status = status
    payment.reviewed_by_tg_id = moderator_tg_id
    payment.reviewed_by_name = moderator_name[:128]
    payment.reviewed_at = now_utc()


async def approve_payment(
    session: AsyncSession, *, payment_id: int, moderator_tg_id: int, moderator_name: str
) -> ReviewResult:
    order, payment, event = await _lock_for_review(session, payment_id)
    _mark_reviewed(payment, PaymentStatus.APPROVED, moderator_tg_id, moderator_name)
    # Money is in, but there is nothing to sell any more: flag for refund instead of tickets.
    # A draft (unpublished) event still honours receipts: admins hide events to stop new sales.
    if event.status == EventStatus.CANCELLED or not await _acquire_seats(session, order):
        await _release_seats(session, order)
        order.status = OrderStatus.REFUND_REQUIRED
        return ReviewResult(order, payment)
    order.status = OrderStatus.PAID
    order.paid_at = now_utc()
    tickets = await issue_tickets(session, order)
    return ReviewResult(order, payment, tickets)


async def reject_payment(
    session: AsyncSession,
    *,
    payment_id: int,
    moderator_tg_id: int,
    moderator_name: str,
    reason: str,
    hold_minutes: int,
    max_attempts: int,
) -> ReviewResult:
    order, payment, event = await _lock_for_review(session, payment_id)
    _mark_reviewed(payment, PaymentStatus.REJECTED, moderator_tg_id, moderator_name)
    payment.reject_reason = reason

    if event.status == EventStatus.CANCELLED:
        order.status, order.cancel_reason = OrderStatus.CANCELLED, CancelReason.EVENT
        await _release_seats(session, order)
        return ReviewResult(order, payment, final=True)
    if order.proof_attempts >= max_attempts or not await _acquire_seats(session, order):
        order.status, order.cancel_reason = OrderStatus.CANCELLED, CancelReason.REJECTED
        await _release_seats(session, order)
        return ReviewResult(order, payment, final=True)

    order.status = OrderStatus.PENDING
    order.expires_at = now_utc() + timedelta(minutes=hold_minutes)
    return ReviewResult(order, payment)


async def cancel_event(session: AsyncSession, *, event_id: int) -> CancelledEvent:
    """Cancel an event: drop open holds, void tickets, flag paid orders for refund.

    Orders under review stay with the moderators; approving them now leads to a refund.
    """
    event = await session.get(Event, event_id, with_for_update=True, populate_existing=True)
    if event is None:
        raise BookingError("err_event_unavailable")
    event.status = EventStatus.CANCELLED
    stmt = (
        select(Order)
        .where(Order.event_id == event_id, Order.status.in_((OrderStatus.PENDING, OrderStatus.UNDER_REVIEW, OrderStatus.PAID)))
        .with_for_update(of=Order)
    )
    result = CancelledEvent([], [], [])
    for order in (await session.execute(stmt)).scalars():
        if order.status == OrderStatus.UNDER_REVIEW:
            result.review_orders.append(order)
            continue
        if order.status == OrderStatus.PENDING:
            order.status, order.cancel_reason = OrderStatus.CANCELLED, CancelReason.EVENT
        elif order.amount_tiyin > 0:
            order.status = OrderStatus.REFUND_REQUIRED
            result.refund_orders.append(order)
        else:
            order.status, order.cancel_reason = OrderStatus.CANCELLED, CancelReason.EVENT
        await _release_seats(session, order)
        result.notify_orders.append(order)
    await session.execute(update(Ticket).where(Ticket.event_id == event_id).values(status=TicketStatus.CANCELLED))
    return result


async def issue_manual(
    session: AsyncSession,
    *,
    ticket_type_id: int,
    quantity: int,
    owner: User,
    issued_by: User,
    guest_name: str | None = None,
    guest_phone: str | None = None,
) -> tuple[Order, list[Ticket]]:
    """An admin issues tickets without payment (guest, speaker, cash).

    Takes seats like a sale, so capacity still holds. Hidden ticket types are allowed:
    that is how a "Guest" quota separate from public sales works. No per-person limit.
    """
    tt = await session.get(TicketType, ticket_type_id)
    if tt is None:
        raise BookingError("err_ticket_type_unavailable")
    event = await _share_lock_event(session, tt.event_id)
    if event is None or event.status == EventStatus.CANCELLED:
        raise BookingError("err_event_unavailable")
    order = Order(
        user_id=owner.id,
        event_id=event.id,
        ticket_type_id=tt.id,
        quantity=quantity,
        unit_price_tiyin=0,
        amount_tiyin=0,
        status=OrderStatus.PAID,
        payment_method=PaymentMethod.MANUAL,
        seats_held=False,
        paid_at=now_utc(),
        issued_by_id=issued_by.id,
        guest_name=guest_name,
        guest_phone=guest_phone,
    )
    async with session.begin_nested():
        if not await _acquire_seats(session, order):
            raise BookingError("err_sold_out")
        session.add(order)
        await session.flush()
    await session.refresh(order)
    tickets = await issue_tickets(session, order, holder_name=guest_name or owner.full_name)
    return order, tickets


async def approved_payment_id(session: AsyncSession, order_id: int) -> int | None:
    stmt = select(Payment.id).where(Payment.order_id == order_id, Payment.status == PaymentStatus.APPROVED).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none()


async def mark_refunded(session: AsyncSession, *, order_id: int, by_tg_id: int) -> Order:
    order = await lock_order(session, order_id)
    if order is None:
        raise BookingError("err_order_not_found")
    if order.status != OrderStatus.REFUND_REQUIRED:
        raise BookingError("err_already_reviewed", order)
    order.status = OrderStatus.REFUNDED
    order.refunded_at = now_utc()
    order.refunded_by_tg_id = by_tg_id
    return order


async def pending_reviews(session: AsyncSession, *, older_than: datetime | None = None, limit: int = 20) -> list[Payment]:
    stmt = select(Payment).where(Payment.status == PaymentStatus.SUBMITTED)
    if older_than is not None:
        stmt = stmt.where(Payment.created_at < older_than)
    return list((await session.execute(stmt.order_by(Payment.id).limit(limit))).scalars())


async def refunds_pending(session: AsyncSession, *, limit: int = 20) -> list[Order]:
    stmt = select(Order).where(Order.status == OrderStatus.REFUND_REQUIRED).order_by(Order.updated_at).limit(limit)
    return list((await session.execute(stmt)).scalars())
