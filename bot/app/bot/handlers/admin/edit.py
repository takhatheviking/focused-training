"""Admin: edit an event (texts, date and map through the same form; poster and map pin
separately) and manage ticket types (names in RU / UZ / EN, price, seats, visibility)."""

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import AdminCb
from app.bot.handlers.admin.create import form_errors_text, map_url_from, pre
from app.bot.handlers.admin.events import show_event_card
from app.bot.states import EventEditSG, TicketTypeSG
from app.config import Settings
from app.db.models import Event, TicketType
from app.formatting import esc, fmt_money, parse_money_to_tiyin
from app.i18n import Translator
from app.services.booking import now_utc
from app.services.event_form import SKIP, filled_form, parse_form, parse_names

router = Router(name="admin_edit")

EVENT_FIELDS = ("poster", "map")
TYPE_FIELDS = ("tt_name", "tt_price", "tt_capacity")


def cancel_kb(tr: Translator, event_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=tr("btn_cancel"), callback_data=AdminCb(action="ev", event_id=event_id))
    return b.as_markup()


@router.callback_query(AdminCb.filter(F.action == "edit"))
async def cb_edit(cb: CallbackQuery, callback_data: AdminCb, tr: Translator) -> None:
    eid = callback_data.event_id
    b = InlineKeyboardBuilder()
    b.button(text=tr("adm_btn_edit_form"), callback_data=AdminCb(action="edit_form", event_id=eid))
    for f in EVENT_FIELDS:
        b.button(text=tr(f"field_{f}"), callback_data=AdminCb(action="edit_field", event_id=eid, value=f))
    b.button(text=tr("btn_back"), callback_data=AdminCb(action="ev", event_id=eid))
    b.adjust(1, 2, 1)
    await cb.answer()
    await cb.message.edit_text(tr("adm_edit_choose"), reply_markup=b.as_markup())


@router.callback_query(AdminCb.filter(F.action == "edit_form"))
async def cb_edit_form(cb: CallbackQuery, callback_data: AdminCb, tr: Translator, session: AsyncSession, settings: Settings, state: FSMContext) -> None:
    event = await session.get(Event, callback_data.event_id)
    if event is None:
        await cb.answer()
        return
    await state.set_state(EventEditSG.form)
    await state.update_data(event_id=event.id)
    await cb.answer()
    await cb.message.answer(tr("form_edit_intro"), reply_markup=cancel_kb(tr, event.id))
    await cb.message.answer(pre(filled_form(event, tr.lang, settings.tz)))


@router.message(EventEditSG.form, F.text)
async def on_edit_form(message: Message, tr: Translator, session: AsyncSession, settings: Settings, state: FSMContext, bot: Bot) -> None:
    event_id = (await state.get_data())["event_id"]
    event = await session.get(Event, event_id)
    if event is None:
        await state.clear()
        return
    form, errors = parse_form(message.text, tz=settings.tz, ui_lang=tr.lang, with_ticket=False, now=now_utc())
    if errors:
        await message.answer(form_errors_text(tr, errors, message.text))
        return
    form.apply_to(event)
    await session.commit()
    await state.clear()
    await message.answer(tr("saved"))
    await show_event_card(message, session, event_id, tr, settings, bot, edit=False)


@router.message(EventEditSG.form)
async def on_edit_form_not_text(message: Message, tr: Translator) -> None:
    await message.answer(tr("form_send_text"))


@router.callback_query(AdminCb.filter(F.action == "edit_field"))
async def cb_edit_field(cb: CallbackQuery, callback_data: AdminCb, tr: Translator, state: FSMContext) -> None:
    field = callback_data.value
    if field not in EVENT_FIELDS + TYPE_FIELDS:
        await cb.answer()
        return
    await state.set_state(EventEditSG.value)
    await state.update_data(event_id=callback_data.event_id, field=field, item_id=callback_data.item_id)
    await cb.answer()
    await cb.message.answer(tr(f"edit_prompt_{field}"), reply_markup=cancel_kb(tr, callback_data.event_id))


@router.message(EventEditSG.value)
async def on_edit_value(message: Message, tr: Translator, session: AsyncSession, settings: Settings, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    field, event_id = data["field"], data["event_id"]
    event = await session.get(Event, event_id)
    if event is None:
        await state.clear()
        return
    text = (message.text or "").strip()

    if field.startswith("tt_"):
        tt = await session.get(TicketType, data["item_id"])
        if tt is None or tt.event_id != event_id:
            await state.clear()
            return
        error = apply_type_field(tt, field, text)
    else:
        error = apply_event_field(event, field, message, text in SKIP)
    if error:
        await message.answer(tr(error))
        return
    try:
        await session.commit()
    except IntegrityError:
        # Seats were booked between the check and the commit; the DB constraint caught it.
        await session.rollback()
        await message.answer(tr("edit_err_capacity_below_reserved"))
        return
    await state.clear()
    await message.answer(tr("saved"))
    await show_event_card(message, session, event_id, tr, settings, bot, edit=False)


def apply_event_field(event: Event, field: str, message: Message, skip: bool) -> str | None:
    """Returns an i18n error key, or None when the value was applied."""
    if field == "poster":
        if message.photo:
            event.poster_file_id = message.photo[-1].file_id
        elif skip:
            event.poster_file_id = None
        else:
            return "edit_err_photo"
    elif field == "map":
        if skip:
            event.map_url = None
        else:
            url = map_url_from(message)
            if url is None:
                return "wiz_err_location"
            event.map_url = url
    return None


def apply_type_field(tt: TicketType, field: str, text: str) -> str | None:
    if field == "tt_name":
        names = parse_names(text)
        if names is None:
            return "tt_err_names"
        tt.set_loc("name", names)
    elif field == "tt_price":
        tiyin = parse_money_to_tiyin(text)
        if tiyin is None:
            return "wiz_err_price"
        tt.price_tiyin = tiyin  # existing orders keep the price they were created with
    elif field == "tt_capacity":
        if not text.isdigit():
            return "wiz_err_capacity"
        if int(text) < tt.reserved:
            return "edit_err_capacity_below_reserved"
        tt.capacity = int(text)
    return None


@router.callback_query(AdminCb.filter(F.action == "types"))
async def cb_types(cb: CallbackQuery, callback_data: AdminCb, tr: Translator, session: AsyncSession) -> None:
    event = await session.get(Event, callback_data.event_id, populate_existing=True)
    if event is None:
        await cb.answer()
        return
    await session.refresh(event, ["ticket_types"])
    b = InlineKeyboardBuilder()
    for tt in event.ticket_types:
        price = fmt_money(tt.price_tiyin, tr.lang) if tt.price_tiyin else tr("price_free")
        hidden = " 🚫" if not tt.is_active else ""
        b.button(text=f"{tt.loc('name', tr.lang)} — {price} · {tt.reserved}/{tt.capacity}{hidden}"[:64], callback_data=AdminCb(action="tt", event_id=event.id, item_id=tt.id))
    b.button(text=tr("adm_btn_add_type"), callback_data=AdminCb(action="tt_add", event_id=event.id))
    b.button(text=tr("btn_back"), callback_data=AdminCb(action="ev", event_id=event.id))
    b.adjust(1)
    await cb.answer()
    await cb.message.edit_text(tr("adm_types_title", title=esc(event.loc("title", tr.lang))), reply_markup=b.as_markup())


@router.callback_query(AdminCb.filter(F.action == "tt"))
async def cb_type(cb: CallbackQuery, callback_data: AdminCb, tr: Translator, session: AsyncSession) -> None:
    tt = await session.get(TicketType, callback_data.item_id, populate_existing=True)
    if tt is None:
        await cb.answer()
        return
    eid = callback_data.event_id
    b = InlineKeyboardBuilder()
    for f in TYPE_FIELDS:
        b.button(text=tr(f"field_{f}"), callback_data=AdminCb(action="edit_field", event_id=eid, item_id=tt.id, value=f))
    b.button(text=tr("adm_btn_hide" if tt.is_active else "adm_btn_show"), callback_data=AdminCb(action="tt_toggle", event_id=eid, item_id=tt.id))
    b.button(text=tr("btn_back"), callback_data=AdminCb(action="types", event_id=eid))
    b.adjust(3, 1, 1)
    price = fmt_money(tt.price_tiyin, tr.lang) if tt.price_tiyin else tr("price_free")
    names = " / ".join(esc(tt.loc("name", lang)) for lang in ("ru", "uz", "en"))
    await cb.answer()
    await cb.message.edit_text(
        tr("adm_type_card", name=names, price=price, capacity=tt.capacity, reserved=tt.reserved, available=tt.available),
        reply_markup=b.as_markup(),
    )


@router.callback_query(AdminCb.filter(F.action == "tt_toggle"))
async def cb_type_toggle(cb: CallbackQuery, callback_data: AdminCb, tr: Translator, session: AsyncSession) -> None:
    tt = await session.get(TicketType, callback_data.item_id)
    if tt is not None:
        tt.is_active = not tt.is_active
        await session.commit()
    await cb_type(cb, callback_data, tr, session)


@router.callback_query(AdminCb.filter(F.action == "tt_add"))
async def cb_type_add(cb: CallbackQuery, callback_data: AdminCb, tr: Translator, state: FSMContext) -> None:
    await state.set_state(TicketTypeSG.name)
    await state.update_data(event_id=callback_data.event_id)
    await cb.answer()
    await cb.message.answer(tr("tt_prompt_name"), reply_markup=cancel_kb(tr, callback_data.event_id))


@router.message(TicketTypeSG.name, F.text)
async def on_type_name(message: Message, tr: Translator, state: FSMContext) -> None:
    names = parse_names(message.text)
    if names is None:
        await message.answer(tr("tt_err_names"))
        return
    await state.update_data(names=names)
    await state.set_state(TicketTypeSG.price)
    await message.answer(tr("tt_prompt_price"))


@router.message(TicketTypeSG.price, F.text)
async def on_type_price(message: Message, tr: Translator, state: FSMContext) -> None:
    tiyin = parse_money_to_tiyin(message.text)
    if tiyin is None:
        await message.answer(tr("wiz_err_price"))
        return
    await state.update_data(price_tiyin=tiyin)
    await state.set_state(TicketTypeSG.capacity)
    await message.answer(tr("tt_prompt_capacity"))


@router.message(TicketTypeSG.capacity, F.text)
async def on_type_capacity(message: Message, tr: Translator, session: AsyncSession, settings: Settings, state: FSMContext, bot: Bot) -> None:
    text = message.text.strip()
    if not text.isdigit() or not 1 <= int(text) <= 100_000:
        await message.answer(tr("wiz_err_capacity"))
        return
    data = await state.get_data()
    await state.clear()
    count = len((await session.get(Event, data["event_id"])).ticket_types)
    tt = TicketType(event_id=data["event_id"], name=data["names"]["ru"], price_tiyin=data["price_tiyin"], capacity=int(text), sort_order=count)
    tt.set_loc("name", data["names"])
    session.add(tt)
    await session.commit()
    await message.answer(tr("saved"))
    await show_event_card(message, session, data["event_id"], tr, settings, bot, edit=False)
