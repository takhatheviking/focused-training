"""Admin: event creation from one filled form (texts in RU / UZ / EN), then the poster."""

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import AdminCb
from app.bot.handlers.admin.events import show_event_card
from app.bot.states import EventCreateSG
from app.config import Settings
from app.db.models import Event, EventStatus, TicketType, User
from app.formatting import esc, fmt_dt, fmt_money
from app.i18n import Translator
from app.services.booking import now_utc
from app.services.event_form import (
    DEFAULT_TICKET_NAMES,
    SKIP,
    EventForm,
    blank_form,
    example_form,
    label_of,
    parse_form,
)

router = Router(name="admin_create")
FORM_HINT_LENGTH = 3900  # Telegram splits longer pastes into several messages
FLAGS = {"ru": "🇷🇺", "uz": "🇺🇿", "en": "🇬🇧"}


def map_url_from(message: Message) -> str | None:
    if message.location:
        return f"https://maps.google.com/?q={message.location.latitude},{message.location.longitude}"
    if message.venue:
        loc = message.venue.location
        return f"https://maps.google.com/?q={loc.latitude},{loc.longitude}"
    if message.text and message.text.strip().startswith(("http://", "https://")):
        return message.text.strip()[:512]
    return None


def cancel_row(b: InlineKeyboardBuilder, tr: Translator) -> None:
    b.button(text=tr("btn_cancel"), callback_data=AdminCb(action="menu"))


def pre(text: str) -> str:
    # <pre> blocks are copied with one tap in Telegram.
    return f"<pre>{esc(text)}</pre>"


async def send_form(message: Message, tr: Translator, form_text: str, intro_key: str) -> None:
    b = InlineKeyboardBuilder()
    b.button(text=tr("form_btn_example"), callback_data=AdminCb(action="form_example"))
    cancel_row(b, tr)
    b.adjust(1)
    await message.answer(tr(intro_key), reply_markup=b.as_markup())
    await message.answer(pre(form_text))


def form_errors_text(tr: Translator, errors: list[str], source: str) -> str:
    text = tr("form_errors", errors="\n".join(f"• {e}" for e in errors))
    if len(source) > FORM_HINT_LENGTH:
        text += "\n\n" + tr("form_too_long_hint")
    return text


def preview_text(form: EventForm, tr: Translator, settings: Settings) -> str:
    parts = [tr("form_preview_header")]
    for lang, flag in FLAGS.items():
        title = form.texts["title"].get(lang) or form.texts["title"]["ru"]
        venue = form.texts["venue"].get(lang) or form.texts["venue"]["ru"]
        description = form.texts["description"].get(lang) or form.texts["description"].get("ru") or ""
        if len(description) > 300:
            description = description[:300] + "…"
        block = [f"{flag} <b>{esc(title)}</b>", f"📍 {esc(venue)}"]
        if description:
            block.append(esc(description))
        parts.append("\n".join(block))
    price = fmt_money(form.price_tiyin, tr.lang) if form.price_tiyin else tr("price_free")
    parts.append(
        "\n".join(
            [
                f"📅 {fmt_dt(form.starts_at, settings.tz, tr.lang)}",
                f"🗺 {esc(form.map_url or '—')}",
                f"💵 {price}",
                tr("form_preview_capacity", n=form.capacity),
            ]
        )
    )
    if form.missing:
        parts.append(tr("form_missing", fields=", ".join(esc(label_of(k, tr.lang)) for k in form.missing)))
    return "\n\n".join(parts)


@router.callback_query(AdminCb.filter(F.action == "new_event"))
async def cb_new_event(cb: CallbackQuery, tr: Translator, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(EventCreateSG.form)
    await cb.answer()
    await send_form(cb.message, tr, blank_form(tr.lang), "form_intro")


@router.callback_query(AdminCb.filter(F.action == "form_example"))
async def cb_example(cb: CallbackQuery, tr: Translator) -> None:
    await cb.answer()
    await cb.message.answer(tr("form_example_intro") + "\n\n" + pre(example_form(tr.lang)))


@router.message(EventCreateSG.form, F.text)
async def on_form(message: Message, tr: Translator, state: FSMContext, settings: Settings) -> None:
    form, errors = parse_form(message.text, tz=settings.tz, ui_lang=tr.lang, with_ticket=True, now=now_utc())
    if errors:
        await message.answer(form_errors_text(tr, errors, message.text))
        return
    await state.update_data(form=form.to_state(), form_text=message.text)
    await state.set_state(EventCreateSG.poster)
    b = InlineKeyboardBuilder()
    b.button(text=tr("btn_skip"), callback_data=AdminCb(action="form_no_poster"))
    cancel_row(b, tr)
    b.adjust(1)
    await message.answer(tr("form_ask_poster"), reply_markup=b.as_markup())


@router.message(EventCreateSG.form)
async def on_form_not_text(message: Message, tr: Translator) -> None:
    await message.answer(tr("form_send_text"))


async def show_preview(message: Message, tr: Translator, state: FSMContext, settings: Settings) -> None:
    data = await state.get_data()
    form = EventForm.from_state(data["form"])
    await state.set_state(EventCreateSG.confirm)
    b = InlineKeyboardBuilder()
    b.button(text=tr("adm_btn_publish"), callback_data=AdminCb(action="wiz_save", value="publish"))
    b.button(text=tr("wiz_btn_draft"), callback_data=AdminCb(action="wiz_save", value="draft"))
    b.button(text=tr("form_btn_fix"), callback_data=AdminCb(action="form_fix"))
    cancel_row(b, tr)
    b.adjust(1)
    if data.get("poster"):
        await message.answer_photo(data["poster"])
    await message.answer(preview_text(form, tr, settings), reply_markup=b.as_markup(), disable_web_page_preview=True)


@router.message(EventCreateSG.poster, F.photo)
@router.message(EventCreateSG.poster, F.text.in_(SKIP))
async def on_poster(message: Message, tr: Translator, state: FSMContext, settings: Settings) -> None:
    await state.update_data(poster=message.photo[-1].file_id if message.photo else None)
    await show_preview(message, tr, state, settings)


@router.callback_query(AdminCb.filter(F.action == "form_no_poster"), EventCreateSG.poster)
async def cb_no_poster(cb: CallbackQuery, tr: Translator, state: FSMContext, settings: Settings) -> None:
    await cb.answer()
    await state.update_data(poster=None)
    await show_preview(cb.message, tr, state, settings)


@router.message(EventCreateSG.poster)
async def on_poster_other(message: Message, tr: Translator) -> None:
    await message.answer(tr("edit_err_photo"))


@router.callback_query(AdminCb.filter(F.action == "form_fix"), EventCreateSG.confirm)
async def cb_fix(cb: CallbackQuery, tr: Translator, state: FSMContext) -> None:
    # Send the admin's own form back so they can copy, correct and resend it.
    data = await state.get_data()
    await state.set_state(EventCreateSG.form)
    await cb.answer()
    await cb.message.edit_reply_markup(reply_markup=None)
    await send_form(cb.message, tr, data.get("form_text") or blank_form(tr.lang), "form_fix_intro")


@router.callback_query(AdminCb.filter(F.action == "wiz_save"), EventCreateSG.confirm)
async def cb_save(cb: CallbackQuery, callback_data: AdminCb, user: User, tr: Translator, session: AsyncSession, settings: Settings, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    await state.clear()
    form = EventForm.from_state(data["form"])
    event = Event(
        title=form.texts["title"]["ru"],
        starts_at=form.starts_at,
        poster_file_id=data.get("poster"),
        status=EventStatus.PUBLISHED if callback_data.value == "publish" else EventStatus.DRAFT,
        created_by_id=user.id,
    )
    form.apply_to(event)
    session.add(event)
    await session.flush()
    tt = TicketType(event_id=event.id, name=DEFAULT_TICKET_NAMES["ru"], price_tiyin=form.price_tiyin, capacity=form.capacity)
    tt.set_loc("name", DEFAULT_TICKET_NAMES)
    session.add(tt)
    await session.commit()
    await cb.answer(tr("saved"))
    await cb.message.edit_reply_markup(reply_markup=None)
    await show_event_card(cb.message, session, event.id, tr, settings, bot, edit=False)
