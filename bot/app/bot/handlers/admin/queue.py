"""Admin: receipts waiting for a decision and refunds waiting to be paid, straight from the DB.

Moderation messages can get lost (deleted, chat switched, bot removed); these lists are the
source of truth and let an admin re-post a receipt or close a refund."""

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import AdminCb
from app.bot.keyboards import admin_back, review_kb
from app.config import Settings
from app.db.models import Payment, User
from app.formatting import esc, fmt_money
from app.i18n import Translator, t
from app.services.booking import (
    BookingError,
    find_duplicate_orders,
    mark_refunded,
    pending_reviews,
    refunds_pending,
)
from app.services.notify import post_for_review, review_caption, safe_send

router = Router(name="admin_queue")


@router.callback_query(AdminCb.filter(F.action == "reviews"))
async def cb_reviews(cb: CallbackQuery, tr: Translator, session: AsyncSession, settings: Settings) -> None:
    payments = await pending_reviews(session)
    b = InlineKeyboardBuilder()
    for p in payments:
        local = p.created_at.astimezone(settings.tz)
        b.button(
            text=f"🔁 №{p.order_id} · {fmt_money(p.amount_tiyin, tr.lang)} · {local:%d.%m %H:%M}"[:64],
            callback_data=AdminCb(action="repost", item_id=p.id),
        )
    b.adjust(1)
    b.row(admin_back(tr))
    await cb.answer()
    await cb.message.edit_text(tr("adm_reviews_title", n=len(payments)) if payments else tr("adm_reviews_empty"), reply_markup=b.as_markup())


@router.callback_query(AdminCb.filter(F.action == "repost"))
async def cb_repost(cb: CallbackQuery, callback_data: AdminCb, tr: Translator, session: AsyncSession, settings: Settings, bot: Bot) -> None:
    payment = await session.get(Payment, callback_data.item_id)
    if payment is None or payment.status != "submitted":
        await cb.answer(tr("err_already_reviewed"), show_alert=True)
        return
    duplicates = await find_duplicate_orders(session, payment.proof_file_unique_id, payment.order_id)
    caption = review_caption(payment.order, payment, settings, duplicates, settings.max_proof_attempts)
    ok = await post_for_review(bot, session, settings, payment, caption, review_kb(payment.id, settings.default_lang))
    await cb.answer(tr("adm_repost_ok" if ok else "adm_repost_fail"), show_alert=not ok)


@router.callback_query(AdminCb.filter(F.action == "refunds"))
async def cb_refunds(cb: CallbackQuery, tr: Translator, session: AsyncSession) -> None:
    orders = await refunds_pending(session)
    lines = [tr("adm_refunds_title", n=len(orders)) if orders else tr("adm_refunds_empty")]
    b = InlineKeyboardBuilder()
    for o in orders:
        lines.append(
            tr(
                "adm_refund_line",
                order_id=o.id,
                title=esc(o.event.loc("title", tr.lang)),
                amount=fmt_money(o.amount_tiyin, tr.lang),
                name=esc(o.user.full_name),
                phone=esc(o.user.phone or "—"),
            )
        )
        b.button(text=tr("adm_btn_refund_done", order_id=o.id), callback_data=AdminCb(action="refund_done", item_id=o.id))
    b.adjust(1)
    b.row(admin_back(tr))
    await cb.answer()
    await cb.message.edit_text("\n\n".join(lines), reply_markup=b.as_markup())


@router.callback_query(AdminCb.filter(F.action == "refund_done"))
async def cb_refund_done(cb: CallbackQuery, callback_data: AdminCb, user: User, tr: Translator, session: AsyncSession, settings: Settings, bot: Bot) -> None:
    try:
        order = await mark_refunded(session, order_id=callback_data.item_id, by_tg_id=user.tg_id)
    except BookingError as e:
        await cb.answer(tr(e.key, **e.kwargs), show_alert=True)
        return
    await session.commit()
    ul = order.user.lang or settings.default_lang
    await safe_send(bot, session, order.user.tg_id, t(ul, "refund_done", order_id=order.id))
    await cb_refunds(cb, tr, session)
