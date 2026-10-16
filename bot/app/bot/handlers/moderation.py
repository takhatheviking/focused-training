"""Receipt moderation. Receipts are posted to the moderation chat (a private channel or
group) with Approve / Reject buttons; admins' DMs are the fallback destination.

Who may press the buttons: users with the admin role, or administrators of the
moderation chat itself. Nobody may decide on their own order."""

import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, ChatMemberUpdated, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import AdminCb, ModCb
from app.bot.keyboards import REJECT_REASONS, pending_order_kb, refund_kb, reject_reasons_kb, review_kb
from app.config import Settings
from app.db.models import OrderStatus, Payment, User, UserRole
from app.formatting import esc
from app.i18n import Translator, t
from app.services import app_settings
from app.services.booking import BookingError, approve_payment, mark_refunded, now_utc, reject_payment
from app.services.notify import deliver_tickets, safe_send

log = logging.getLogger(__name__)
router = Router(name="moderation")
CAPTION_LIMIT = 1024


async def is_moderator(bot: Bot, session: AsyncSession, settings: Settings, cb: CallbackQuery, user: User | None) -> bool:
    if user is not None and user.role == UserRole.ADMIN:
        return True
    chat_id, _ = await app_settings.get_moderation_chat(session, settings)
    if chat_id is None or cb.message is None or cb.message.chat.id != chat_id:
        return False
    try:
        member = await bot.get_chat_member(chat_id, cb.from_user.id)
    except TelegramBadRequest:
        return False
    return member.status in ("creator", "administrator")


async def _answer(cb: CallbackQuery, text: str | None = None, alert: bool = False) -> None:
    # After a restart with a backlog the query may be too old to answer; the decision still stands.
    try:
        await cb.answer(text, show_alert=alert)
    except TelegramBadRequest:
        pass


async def _finish(cb: CallbackQuery, line: str, markup: InlineKeyboardMarkup | None = None) -> None:
    text = f"{cb.message.html_text}\n\n{line}"
    try:
        if cb.message.photo or cb.message.document:
            await cb.message.edit_caption(caption=text if len(text) <= CAPTION_LIMIT else line, reply_markup=markup)
        else:
            await cb.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        log.warning("could not edit moderation message: %s", e)


@router.callback_query(ModCb.filter())
async def on_moderation(cb: CallbackQuery, callback_data: ModCb, session: AsyncSession, settings: Settings, bot: Bot, user: User | None = None) -> None:
    lang = settings.default_lang
    if not await is_moderator(bot, session, settings, cb, user):
        await _answer(cb, t(lang, "err_no_rights"), alert=True)
        return

    action, payment_id = callback_data.action, callback_data.payment_id
    if action == "no":
        await _answer(cb)
        await cb.message.edit_reply_markup(reply_markup=reject_reasons_kb(payment_id, lang))
        return
    if action == "back":
        await _answer(cb)
        await cb.message.edit_reply_markup(reply_markup=review_kb(payment_id, lang))
        return
    if action == "reason" and callback_data.reason not in REJECT_REASONS:
        await _answer(cb)
        return

    payment = await session.get(Payment, payment_id)
    if payment is None:
        await _answer(cb, t(lang, "err_payment_not_found"), alert=True)
        return
    if payment.order.user.tg_id == cb.from_user.id:
        await _answer(cb, t(lang, "err_own_order"), alert=True)
        return

    moderator = cb.from_user.full_name + (f" (@{cb.from_user.username})" if cb.from_user.username else "")
    at = now_utc().astimezone(settings.tz).strftime("%d.%m %H:%M")
    try:
        if action == "ok":
            result = await approve_payment(session, payment_id=payment_id, moderator_tg_id=cb.from_user.id, moderator_name=moderator)
        elif action == "reason":
            result = await reject_payment(
                session,
                payment_id=payment_id,
                moderator_tg_id=cb.from_user.id,
                moderator_name=moderator,
                reason=callback_data.reason,
                hold_minutes=settings.hold_minutes,
                max_attempts=settings.max_proof_attempts,
            )
        elif action == "refunded":
            order = await mark_refunded(session, order_id=payment.order_id, by_tg_id=cb.from_user.id)
            await session.commit()
            await _answer(cb, t(lang, "mod_done"))
            await _finish(cb, t(lang, "mod_result_refunded", by=esc(moderator), at=at))
            ul = order.user.lang or lang
            await safe_send(bot, session, order.user.tg_id, t(ul, "refund_done", order_id=order.id))
            return
        else:
            await _answer(cb)
            return
    except BookingError as e:
        await _answer(cb, t(lang, e.key, **e.kwargs), alert=True)
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        return

    await session.commit()
    await _answer(cb, t(lang, "mod_done"))
    order = result.order
    customer = order.user
    ul = customer.lang or lang

    if action == "ok" and order.status == OrderStatus.PAID:
        await _finish(cb, t(lang, "mod_result_approved", by=esc(moderator), at=at))
        await safe_send(bot, session, customer.tg_id, t(ul, "payment_approved", order_id=order.id, title=esc(order.event.loc("title", ul))))
        await deliver_tickets(bot, session, settings, result.tickets)
    elif action == "ok":
        await _finish(cb, t(lang, "mod_result_refund_required", by=esc(moderator), at=at), refund_kb(payment_id, lang))
        key = "payment_approved_event_cancelled" if order.event.status == "cancelled" else "payment_approved_no_seats"
        await safe_send(bot, session, customer.tg_id, t(ul, key, order_id=order.id, title=esc(order.event.loc("title", ul))))
    else:
        reason_mod = t(lang, f"reject_{callback_data.reason}")
        reason_user = t(ul, f"reject_{callback_data.reason}")
        await _finish(cb, t(lang, "mod_result_rejected", reason=reason_mod, by=esc(moderator), at=at))
        if result.final:
            support = f"@{settings.support_username}" if settings.support_username else "—"
            await safe_send(bot, session, customer.tg_id, t(ul, "payment_rejected_final", order_id=order.id, reason=reason_user, support=support))
        else:
            until = order.expires_at.astimezone(settings.tz).strftime("%H:%M")
            await safe_send(
                bot,
                session,
                customer.tg_id,
                t(ul, "payment_rejected_retry", order_id=order.id, reason=reason_user, until=until),
                reply_markup=pending_order_kb(Translator(ul), order.id),
            )


# The bot was added to a group or channel: offer it as the moderation chat to the super-admin who did it.
@router.my_chat_member(F.chat.type.in_({"group", "supergroup", "channel"}))
async def on_added_to_chat(event: ChatMemberUpdated, session: AsyncSession, settings: Settings, bot: Bot, user: User | None = None) -> None:
    status = event.new_chat_member.status
    if status != "administrator" and not (status == "member" and event.chat.type != "channel"):
        return
    if user is None or user.tg_id not in settings.admin_ids:
        return
    tr = Translator(user.lang or settings.default_lang)
    b = InlineKeyboardBuilder()
    b.button(text=tr("adm_modchat_use"), callback_data=AdminCb(action="set_modchat", value=str(event.chat.id)))
    await safe_send(bot, session, user.tg_id, tr("adm_modchat_offer", title=esc(event.chat.title or str(event.chat.id))), reply_markup=b.as_markup())
