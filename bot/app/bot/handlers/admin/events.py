"""Admin: event list, event card with live numbers, publish, participants export, broadcast, cancel."""

import asyncio

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import AdminCb, ModCb
from app.bot.keyboards import admin_back, admin_menu_kb
from app.bot.states import BroadcastSG
from app.config import Settings
from app.db.models import Event, EventStatus, User
from app.formatting import esc, fmt_dt, fmt_money
from app.i18n import Translator, all_variants, t
from app.services import app_settings
from app.services import events as events_svc
from app.services.booking import approved_payment_id, cancel_event, now_utc
from app.services.export import build_event_report
from app.services.notify import safe_send

router = Router(name="admin_events")
PAGE = 8


async def menu_text(session: AsyncSession, tr: Translator) -> str:
    s = await events_svc.global_stats(session)
    return tr("adm_menu", upcoming=s["upcoming"], review=s["review"], tickets=s["tickets"], revenue=fmt_money(s["revenue"], tr.lang))


@router.message(F.chat.type == "private", Command("admin"))
@router.message(F.chat.type == "private", F.text.in_(all_variants("btn_admin")))
async def admin_menu(message: Message, tr: Translator, session: AsyncSession, state: FSMContext) -> None:
    await state.clear()
    await message.answer(await menu_text(session, tr), reply_markup=admin_menu_kb(tr))


@router.callback_query(AdminCb.filter(F.action == "menu"))
async def cb_menu(cb: CallbackQuery, tr: Translator, session: AsyncSession, state: FSMContext) -> None:
    await state.clear()
    await cb.answer()
    await cb.message.edit_text(await menu_text(session, tr), reply_markup=admin_menu_kb(tr))


@router.callback_query(AdminCb.filter(F.action == "stats"))
async def cb_stats(cb: CallbackQuery, tr: Translator, session: AsyncSession) -> None:
    s = await events_svc.global_stats(session)
    await cb.answer()
    await cb.message.edit_text(
        tr("adm_stats", users=s["users"], paid_orders=s["paid_orders"], tickets=s["tickets"], revenue=fmt_money(s["revenue"], tr.lang), review=s["review"]),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[admin_back(tr)]]),
    )


@router.callback_query(AdminCb.filter(F.action == "events"))
async def cb_events(cb: CallbackQuery, callback_data: AdminCb, tr: Translator, session: AsyncSession, settings: Settings) -> None:
    page = callback_data.item_id
    events, total = await events_svc.list_for_admin(session, offset=page * PAGE, limit=PAGE)
    b = InlineKeyboardBuilder()
    for ev in events:
        local = ev.starts_at.astimezone(settings.tz)
        mark = {"draft": "📝", "published": "🟢"}.get(ev.status, "•")
        if ev.starts_at < now_utc():
            mark = "🏁"
        b.button(text=f"{mark} {local:%d.%m %H:%M} · {ev.loc('title', tr.lang)}"[:64], callback_data=AdminCb(action="ev", event_id=ev.id))
    b.adjust(1)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=AdminCb(action="events", item_id=page - 1).pack()))
    if (page + 1) * PAGE < total:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=AdminCb(action="events", item_id=page + 1).pack()))
    if nav:
        b.row(*nav)
    b.row(InlineKeyboardButton(text=tr("adm_btn_new_event"), callback_data=AdminCb(action="new_event").pack()))
    b.row(admin_back(tr))
    await cb.answer()
    await cb.message.edit_text(tr("adm_events_title", n=total) if total else tr("adm_events_empty"), reply_markup=b.as_markup())


async def event_card(session: AsyncSession, event: Event, tr: Translator, settings: Settings, bot: Bot) -> tuple[str, InlineKeyboardMarkup]:
    stats = await events_svc.event_stats(session, event)
    me = await bot.me()
    lines = [
        f"<b>{esc(event.loc('title', tr.lang))}</b> · {tr(f'adm_status_{event.status}')}",
        f"📅 {fmt_dt(event.starts_at, settings.tz, tr.lang)}",
    ]
    place = event.loc("venue", tr.lang)
    if place:
        lines.append(f"📍 {esc(place)}")
    lines += ["", tr("adm_types_header")]
    for s in stats.types:
        price = fmt_money(s.ticket_type.price_tiyin, tr.lang) if s.ticket_type.price_tiyin else tr("price_free")
        hidden = "" if s.ticket_type.is_active else " " + tr("adm_hidden")
        issued = tr("adm_type_issued", n=s.issued) if s.issued else ""
        lines.append(tr("adm_type_line", name=esc(s.ticket_type.loc("name", tr.lang)), price=price, sold=s.sold, held=s.held, capacity=s.ticket_type.capacity) + issued + hidden)
    lines += [
        "",
        tr("adm_revenue", amount=fmt_money(stats.revenue_tiyin, tr.lang)),
        tr("adm_checked_in", n=stats.checked_in, total=stats.sold + stats.issued),
        tr("adm_review_refunds", review=stats.awaiting_review, refunds=stats.refunds_pending),
        "",
        tr("adm_share_link", link=f"https://t.me/{me.username}?start=ev_{event.id}"),
    ]
    b = InlineKeyboardBuilder()
    eid = event.id
    if event.status == EventStatus.DRAFT:
        b.button(text=tr("adm_btn_publish"), callback_data=AdminCb(action="publish", event_id=eid))
    elif event.status == EventStatus.PUBLISHED:
        b.button(text=tr("adm_btn_unpublish"), callback_data=AdminCb(action="unpublish", event_id=eid))
    if event.status != EventStatus.CANCELLED:
        b.button(text=tr("adm_btn_edit"), callback_data=AdminCb(action="edit", event_id=eid))
        b.button(text=tr("adm_btn_types"), callback_data=AdminCb(action="types", event_id=eid))
        b.button(text=tr("adm_btn_issue"), callback_data=AdminCb(action="issue", event_id=eid))
    b.button(text=tr("adm_btn_export"), callback_data=AdminCb(action="export", event_id=eid))
    b.button(text=tr("adm_btn_broadcast"), callback_data=AdminCb(action="broadcast", event_id=eid))
    if event.status != EventStatus.CANCELLED:
        b.button(text=tr("adm_btn_cancel_event"), callback_data=AdminCb(action="cancel_ev", event_id=eid))
    b.button(text=tr("btn_back"), callback_data=AdminCb(action="events"))
    b.adjust(1, 2, 1, 2, 1, 1)
    return "\n".join(lines), b.as_markup()


async def show_event_card(target: Message, session: AsyncSession, event_id: int, tr: Translator, settings: Settings, bot: Bot, edit: bool) -> None:
    event = await session.get(Event, event_id, populate_existing=True)
    if event is None:
        await target.answer(tr("err_event_unavailable"))
        return
    await session.refresh(event, ["ticket_types"])
    text, kb = await event_card(session, event, tr, settings, bot)
    if edit:
        await target.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    else:
        await target.answer(text, reply_markup=kb, disable_web_page_preview=True)


@router.callback_query(AdminCb.filter(F.action == "ev"))
async def cb_event(cb: CallbackQuery, callback_data: AdminCb, tr: Translator, session: AsyncSession, settings: Settings, bot: Bot, state: FSMContext) -> None:
    await state.clear()
    await cb.answer()
    await show_event_card(cb.message, session, callback_data.event_id, tr, settings, bot, edit=not (cb.message.photo or cb.message.document))


@router.callback_query(AdminCb.filter(F.action.in_({"publish", "unpublish"})))
async def cb_publish(cb: CallbackQuery, callback_data: AdminCb, tr: Translator, session: AsyncSession, settings: Settings, bot: Bot) -> None:
    event = await session.get(Event, callback_data.event_id)
    if event is None or event.status == EventStatus.CANCELLED:
        await cb.answer(tr("err_event_unavailable"), show_alert=True)
        return
    if callback_data.action == "publish" and not event.ticket_types:
        await cb.answer(tr("adm_err_no_types"), show_alert=True)
        return
    event.status = EventStatus.PUBLISHED if callback_data.action == "publish" else EventStatus.DRAFT
    await session.commit()
    await cb.answer(tr("saved"))
    await show_event_card(cb.message, session, event.id, tr, settings, bot, edit=True)


@router.callback_query(AdminCb.filter(F.action == "export"))
async def cb_export(cb: CallbackQuery, callback_data: AdminCb, tr: Translator, session: AsyncSession, settings: Settings) -> None:
    event = await session.get(Event, callback_data.event_id)
    if event is None:
        await cb.answer(tr("err_event_unavailable"), show_alert=True)
        return
    await cb.answer()
    data = await build_event_report(session, event, tr.lang, settings.tz)
    stamp = now_utc().astimezone(settings.tz).strftime("%Y-%m-%d")
    await cb.message.answer_document(
        BufferedInputFile(data, filename=f"focused-event-{event.id}-{stamp}.xlsx"),
        caption=tr("adm_export_caption", title=esc(event.loc("title", tr.lang))),
    )


@router.callback_query(AdminCb.filter(F.action == "broadcast"))
async def cb_broadcast(cb: CallbackQuery, callback_data: AdminCb, tr: Translator, state: FSMContext) -> None:
    await state.set_state(BroadcastSG.text)
    await state.update_data(event_id=callback_data.event_id)
    await cb.answer()
    await cb.message.answer(tr("adm_broadcast_prompt"))


@router.message(BroadcastSG.text, F.text)
async def on_broadcast_text(message: Message, tr: Translator, session: AsyncSession, state: FSMContext) -> None:
    if len(message.html_text) > 3500:
        await message.answer(tr("adm_broadcast_too_long"))
        return
    data = await state.get_data()
    recipients = await events_svc.ticket_holders(session, data["event_id"])
    await state.update_data(text=message.html_text)
    await state.set_state(BroadcastSG.confirm)
    b = InlineKeyboardBuilder()
    b.button(text=tr("adm_btn_send"), callback_data=AdminCb(action="broadcast_go", event_id=data["event_id"]))
    b.button(text=tr("btn_cancel"), callback_data=AdminCb(action="ev", event_id=data["event_id"]))
    b.adjust(1)
    await message.answer(tr("adm_broadcast_confirm", n=len(recipients)) + "\n\n" + message.html_text, reply_markup=b.as_markup())


@router.callback_query(AdminCb.filter(F.action == "broadcast_go"), BroadcastSG.confirm)
async def cb_broadcast_go(cb: CallbackQuery, callback_data: AdminCb, tr: Translator, session: AsyncSession, settings: Settings, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    await state.clear()
    event = await session.get(Event, callback_data.event_id)
    recipients = await events_svc.ticket_holders(session, callback_data.event_id)
    await cb.answer()
    await cb.message.edit_reply_markup(reply_markup=None)
    sent = 0
    for tg_id, lang in recipients:
        header = f"📣 <b>{esc(event.loc('title', lang or settings.default_lang))}</b>\n\n" if event else ""
        if await safe_send(bot, session, tg_id, header + data["text"]):
            sent += 1
        await asyncio.sleep(0.05)  # stay well under Telegram's ~30 msg/s limit
    await cb.message.answer(tr("adm_broadcast_done", sent=sent, total=len(recipients)))


@router.callback_query(AdminCb.filter(F.action == "cancel_ev"))
async def cb_cancel_event(cb: CallbackQuery, callback_data: AdminCb, tr: Translator) -> None:
    b = InlineKeyboardBuilder()
    b.button(text=tr("adm_btn_cancel_event_yes"), callback_data=AdminCb(action="cancel_ev_yes", event_id=callback_data.event_id))
    b.button(text=tr("btn_back"), callback_data=AdminCb(action="ev", event_id=callback_data.event_id))
    b.adjust(1)
    await cb.answer()
    await cb.message.edit_text(tr("adm_cancel_event_confirm"), reply_markup=b.as_markup())


@router.callback_query(AdminCb.filter(F.action == "cancel_ev_yes"))
async def cb_cancel_event_yes(cb: CallbackQuery, callback_data: AdminCb, tr: Translator, session: AsyncSession, settings: Settings, bot: Bot) -> None:
    result = await cancel_event(session, event_id=callback_data.event_id)
    await session.commit()
    await cb.answer(tr("saved"))
    event = await session.get(Event, callback_data.event_id)
    support = f"@{settings.support_username}" if settings.support_username else "—"

    for order in result.notify_orders:
        ul = order.user.lang or settings.default_lang
        key = "event_cancelled_refund" if order in result.refund_orders else "event_cancelled"
        await safe_send(bot, session, order.user.tg_id, t(ul, key, title=esc(event.loc("title", ul)), order_id=order.id, support=support))
        await asyncio.sleep(0.05)
    for order in result.review_orders:
        ul = order.user.lang or settings.default_lang
        await safe_send(bot, session, order.user.tg_id, t(ul, "event_cancelled_review", title=esc(event.loc("title", ul)), order_id=order.id, support=support))
        await asyncio.sleep(0.05)

    # Each refund gets its own message in the moderation chat with a "refund done" button.
    chat_id, _ = await app_settings.get_moderation_chat(session, settings)
    lang = settings.default_lang
    for order in result.refund_orders:
        payment_id = await approved_payment_id(session, order.id)
        text = t(
            lang,
            "mod_refund_cancelled_event",
            order_id=order.id,
            title=esc(event.loc("title", lang)),
            amount=fmt_money(order.amount_tiyin, lang),
            name=esc(order.user.full_name),
            phone=esc(order.user.phone or "—"),
            username=f"@{esc(order.user.username)}" if order.user.username else "—",
        )
        kb = None
        if payment_id:
            b = InlineKeyboardBuilder()
            b.button(text=t(lang, "mod_btn_refunded"), callback_data=ModCb(action="refunded", payment_id=payment_id))
            kb = b.as_markup()
        await safe_send(bot, session, chat_id or cb.from_user.id, text, reply_markup=kb)
    await show_event_card(cb.message, session, callback_data.event_id, tr, settings, bot, edit=True)
