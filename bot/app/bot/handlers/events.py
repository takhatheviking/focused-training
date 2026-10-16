from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import EventsCb, OrderCb
from app.config import Settings
from app.db.models import Event, EventStatus, User
from app.formatting import esc, fmt_dt, fmt_money
from app.i18n import Translator, all_variants
from app.services import events as events_svc
from app.services.booking import get_active_order, get_free_registration, now_utc

router = Router(name="events")
PAGE_SIZE = 6
CAPTION_LIMIT = 1024


def price_line(event: Event, tr: Translator) -> str:
    prices = sorted({tt.price_tiyin for tt in event.ticket_types if tt.is_active})
    if not prices:
        return ""
    if prices == [0]:
        return tr("price_free")
    if len(prices) == 1:
        return fmt_money(prices[0], tr.lang)
    low = prices[0]
    return tr("price_from", price=fmt_money(low, tr.lang)) if low else tr("price_free_or_paid")


def seats_left(event: Event) -> int:
    return sum(tt.available for tt in event.ticket_types if tt.is_active)


def event_text(event: Event, tr: Translator, settings: Settings, with_description: bool = True) -> str:
    lines = [f"<b>{esc(event.loc('title', tr.lang))}</b>", ""]
    lines.append(f"📅 {fmt_dt(event.starts_at, settings.tz, tr.lang)}")
    place = event.loc("venue", tr.lang)
    if place:
        lines.append(f"📍 {esc(place)}")
    if event.map_url:
        lines.append(f'🗺 <a href="{esc(event.map_url)}">{tr("map_link")}</a>')
    price = price_line(event, tr)
    if price:
        lines.append(f"💵 {price}")
    left = seats_left(event)
    lines.append(f"🎟 {tr('seats_left', n=left) if left else tr('sold_out')}")
    description = event.loc("description", tr.lang)
    if with_description and description:
        lines += ["", esc(description)]
    return "\n".join(lines)


def event_kb(event: Event, tr: Translator, has_active_order_id: int | None, page: int = 0, registered: bool = False) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    bookable = event.status == EventStatus.PUBLISHED and event.starts_at > now_utc()
    if has_active_order_id:
        b.button(text=tr("btn_my_booking"), callback_data=OrderCb(action="show", order_id=has_active_order_id))
    elif registered:
        pass  # free event, already registered: the ticket is in "My tickets"
    elif bookable and seats_left(event) > 0:
        is_free = all(tt.price_tiyin == 0 for tt in event.ticket_types if tt.is_active)
        b.button(text=tr("btn_register" if is_free else "btn_buy"), callback_data=EventsCb(action="buy", event_id=event.id))
    b.button(text=tr("btn_all_events"), callback_data=EventsCb(action="list", page=page))
    b.adjust(1)
    return b.as_markup()


async def send_event_card(
    message: Message, event_id: int, *, user: User, tr: Translator, session: AsyncSession, settings: Settings, page: int = 0
) -> None:
    event = await session.get(Event, event_id)
    if event is None or event.status != EventStatus.PUBLISHED:
        await message.answer(tr("err_event_unavailable"))
        return
    active = await get_active_order(session, user.id, event.id)
    registered = await get_free_registration(session, user.id, event.id) is not None
    kb = event_kb(event, tr, active.id if active else None, page, registered)
    full = event_text(event, tr, settings)
    if registered:
        full += "\n\n" + tr("already_registered")
    if event.poster_file_id and len(full) <= CAPTION_LIMIT:
        await message.answer_photo(event.poster_file_id, caption=full, reply_markup=kb)
    elif event.poster_file_id:
        await message.answer_photo(event.poster_file_id)
        await message.answer(full, reply_markup=kb, disable_web_page_preview=True)
    else:
        await message.answer(full, reply_markup=kb, disable_web_page_preview=True)


async def events_list_view(session: AsyncSession, tr: Translator, settings: Settings, page: int) -> tuple[str, InlineKeyboardMarkup | None]:
    events, total = await events_svc.list_upcoming(session, offset=page * PAGE_SIZE, limit=PAGE_SIZE)
    if not total:
        return tr("no_events"), None
    b = InlineKeyboardBuilder()
    for ev in events:
        local = ev.starts_at.astimezone(settings.tz)
        mark = "" if seats_left(ev) else " · " + tr("sold_out_short")
        b.button(text=f"{local:%d.%m} · {ev.loc('title', tr.lang)}{mark}"[:60], callback_data=EventsCb(action="open", event_id=ev.id, page=page))
    b.adjust(1)
    nav = []
    if page > 0:
        nav.append(("◀️", page - 1))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(("▶️", page + 1))
    if nav:
        b.row(*[InlineKeyboardButton(text=label, callback_data=EventsCb(action="list", page=p).pack()) for label, p in nav])
    return tr("events_title", n=total), b.as_markup()


@router.message(F.chat.type == "private", F.text.in_(all_variants("btn_events")))
async def show_events(message: Message, tr: Translator, session: AsyncSession, settings: Settings) -> None:
    text, kb = await events_list_view(session, tr, settings, 0)
    await message.answer(text, reply_markup=kb)


@router.callback_query(EventsCb.filter(F.action == "list"))
async def cb_list(cb: CallbackQuery, callback_data: EventsCb, tr: Translator, session: AsyncSession, settings: Settings) -> None:
    text, kb = await events_list_view(session, tr, settings, callback_data.page)
    await cb.answer()
    if cb.message.photo or cb.message.document:
        # Event cards with a poster cannot be edited into a text list.
        await cb.message.answer(text, reply_markup=kb)
    else:
        await cb.message.edit_text(text, reply_markup=kb)


@router.callback_query(EventsCb.filter(F.action == "open"))
async def cb_open(
    cb: CallbackQuery, callback_data: EventsCb, user: User, tr: Translator, session: AsyncSession, settings: Settings
) -> None:
    await cb.answer()
    await send_event_card(cb.message, callback_data.event_id, user=user, tr=tr, session=session, settings=settings, page=callback_data.page)
