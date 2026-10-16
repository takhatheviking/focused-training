from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    CancelReason,
    PaymentMethod,
    Event,
    EventStatus,
    Order,
    OrderStatus,
    Ticket,
    TicketStatus,
    TicketType,
    User,
)


def _now() -> datetime:
    return datetime.now(UTC)


async def list_upcoming(session: AsyncSession, *, offset: int, limit: int) -> tuple[list[Event], int]:
    cond = (Event.status == EventStatus.PUBLISHED, Event.starts_at > _now())
    total = await session.scalar(select(func.count()).select_from(Event).where(*cond))
    stmt = select(Event).where(*cond).order_by(Event.starts_at).offset(offset).limit(limit)
    return list((await session.execute(stmt)).scalars()), total or 0


async def list_for_admin(session: AsyncSession, *, offset: int, limit: int) -> tuple[list[Event], int]:
    # Upcoming first, then the last month of past events; cancelled ones are hidden.
    cond = (Event.status != EventStatus.CANCELLED, Event.starts_at > _now() - timedelta(days=30))
    total = await session.scalar(select(func.count()).select_from(Event).where(*cond))
    upcoming_first = case((Event.starts_at > _now(), 0), else_=1)
    stmt = select(Event).where(*cond).order_by(upcoming_first, Event.starts_at).offset(offset).limit(limit)
    return list((await session.execute(stmt)).scalars()), total or 0


@dataclass
class TypeStats:
    ticket_type: TicketType
    sold: int  # bought (paid or free registration)
    held: int  # seats in bookings that are not paid yet
    revenue_tiyin: int
    issued: int = 0  # handed out by admins (guests, speakers, cash)


@dataclass
class EventStats:
    types: list[TypeStats]
    checked_in: int
    awaiting_review: int
    refunds_pending: int

    @property
    def sold(self) -> int:
        return sum(s.sold for s in self.types)

    @property
    def issued(self) -> int:
        return sum(s.issued for s in self.types)

    @property
    def revenue_tiyin(self) -> int:
        return sum(s.revenue_tiyin for s in self.types)


async def event_stats(session: AsyncSession, event: Event) -> EventStats:
    paid = (
        select(Order.ticket_type_id, Order.payment_method, func.sum(Order.quantity), func.sum(Order.amount_tiyin))
        .where(Order.event_id == event.id, Order.status == OrderStatus.PAID)
        .group_by(Order.ticket_type_id, Order.payment_method)
    )
    counts: dict[tuple[int, bool], tuple[int, int]] = {}
    for tt_id, method, q, a in (await session.execute(paid)).all():
        key = (tt_id, method == PaymentMethod.MANUAL)
        prev_q, prev_a = counts.get(key, (0, 0))
        counts[key] = (prev_q + int(q or 0), prev_a + int(a or 0))
    types = []
    for tt in event.ticket_types:
        sold, revenue = counts.get((tt.id, False), (0, 0))
        issued = counts.get((tt.id, True), (0, 0))[0]
        types.append(TypeStats(tt, sold, max(tt.reserved - sold - issued, 0), revenue, issued))

    async def count_orders(status: OrderStatus) -> int:
        q = select(func.count()).select_from(Order).where(Order.event_id == event.id, Order.status == status)
        return int(await session.scalar(q) or 0)

    checked_in = await session.scalar(
        select(func.count()).select_from(Ticket).where(Ticket.event_id == event.id, Ticket.status == TicketStatus.USED)
    )
    return EventStats(
        types=types,
        checked_in=int(checked_in or 0),
        awaiting_review=await count_orders(OrderStatus.UNDER_REVIEW),
        refunds_pending=await count_orders(OrderStatus.REFUND_REQUIRED),
    )


async def user_tickets(session: AsyncSession, user_id: int) -> list[Ticket]:
    stmt = (
        select(Ticket)
        .join(Event, Ticket.event_id == Event.id)
        .where(
            Ticket.user_id == user_id,
            Ticket.status != TicketStatus.CANCELLED,
            Event.starts_at > _now() - timedelta(days=2),
            # Tickets an admin issued for a guest without Telegram are not the admin's own.
            ~Ticket.order_id.in_(select(Order.id).where(Order.guest_name.is_not(None), Order.user_id == user_id)),
        )
        .order_by(Event.starts_at, Ticket.id)
    )
    return list((await session.execute(stmt)).scalars())


async def user_open_orders(session: AsyncSession, user_id: int) -> list[Order]:
    stmt = (
        select(Order)
        .where(
            Order.user_id == user_id,
            Order.status.in_((OrderStatus.PENDING, OrderStatus.UNDER_REVIEW, OrderStatus.REFUND_REQUIRED)),
        )
        .order_by(Order.created_at.desc())
    )
    return list((await session.execute(stmt)).scalars())


async def recent_closed_orders(session: AsyncSession, user_id: int, hours: int = 24) -> list[Order]:
    """Bookings that ended without payment recently; a receipt for them is still accepted."""
    stmt = (
        select(Order)
        .where(
            Order.user_id == user_id,
            or_(
                Order.status == OrderStatus.EXPIRED,
                and_(Order.status == OrderStatus.CANCELLED, Order.cancel_reason.in_((CancelReason.USER, CancelReason.EVENT))),
            ),
            Order.updated_at > _now() - timedelta(hours=hours),
        )
        .order_by(Order.updated_at.desc())
        .limit(10)
    )
    return list((await session.execute(stmt)).scalars())


async def participants(session: AsyncSession, event_id: int) -> list[Ticket]:
    stmt = (
        select(Ticket)
        .where(Ticket.event_id == event_id, Ticket.status != TicketStatus.CANCELLED)
        .order_by(Ticket.ticket_type_id, Ticket.id)
    )
    return list((await session.execute(stmt)).scalars())


async def ticket_holders(session: AsyncSession, event_id: int) -> list[tuple[int, str | None]]:
    """(tg_id, language) of everyone holding a ticket for the event."""
    stmt = (
        select(User.tg_id, User.lang)
        .join(Ticket, Ticket.user_id == User.id)
        .where(Ticket.event_id == event_id, Ticket.status != TicketStatus.CANCELLED, User.bot_blocked.is_(False))
        .distinct()
    )
    return [(tg_id, lang) for tg_id, lang in (await session.execute(stmt)).all()]


async def global_stats(session: AsyncSession) -> dict[str, int]:
    users = await session.scalar(select(func.count()).select_from(User))
    paid_orders = await session.scalar(
        select(func.count()).select_from(Order).where(Order.status == OrderStatus.PAID)
    )
    tickets = await session.scalar(
        select(func.count()).select_from(Ticket).where(Ticket.status != TicketStatus.CANCELLED)
    )
    revenue = await session.scalar(
        select(func.coalesce(func.sum(Order.amount_tiyin), 0)).where(Order.status == OrderStatus.PAID)
    )
    review = await session.scalar(
        select(func.count()).select_from(Order).where(Order.status == OrderStatus.UNDER_REVIEW)
    )
    upcoming = await session.scalar(
        select(func.count()).select_from(Event).where(Event.status == EventStatus.PUBLISHED, Event.starts_at > _now())
    )
    return {
        "users": int(users or 0),
        "paid_orders": int(paid_orders or 0),
        "tickets": int(tickets or 0),
        "revenue": int(revenue or 0),
        "review": int(review or 0),
        "upcoming": int(upcoming or 0),
    }
