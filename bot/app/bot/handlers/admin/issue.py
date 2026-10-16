"""Admin: issue tickets by hand — guests, speakers, partners, cash payments.

Event card → "🎫 Issue ticket" → ticket type → quantity → recipient (a Telegram user
picked from contacts, or a typed name with an optional phone) → confirm. The ticket goes
to the recipient's Telegram; if there is none, or they never opened the bot, it comes to
the admin to forward.
"""

import re

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, KeyboardButton, KeyboardButtonRequestUsers, Message, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import AdminCb
from app.bot.keyboards import main_menu
from app.bot.states import IssueSG
from app.config import Settings
from app.db.models import Event, EventStatus, TicketType, User, UserRole
from app.formatting import esc
from app.i18n import Translator
from app.services.booking import BookingError, issue_manual
from app.services.notify import send_ticket

router = Router(name="admin_issue")
REQUEST_RECIPIENT = 3
MAX_ISSUE = 10
PHONE_RE = re.compile(r"\+?\d[\d\s\-()]{7,}\d")


def split_name_phone(text: str) -> tuple[str, str | None]:
    """'Иван Петров, +998 90 123 45 67' -> ('Иван Петров', '+998901234567')."""
    phone = None
    if m := PHONE_RE.search(text):
        digits = re.sub(r"\D", "", m.group(0))
        phone = f"+{digits}"
        text = text[: m.start()] + text[m.end():]
    name = " ".join(text.replace(",", " ").replace(";", " ").split())
    return name, phone


async def _event_or_alert(cb: CallbackQuery, session: AsyncSession, event_id: int, tr: Translator) -> Event | None:
    event = await session.get(Event, event_id)
    if event is None or event.status == EventStatus.CANCELLED:
        await cb.answer(tr("err_event_unavailable"), show_alert=True)
        return None
    return event


async def ask_quantity(cb: CallbackQuery, tt: TicketType, tr: Translator) -> None:
    if tt.available <= 0:
        await cb.answer(tr("issue_no_seats"), show_alert=True)
        return
    await cb.answer()
    b = InlineKeyboardBuilder()
    for n in range(1, min(MAX_ISSUE, tt.available) + 1):
        b.button(text=str(n), callback_data=AdminCb(action="issue_qty", event_id=tt.event_id, item_id=tt.id, value=str(n)))
    b.adjust(5)
    b.row(InlineKeyboardButton(text=tr("btn_cancel"), callback_data=AdminCb(action="ev", event_id=tt.event_id).pack()))
    await cb.message.answer(tr("issue_choose_qty", type=esc(tt.loc("name", tr.lang)), n=tt.available), reply_markup=b.as_markup())


@router.callback_query(AdminCb.filter(F.action == "issue"))
async def cb_issue(cb: CallbackQuery, callback_data: AdminCb, tr: Translator, session: AsyncSession, state: FSMContext) -> None:
    await state.clear()
    event = await _event_or_alert(cb, session, callback_data.event_id, tr)
    if event is None:
        return
    await session.refresh(event, ["ticket_types"])
    types = list(event.ticket_types)
    if not types:
        await cb.answer(tr("adm_err_no_types"), show_alert=True)
        return
    if len(types) == 1:
        await ask_quantity(cb, types[0], tr)
        return
    await cb.answer()
    b = InlineKeyboardBuilder()
    for tt in types:
        hidden = " 🚫" if not tt.is_active else ""
        b.button(
            text=f"{tt.loc('name', tr.lang)}{hidden} · {tr('left_short', n=tt.available)}"[:64],
            callback_data=AdminCb(action="issue_tt", event_id=event.id, item_id=tt.id),
        )
    b.button(text=tr("btn_cancel"), callback_data=AdminCb(action="ev", event_id=event.id))
    b.adjust(1)
    await cb.message.answer(tr("issue_choose_type"), reply_markup=b.as_markup())


@router.callback_query(AdminCb.filter(F.action == "issue_tt"))
async def cb_issue_type(cb: CallbackQuery, callback_data: AdminCb, tr: Translator, session: AsyncSession) -> None:
    tt = await session.get(TicketType, callback_data.item_id)
    if tt is None or tt.event_id != callback_data.event_id:
        await cb.answer()
        return
    await ask_quantity(cb, tt, tr)


@router.callback_query(AdminCb.filter(F.action == "issue_qty"))
async def cb_issue_qty(cb: CallbackQuery, callback_data: AdminCb, tr: Translator, state: FSMContext) -> None:
    await state.set_state(IssueSG.recipient)
    await state.update_data(event_id=callback_data.event_id, tt_id=callback_data.item_id, qty=int(callback_data.value))
    await cb.answer()
    await cb.message.edit_reply_markup(reply_markup=None)
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=tr("issue_btn_pick"), request_users=KeyboardButtonRequestUsers(request_id=REQUEST_RECIPIENT, user_is_bot=False, max_quantity=1, request_name=True, request_username=True))],
            [KeyboardButton(text=tr("btn_cancel"))],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await cb.message.answer(tr("issue_prompt_recipient"), reply_markup=kb)


async def confirm(message: Message, user: User, tr: Translator, state: FSMContext, session: AsyncSession) -> None:
    data = await state.get_data()
    tt = await session.get(TicketType, data["tt_id"])
    event = await session.get(Event, data["event_id"])
    holder = data["holder"]
    dest = tr("issue_dest_tg", user=esc(data["recipient_label"])) if data.get("recipient_tg") else tr("issue_dest_admin")
    await state.set_state(IssueSG.confirm)
    await message.answer(tr("issue_recipient_ok"), reply_markup=main_menu(tr, user))
    b = InlineKeyboardBuilder()
    b.button(text=tr("issue_btn_go"), callback_data=AdminCb(action="issue_go", event_id=event.id))
    b.button(text=tr("btn_cancel"), callback_data=AdminCb(action="ev", event_id=event.id))
    b.adjust(1)
    phone = f" · {esc(data['phone'])}" if data.get("phone") else ""
    await message.answer(
        tr(
            "issue_confirm",
            qty=data["qty"],
            type=esc(tt.loc("name", tr.lang)),
            title=esc(event.loc("title", tr.lang)),
            holder=esc(holder) + phone,
            dest=dest,
        ),
        reply_markup=b.as_markup(),
    )


@router.message(IssueSG.recipient, F.users_shared)
async def on_recipient_picked(message: Message, user: User, tr: Translator, state: FSMContext, session: AsyncSession) -> None:
    shared = message.users_shared.users[0]
    name = " ".join(p for p in (shared.first_name, shared.last_name) if p) or (shared.username or str(shared.user_id))
    label = f"@{shared.username}" if shared.username else name
    await state.update_data(
        recipient_tg=shared.user_id,
        recipient_first=shared.first_name,
        recipient_last=shared.last_name,
        recipient_username=shared.username,
        recipient_label=label,
        holder=name[:255],
        phone=None,
    )
    await confirm(message, user, tr, state, session)


@router.message(IssueSG.recipient, F.text)
async def on_recipient_typed(message: Message, user: User, tr: Translator, state: FSMContext, session: AsyncSession) -> None:
    name, phone = split_name_phone(message.text)
    if not 2 <= len(name) <= 100:
        await message.answer(tr("issue_err_name"))
        return
    await state.update_data(recipient_tg=None, holder=name, phone=phone)
    await confirm(message, user, tr, state, session)


async def _recipient_user(session: AsyncSession, data: dict) -> User:
    """The picked Telegram user, created if they never opened the bot (the role is untouched)."""
    found = (await session.execute(select(User).where(User.tg_id == data["recipient_tg"]))).scalar_one_or_none()
    if found is not None:
        return found
    created = User(
        tg_id=data["recipient_tg"],
        first_name=data.get("recipient_first"),
        last_name=data.get("recipient_last"),
        username=data.get("recipient_username"),
        role=UserRole.USER,
    )
    session.add(created)
    await session.flush()
    return created


@router.callback_query(AdminCb.filter(F.action == "issue_go"), IssueSG.confirm)
async def cb_issue_go(cb: CallbackQuery, user: User, tr: Translator, session: AsyncSession, settings: Settings, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    await state.clear()
    recipient = await _recipient_user(session, data) if data.get("recipient_tg") else None
    try:
        order, tickets = await issue_manual(
            session,
            ticket_type_id=data["tt_id"],
            quantity=data["qty"],
            owner=recipient or user,
            issued_by=user,
            guest_name=None if recipient else data["holder"],
            guest_phone=None if recipient else data.get("phone"),
        )
    except BookingError as e:
        await cb.answer(tr("issue_no_seats" if e.key == "err_sold_out" else e.key), show_alert=True)
        return
    await session.commit()
    await cb.answer()
    await cb.message.edit_reply_markup(reply_markup=None)

    codes = ", ".join(f"<code>{t.code}</code>" for t in tickets)
    if recipient is not None:
        try:
            for ticket in tickets:
                await send_ticket(bot, session, settings, ticket)
        except (TelegramForbiddenError, TelegramBadRequest):
            # They never opened the bot (or blocked it): Telegram refuses, so the admin forwards it.
            await cb.message.answer(tr("issue_tg_failed", user=esc(data["recipient_label"])))
            for ticket in tickets:
                await send_ticket(bot, session, settings, ticket, chat_id=user.tg_id, lang=tr.lang)
            await cb.message.answer(tr("issue_done_forward", codes=codes))
            return
        await cb.message.answer(tr("issue_done_sent", user=esc(data["recipient_label"]), codes=codes))
        return
    for ticket in tickets:
        await send_ticket(bot, session, settings, ticket)
    await cb.message.answer(tr("issue_done_forward", codes=codes))
