import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from app.db.models import EventStatus, Order, OrderStatus, PaymentStatus, Ticket, TicketStatus, TicketType
from app.services.booking import (
    BookingError,
    approve_payment,
    cancel_event,
    cancel_order,
    create_order,
    expire_due_orders,
    now_utc,
    reject_payment,
    submit_proof,
    unique_amount,
    issue_manual,
)
from app.services.tickets import check_in, find_ticket
from app.services.events import event_stats, user_tickets
from tests.conftest import make_event, make_user

HOLD, MAX_QTY = 30, 5


async def book(session, user, tt, qty=1):
    order, tickets = await create_order(session, user=user, ticket_type_id=tt.id, quantity=qty, hold_minutes=HOLD, max_per_order=MAX_QTY)
    await session.commit()
    return order, tickets


async def receipt(session, order, unique="file-1"):
    result = await submit_proof(session, order_id=order.id, user_id=order.user_id, file_id=f"id-{unique}", file_unique_id=unique, kind="photo")
    await session.commit()
    return result


async def approve(session, payment):
    result = await approve_payment(session, payment_id=payment.id, moderator_tg_id=1, moderator_name="Mod")
    await session.commit()
    return result


async def reserved(session, tt) -> int:
    await session.refresh(tt)
    return tt.reserved


async def expire_now(session, order):
    await session.execute(update(Order).where(Order.id == order.id).values(expires_at=now_utc() - timedelta(minutes=1)))
    await session.commit()
    expired = await expire_due_orders(session)
    await session.commit()
    return expired


async def test_booking_holds_seats_and_blocks_oversell(session):
    _, tt = await make_event(session, capacity=3)
    a, b = await make_user(session, 1), await make_user(session, 2)
    order, tickets = await book(session, a, tt, qty=2)
    assert order.status == OrderStatus.PENDING and order.seats_held and not tickets
    assert order.amount_tiyin == unique_amount(2 * tt.price_tiyin, order.id) < 2 * tt.price_tiyin
    assert await reserved(session, tt) == 2
    with pytest.raises(BookingError) as e:
        await book(session, b, tt, qty=2)
    assert e.value.key == "err_sold_out"
    await session.rollback()
    assert await reserved(session, tt) == 2


async def test_parallel_buyers_never_exceed_capacity(factory):
    async with factory() as s:
        _, tt = await make_event(s, capacity=5)
        users = [await make_user(s, 100 + i) for i in range(20)]

    async def attempt(user):
        async with factory() as s:
            try:
                await book(s, user, tt)
                return True
            except BookingError:
                return False

    results = await asyncio.gather(*(attempt(u) for u in users))
    assert sum(results) == 5
    async with factory() as s:
        assert (await s.get(TicketType, tt.id)).reserved == 5


async def test_double_tap_creates_one_order(factory):
    async with factory() as s:
        _, tt = await make_event(s, capacity=10)
        user = await make_user(s, 1)

    async def attempt():
        async with factory() as s:
            try:
                await book(s, user, tt)
                return "ok"
            except BookingError as e:
                return e.key

    results = await asyncio.gather(*(attempt() for _ in range(5)))
    assert results.count("ok") == 1
    assert set(results) - {"ok"} == {"err_active_order_exists"}
    async with factory() as s:
        assert (await s.get(TicketType, tt.id)).reserved == 1


async def test_happy_path_receipt_approve_issues_tickets(session):
    _, tt = await make_event(session)
    user = await make_user(session, 1)
    order, _ = await book(session, user, tt, qty=3)
    order, payment, dups = await receipt(session, order)
    assert order.status == OrderStatus.UNDER_REVIEW and payment.status == PaymentStatus.SUBMITTED and dups == []

    result = await approve(session, payment)
    assert result.order.status == OrderStatus.PAID
    assert len(result.tickets) == 3
    assert len({t.code for t in result.tickets}) == 3
    assert all(t.status == TicketStatus.VALID for t in result.tickets)
    assert await reserved(session, tt) == 3


async def test_approve_is_idempotent_under_concurrency(factory):
    async with factory() as s:
        _, tt = await make_event(s)
        user = await make_user(s, 1)
        order, _ = await book(s, user, tt, qty=2)
        _, payment, _ = await receipt(s, order)

    async def press():
        async with factory() as s:
            try:
                r = await approve(s, payment)
                return len(r.tickets)
            except BookingError as e:
                return e.key

    results = await asyncio.gather(*(press() for _ in range(4)))
    assert results.count(2) == 1
    assert results.count("err_already_reviewed") == 3


async def test_hold_expiry_releases_seats(session):
    _, tt = await make_event(session, capacity=2)
    user = await make_user(session, 1)
    order, _ = await book(session, user, tt, qty=2)
    expired = await expire_now(session, order)
    assert [o.id for o in expired] == [order.id]
    assert expired[0].status == OrderStatus.EXPIRED
    assert await reserved(session, tt) == 0
    # Running the job again must not release the seats twice.
    assert await expire_due_orders(session) == []
    assert await reserved(session, tt) == 0


async def test_late_receipt_reacquires_free_seats(session):
    _, tt = await make_event(session, capacity=2)
    user = await make_user(session, 1)
    order, _ = await book(session, user, tt)
    await expire_now(session, order)
    order, payment, _ = await receipt(session, order)
    assert order.status == OrderStatus.UNDER_REVIEW and order.seats_held
    result = await approve(session, payment)
    assert result.order.status == OrderStatus.PAID and len(result.tickets) == 1


async def test_late_receipt_when_sold_out_goes_to_refund(session):
    _, tt = await make_event(session, capacity=1)
    late, early = await make_user(session, 1), await make_user(session, 2)
    order, _ = await book(session, late, tt)
    await expire_now(session, order)
    await book(session, early, tt)  # takes the freed seat

    order, payment, _ = await receipt(session, order)
    assert order.status == OrderStatus.UNDER_REVIEW and not order.seats_held
    result = await approve(session, payment)
    assert result.order.status == OrderStatus.REFUND_REQUIRED and not result.tickets
    assert await reserved(session, tt) == 1


async def test_reject_gives_new_hold_then_cancels_after_max_attempts(session):
    _, tt = await make_event(session, capacity=5)
    user = await make_user(session, 1)
    order, _ = await book(session, user, tt, qty=2)
    for attempt in range(1, 4):
        order, payment, _ = await receipt(session, order, unique=f"f{attempt}")
        result = await reject_payment(
            session, payment_id=payment.id, moderator_tg_id=1, moderator_name="Mod", reason="amount", hold_minutes=HOLD, max_attempts=3
        )
        await session.commit()
        if attempt < 3:
            assert result.order.status == OrderStatus.PENDING and not result.final
            assert result.order.expires_at > now_utc()
            assert await reserved(session, tt) == 2
    assert result.final and result.order.status == OrderStatus.CANCELLED
    assert await reserved(session, tt) == 0


async def test_reused_screenshot_is_flagged(session):
    _, tt = await make_event(session)
    a, b = await make_user(session, 1), await make_user(session, 2)
    oa, _ = await book(session, a, tt)
    ob, _ = await book(session, b, tt)
    await receipt(session, oa, unique="same-file")
    _, _, dups = await receipt(session, ob, unique="same-file")
    assert dups == [oa.id]


async def test_user_cancel_only_before_receipt(session):
    _, tt = await make_event(session)
    user = await make_user(session, 1)
    order, _ = await book(session, user, tt, qty=2)
    await cancel_order(session, order_id=order.id, user_id=user.id)
    await session.commit()
    assert await reserved(session, tt) == 0

    order, _ = await book(session, user, tt)
    await receipt(session, order)
    with pytest.raises(BookingError) as e:
        await cancel_order(session, order_id=order.id, user_id=user.id)
    assert e.value.key == "err_cannot_cancel"


async def test_free_event_issues_tickets_immediately(session):
    _, tt = await make_event(session, price_tiyin=0)
    user = await make_user(session, 1)
    order, tickets = await book(session, user, tt, qty=2)
    assert order.status == OrderStatus.PAID and len(tickets) == 2


async def test_check_in_once(session):
    _, tt = await make_event(session, price_tiyin=0)
    user, staff = await make_user(session, 1), await make_user(session, 2, role="staff")
    _, tickets = await book(session, user, tt)
    ticket = await find_ticket(session, code=tickets[0].code.replace("-", "").lower())
    assert ticket is not None
    first = await check_in(session, ticket_id=ticket.id, staff=staff)
    await session.commit()
    second = await check_in(session, ticket_id=ticket.id, staff=staff)
    assert (first.status, second.status) == ("ok", "already_used")


async def test_cancel_event_flags_refunds_and_voids_tickets(session):
    event, tt = await make_event(session)
    paid_user, pending_user = await make_user(session, 1), await make_user(session, 2)
    order, _ = await book(session, paid_user, tt)
    _, payment, _ = await receipt(session, order)
    await approve(session, payment)
    pending, _ = await book(session, pending_user, tt)

    result = await cancel_event(session, event_id=event.id)
    await session.commit()
    assert [o.id for o in result.refund_orders] == [order.id]
    assert {o.id for o in result.notify_orders} == {order.id, pending.id}
    await session.refresh(event)
    assert event.status == EventStatus.CANCELLED
    assert await reserved(session, tt) == 0
    statuses = (await session.execute(select(Ticket.status).where(Ticket.order_id == order.id))).scalars().all()
    assert statuses == [TicketStatus.CANCELLED]
    with pytest.raises(BookingError) as e:
        await book(session, pending_user, tt)
    assert e.value.key == "err_event_unavailable"


async def test_unique_amounts_differ_per_order(session):
    _, tt = await make_event(session, price_tiyin=15_000_000)
    amounts = set()
    for uid in range(1, 6):
        order, _ = await book(session, await make_user(session, uid), tt)
        amounts.add(order.amount_tiyin)
        assert 15_000_000 - 99 * 100 <= order.amount_tiyin < 15_000_000
    assert len(amounts) == 5
    assert unique_amount(500_000, 7) == 500_000  # cheap tickets keep the round price


async def test_per_user_cap_across_orders(session):
    _, tt = await make_event(session, capacity=100)
    user = await make_user(session, 1)
    order, _ = await book(session, user, tt, qty=MAX_QTY)
    _, payment, _ = await receipt(session, order)
    await approve(session, payment)
    with pytest.raises(BookingError) as e:
        await book(session, user, tt, qty=1)
    assert e.value.key == "err_user_limit" and e.value.kwargs["max"] == MAX_QTY


async def test_free_event_registers_once_even_with_double_tap(factory):
    async with factory() as s:
        _, tt = await make_event(s, price_tiyin=0, capacity=100)
        user = await make_user(s, 1)

    async def tap():
        async with factory() as s:
            try:
                await book(s, user, tt, qty=2)
                return "ok"
            except BookingError as e:
                return e.key

    results = await asyncio.gather(*(tap() for _ in range(4)))
    assert results.count("ok") == 1
    assert set(results) - {"ok"} == {"err_already_registered"}
    async with factory() as s:
        assert len((await s.execute(select(Ticket))).scalars().all()) == 2


async def test_receipt_after_user_cancel_is_accepted(session):
    _, tt = await make_event(session, capacity=3)
    user = await make_user(session, 1)
    order, _ = await book(session, user, tt)
    await cancel_order(session, order_id=order.id, user_id=user.id)
    await session.commit()
    order, payment, _ = await receipt(session, order)
    assert order.status == OrderStatus.UNDER_REVIEW and order.seats_held
    assert (await approve(session, payment)).order.status == OrderStatus.PAID


async def test_rejected_after_event_cancel_is_final(session):
    event, tt = await make_event(session)
    user = await make_user(session, 1)
    order, _ = await book(session, user, tt)
    _, payment, _ = await receipt(session, order)
    result = await cancel_event(session, event_id=event.id)
    await session.commit()
    assert [o.id for o in result.review_orders] == [order.id]
    rejected = await reject_payment(
        session, payment_id=payment.id, moderator_tg_id=1, moderator_name="Mod", reason="amount", hold_minutes=HOLD, max_attempts=3
    )
    await session.commit()
    assert rejected.final and rejected.order.status == OrderStatus.CANCELLED
    assert await reserved(session, tt) == 0


async def test_approve_after_event_cancel_goes_to_refund(session):
    event, tt = await make_event(session)
    user = await make_user(session, 1)
    order, _ = await book(session, user, tt)
    _, payment, _ = await receipt(session, order)
    await cancel_event(session, event_id=event.id)
    await session.commit()
    result = await approve(session, payment)
    assert result.order.status == OrderStatus.REFUND_REQUIRED and not result.tickets


async def test_manual_issue_for_guest_and_hidden_type(session):
    event, tt = await make_event(session, capacity=2)
    admin = await make_user(session, 1, role="admin")
    tt.is_active = False  # a "Guest" quota hidden from public sale
    await session.commit()
    order, tickets = await issue_manual(
        session, ticket_type_id=tt.id, quantity=2, owner=admin, issued_by=admin, guest_name="Иван Петров", guest_phone="+998901234567"
    )
    await session.commit()
    assert order.status == OrderStatus.PAID and order.amount_tiyin == 0 and order.payment_method == "manual"
    assert [t.holder_name for t in tickets] == ["Иван Петров", "Иван Петров"]
    assert order.buyer_name == "Иван Петров" and order.buyer_phone == "+998901234567" and order.buyer_username is None
    assert await reserved(session, tt) == 2
    with pytest.raises(BookingError) as e:
        await issue_manual(session, ticket_type_id=tt.id, quantity=1, owner=admin, issued_by=admin, guest_name="Ещё гость")
    assert e.value.key == "err_sold_out"
    # The guest's tickets are not the admin's own "My tickets".
    assert await user_tickets(session, admin.id) == []
    stats = await event_stats(session, event)
    assert (stats.sold, stats.issued) == (0, 2)


async def test_manual_issue_to_telegram_user_shows_in_their_tickets(session):
    _, tt = await make_event(session)
    admin, guest = await make_user(session, 1, role="admin"), await make_user(session, 2)
    _, tickets = await issue_manual(session, ticket_type_id=tt.id, quantity=1, owner=guest, issued_by=admin)
    await session.commit()
    assert [t.id for t in await user_tickets(session, guest.id)] == [tickets[0].id]
    assert tickets[0].holder_name == guest.full_name
