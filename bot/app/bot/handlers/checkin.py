"""Entrance check-in. Staff scan the ticket QR with the phone camera: it opens
t.me/<bot>?start=t_<token>, and the bot shows the ticket with a "let in" button."""

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import CheckInCb
from app.bot.middlewares import IsStaff
from app.bot.states import CheckInSG
from app.config import Settings
from app.db.models import Ticket, TicketStatus, User, UserRole
from app.formatting import esc, fmt_dt
from app.i18n import Translator, all_variants
from app.services.booking import now_utc
from app.services.notify import send_ticket
from app.services.tickets import check_in, find_ticket

router = Router(name="checkin")
router.message.filter(F.chat.type == "private")


def ticket_card(ticket: Ticket, tr: Translator, settings: Settings) -> str:
    event = ticket.event
    status_key = {
        TicketStatus.VALID: "ci_status_valid",
        TicketStatus.USED: "ci_status_used",
        TicketStatus.CANCELLED: "ci_status_cancelled",
    }[TicketStatus(ticket.status)]
    lines = [
        tr(status_key, at=fmt_dt(ticket.checked_in_at, settings.tz, tr.lang, with_weekday=False) if ticket.checked_in_at else ""),
        "",
        f"<b>{esc(event.loc('title', tr.lang))}</b>",
        f"📅 {fmt_dt(event.starts_at, settings.tz, tr.lang)}",
        tr("ci_holder", name=esc(ticket.holder_name)),
        tr("ci_type", type=esc(ticket.ticket_type.loc("name", tr.lang))),
        tr("ci_code", code=ticket.code),
    ]
    if event.starts_at.astimezone(settings.tz).date() != now_utc().astimezone(settings.tz).date():
        lines += ["", tr("ci_not_today")]
    return "\n".join(lines)


def checkin_kb(ticket: Ticket, tr: Translator) -> InlineKeyboardMarkup | None:
    if ticket.status != TicketStatus.VALID:
        return None
    b = InlineKeyboardBuilder()
    b.button(text=tr("ci_btn_let_in"), callback_data=CheckInCb(token=ticket.token))
    return b.as_markup()


async def show_ticket_to_staff(message: Message, ticket: Ticket, tr: Translator, settings: Settings) -> None:
    await message.answer(ticket_card(ticket, tr, settings), reply_markup=checkin_kb(ticket, tr))


async def handle_scan(
    message: Message, token: str, *, user: User, tr: Translator, session: AsyncSession, settings: Settings, bot: Bot
) -> None:
    ticket = await find_ticket(session, token=token)
    if ticket is None:
        await message.answer(tr("ci_not_found"))
        return
    if user.role in (UserRole.STAFF, UserRole.ADMIN):
        await show_ticket_to_staff(message, ticket, tr, settings)
    elif ticket.user_id == user.id:
        await send_ticket(bot, session, settings, ticket)
    else:
        await message.answer(tr("ci_staff_only", title=esc(ticket.event.loc("title", tr.lang))))


@router.message(Command("checkin"), IsStaff)
async def cmd_checkin(message: Message, command: CommandObject, tr: Translator, session: AsyncSession, settings: Settings, state: FSMContext) -> None:
    if command.args:
        await lookup_code(message, command.args, tr, session, settings)
        return
    await state.set_state(CheckInSG.waiting_code)
    await message.answer(tr("ci_prompt"))


@router.message(F.text.in_(all_variants("btn_checkin")), IsStaff)
async def btn_checkin(message: Message, tr: Translator, state: FSMContext) -> None:
    await state.set_state(CheckInSG.waiting_code)
    await message.answer(tr("ci_prompt"))


async def lookup_code(message: Message, code: str, tr: Translator, session: AsyncSession, settings: Settings) -> None:
    ticket = await find_ticket(session, code=code)
    if ticket is None:
        await message.answer(tr("ci_not_found"))
        return
    await show_ticket_to_staff(message, ticket, tr, settings)


@router.message(CheckInSG.waiting_code, F.text, IsStaff)
async def on_code(message: Message, tr: Translator, session: AsyncSession, settings: Settings) -> None:
    # The state stays on so staff can type codes one after another.
    await lookup_code(message, message.text, tr, session, settings)


@router.callback_query(CheckInCb.filter(), IsStaff)
async def cb_let_in(cb: CallbackQuery, callback_data: CheckInCb, user: User, tr: Translator, session: AsyncSession, settings: Settings) -> None:
    ticket = await find_ticket(session, token=callback_data.token)
    if ticket is None:
        await cb.answer(tr("ci_not_found"), show_alert=True)
        return
    result = await check_in(session, ticket_id=ticket.id, staff=user)
    await session.commit()
    if result.ticket is None:
        await cb.answer(tr("ci_not_found"), show_alert=True)
        return
    if result.status == "ok":
        await cb.answer(tr("ci_done_alert"))
        await cb.message.edit_text(tr("ci_done") + "\n\n" + ticket_card(result.ticket, tr, settings))
    else:
        await cb.answer(tr("ci_cancelled_alert" if result.status == "cancelled" else "ci_already_alert"), show_alert=True)
        await cb.message.edit_text(ticket_card(result.ticket, tr, settings))


@router.callback_query(CheckInCb.filter())
async def cb_let_in_denied(cb: CallbackQuery, tr: Translator) -> None:
    await cb.answer(tr("err_no_rights"), show_alert=True)
