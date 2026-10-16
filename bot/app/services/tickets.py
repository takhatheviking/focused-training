import secrets
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Order, Ticket, TicketStatus, User

# No 0/O, 1/I/L: the code is read out loud and typed by hand at the door.
CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
CHECKIN_PREFIX = "t_"


def generate_code() -> str:
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def generate_token() -> str:
    # 24 url-safe chars (~144 bits); fits Telegram's 64-char deep-link limit with the prefix.
    return secrets.token_urlsafe(18)


def checkin_link(bot_username: str, ticket: Ticket) -> str:
    return f"https://t.me/{bot_username}?start={CHECKIN_PREFIX}{ticket.token}"


async def issue_tickets(session: AsyncSession, order: Order, holder_name: str | None = None) -> list[Ticket]:
    user = order.user or await session.get(User, order.user_id)
    tickets = [
        Ticket(
            order_id=order.id,
            event_id=order.event_id,
            ticket_type_id=order.ticket_type_id,
            user_id=order.user_id,
            code=generate_code(),
            token=generate_token(),
            holder_name=(holder_name or user.full_name)[:255],
            status=TicketStatus.VALID,
        )
        for _ in range(order.quantity)
    ]
    session.add_all(tickets)
    await session.flush()
    return tickets


@dataclass
class CheckInResult:
    status: str  # ok | already_used | cancelled | not_found
    ticket: Ticket | None = None


def normalize_code(text: str) -> str:
    cleaned = "".join(ch for ch in text.upper() if ch.isalnum())
    return f"{cleaned[:4]}-{cleaned[4:]}" if len(cleaned) == 8 else text.strip().upper()


async def find_ticket(session: AsyncSession, *, token: str | None = None, code: str | None = None) -> Ticket | None:
    cond = []
    if token:
        cond.append(Ticket.token == token)
    if code:
        cond.append(Ticket.code == normalize_code(code))
    if not cond:
        return None
    return (await session.execute(select(Ticket).where(or_(*cond)))).scalar_one_or_none()


async def check_in(session: AsyncSession, *, ticket_id: int, staff: User) -> CheckInResult:
    stmt = (
        select(Ticket)
        .where(Ticket.id == ticket_id)
        .with_for_update(of=Ticket)
        .execution_options(populate_existing=True)
    )
    ticket = (await session.execute(stmt)).scalar_one_or_none()
    if ticket is None:
        return CheckInResult("not_found")
    if ticket.status == TicketStatus.CANCELLED:
        return CheckInResult("cancelled", ticket)
    if ticket.status == TicketStatus.USED:
        return CheckInResult("already_used", ticket)
    ticket.status = TicketStatus.USED
    ticket.checked_in_at = datetime.now(UTC)
    ticket.checked_in_by_id = staff.id
    return CheckInResult("ok", ticket)
