"""Outgoing messages that must survive Telegram hiccups: tickets and receipts for moderation."""

import asyncio
import logging
from datetime import UTC, datetime

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup, Message
from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import EventStatus, Order, Payment, PaymentStatus, Ticket, TicketStatus, User, UserRole
from app.formatting import esc, fmt_dt, fmt_money
from app.i18n import t
from app.services import app_settings
from app.services.ticket_image import TicketView, render_ticket
from app.services.tickets import checkin_link

log = logging.getLogger(__name__)
MAX_DELIVERY_ATTEMPTS = 10


async def _mark_blocked(session: AsyncSession, tg_id: int) -> None:
    await session.execute(update(User).where(User.tg_id == tg_id).values(bot_blocked=True))
    await session.commit()


async def safe_send(bot: Bot, session: AsyncSession, chat_id: int, text: str, **kwargs) -> bool:
    """Send a message; a user who blocked the bot is flagged instead of raising."""
    for attempt in range(2):
        try:
            await bot.send_message(chat_id, text, **kwargs)
            return True
        except TelegramRetryAfter as e:
            if attempt:
                break
            await asyncio.sleep(e.retry_after)
        except TelegramForbiddenError:
            await _mark_blocked(session, chat_id)
            return False
        except Exception as e:  # network errors included: callers must not die on a notification
            log.warning("send_message to %s failed: %s", chat_id, e)
            return False
    return False


async def admin_tg_ids(session: AsyncSession, settings: Settings) -> list[int]:
    db_admins = (await session.execute(select(User.tg_id).where(User.role == UserRole.ADMIN))).scalars()
    return list(dict.fromkeys([*settings.admin_ids, *db_admins]))


async def alert_admins(bot: Bot, session: AsyncSession, settings: Settings, text: str, **kwargs) -> None:
    for tg_id in await admin_tg_ids(session, settings):
        await safe_send(bot, session, tg_id, text, **kwargs)


async def send_ticket(
    bot: Bot, session: AsyncSession, settings: Settings, ticket: Ticket, chat_id: int | None = None, lang: str | None = None
) -> Message:
    """Send the ticket image to its owner, or to `chat_id` (rendered once, then reused by file_id). Commits."""
    user, event = ticket.user, ticket.event
    lang = lang or user.lang or settings.default_lang
    target = chat_id or user.tg_id
    caption = t(lang, "ticket_caption", title=esc(event.loc("title", lang)), when=fmt_dt(event.starts_at, settings.tz, lang), code=ticket.code)
    if ticket.image_file_id:
        msg = await bot.send_photo(target, ticket.image_file_id, caption=caption)
    else:
        me = await bot.me()
        view = TicketView(
            event_title=event.loc("title", lang),
            date_line=fmt_dt(event.starts_at, settings.tz, lang),
            venue_line=event.loc("venue", lang),
            ticket_type=ticket.ticket_type.loc("name", lang),
            holder=ticket.holder_name,
            code=ticket.code,
            qr_payload=checkin_link(me.username, ticket),
            order_line=t(lang, "ticket_img_order", order_id=ticket.order_id),
            labels={"holder": t(lang, "ticket_img_holder"), "type": t(lang, "ticket_img_type"), "footer": t(lang, "ticket_img_footer")},
        )
        png = await asyncio.to_thread(render_ticket, view, settings.ticket_font_path)
        msg = await bot.send_photo(target, BufferedInputFile(png, filename=f"ticket-{ticket.code}.png"), caption=caption)
        ticket.image_file_id = msg.photo[-1].file_id
    if ticket.delivered_at is None:
        ticket.delivered_at = datetime.now(UTC)
    await session.commit()
    return msg


async def deliver_tickets(bot: Bot, session: AsyncSession, settings: Settings, tickets: list[Ticket]) -> int:
    """Send issued tickets. A failure is counted per ticket and the loop moves on;
    the worker retries undelivered tickets and alerts admins when one keeps failing."""
    sent = 0
    for ticket in tickets:
        if ticket.delivered_at is not None or ticket.status == TicketStatus.CANCELLED:
            continue
        try:
            await send_ticket(bot, session, settings, ticket)
            sent += 1
            continue
        except TelegramForbiddenError:
            await _mark_blocked(session, ticket.user.tg_id)
            continue
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
        except SQLAlchemyError:
            # The session is unusable now and every loaded object is expired: stop, retry later.
            log.exception("ticket %s: database error during delivery", ticket.id)
            await session.rollback()
            return sent
        except Exception:
            log.exception("ticket %s delivery failed", ticket.id)
        ticket.delivery_attempts += 1
        await session.commit()
        if ticket.delivery_attempts == MAX_DELIVERY_ATTEMPTS:
            await alert_admins(bot, session, settings, t(settings.default_lang, "adm_alert_ticket_stuck", code=ticket.code, order_id=ticket.order_id))
    return sent


async def undelivered_tickets(session: AsyncSession, older_than: datetime, limit: int = 50) -> list[Ticket]:
    stmt = (
        select(Ticket)
        .join(User, Ticket.user_id == User.id)
        .where(
            Ticket.delivered_at.is_(None),
            Ticket.status != TicketStatus.CANCELLED,
            Ticket.delivery_attempts < MAX_DELIVERY_ATTEMPTS,
            Ticket.created_at < older_than,
            User.bot_blocked.is_(False),
        )
        .order_by(Ticket.delivery_attempts, Ticket.id)
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars())


def review_caption(order: Order, payment: Payment, settings: Settings, duplicates: list[int], max_attempts: int) -> str:
    lang = settings.default_lang
    user, event, tt = order.user, order.event, order.ticket_type
    card = f"•••• {order.card_last4}" if order.card_last4 else "—"
    lines = [
        t(lang, "mod_title", order_id=order.id),
        t(lang, "mod_event", title=esc(event.loc("title", lang)[:80]), when=fmt_dt(event.starts_at, settings.tz, lang)),
        t(lang, "mod_ticket", type=esc(tt.loc("name", lang)), qty=order.quantity),
        t(lang, "mod_amount", amount=fmt_money(payment.amount_tiyin, lang), card=card),
        t(
            lang,
            "mod_buyer",
            name=esc(user.full_name[:64]),
            username=f"@{esc(user.username)}" if user.username else "—",
            phone=esc(user.phone or "—"),
            tg_id=user.tg_id,
        ),
        t(lang, "mod_attempt", n=order.proof_attempts, max=max_attempts),
    ]
    # Warnings, most important first: the tail is dropped if the caption gets too long.
    if event.status == EventStatus.CANCELLED:
        lines.append(t(lang, "mod_event_cancelled"))
    elif not order.seats_held:
        lines.append(t(lang, "mod_no_seats"))
    if duplicates:
        lines.append(t(lang, "mod_duplicate", orders=", ".join(f"#{d}" for d in duplicates[:5])))
    if order.unit_price_tiyin != tt.price_tiyin:
        lines.append(t(lang, "mod_price_changed", old=fmt_money(order.unit_price_tiyin, lang), new=fmt_money(tt.price_tiyin, lang)))
    submitted = payment.created_at or datetime.now(UTC)
    if order.expires_at and order.expires_at < submitted:
        lines.append(t(lang, "mod_late", minutes=int((submitted - order.expires_at).total_seconds() // 60)))
    # Captions are capped at 1024 chars; leave room for the decision line added later.
    while len("\n".join(lines)) > 900 and len(lines) > 6:
        lines.pop()
    return "\n".join(lines)


async def post_for_review(
    bot: Bot, session: AsyncSession, settings: Settings, payment: Payment, caption: str, markup: InlineKeyboardMarkup
) -> bool:
    """Post a receipt to the moderation chat; if that fails, to admins' DMs.

    The first successful message is recorded on the payment. Payments without one are
    retried by the worker, so a receipt is never silently lost.
    """
    chat_id, _ = await app_settings.get_moderation_chat(session, settings)
    targets = ([chat_id] if chat_id else []) + await admin_tg_ids(session, settings)
    for target in targets:
        try:
            if payment.proof_kind == "photo":
                msg = await bot.send_photo(target, payment.proof_file_id, caption=caption, reply_markup=markup)
            else:
                msg = await bot.send_document(target, payment.proof_file_id, caption=caption, reply_markup=markup)
        except Exception:
            log.exception("could not post payment %s to %s", payment.id, target)
            continue
        if target != chat_id and chat_id:
            log.warning("payment %s posted to admin %s instead of moderation chat %s", payment.id, target, chat_id)
        payment.review_chat_id = target
        payment.review_message_id = msg.message_id
        await session.commit()
        return True
    return False


async def unposted_payments(session: AsyncSession, older_than: datetime, limit: int = 20) -> list[Payment]:
    stmt = (
        select(Payment)
        .where(Payment.status == PaymentStatus.SUBMITTED, Payment.review_message_id.is_(None), Payment.created_at < older_than)
        .order_by(Payment.id)
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars())
