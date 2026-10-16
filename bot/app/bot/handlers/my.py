from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import MyCb, OrderCb
from app.config import Settings
from app.db.models import OrderStatus, Ticket, TicketStatus, User
from app.i18n import Translator, all_variants
from app.services import events as events_svc
from app.services.notify import send_ticket

router = Router(name="my")

ORDER_ICON = {OrderStatus.PENDING: "⏳", OrderStatus.UNDER_REVIEW: "🕓", OrderStatus.REFUND_REQUIRED: "💸"}


@router.message(F.chat.type == "private", Command("my"))
@router.message(F.chat.type == "private", F.text.in_(all_variants("btn_my")))
async def my_tickets(message: Message, user: User, tr: Translator, session: AsyncSession, settings: Settings) -> None:
    orders = await events_svc.user_open_orders(session, user.id)
    tickets = await events_svc.user_tickets(session, user.id)
    if not orders and not tickets:
        await message.answer(tr("my_empty"))
        return
    b = InlineKeyboardBuilder()
    for o in orders:
        status = tr(f"order_status_{o.status}")
        b.button(text=f"{ORDER_ICON.get(OrderStatus(o.status), '•')} №{o.id} · {o.event.loc('title', tr.lang)} — {status}"[:64], callback_data=OrderCb(action="show", order_id=o.id))
    for tk in tickets:
        local = tk.event.starts_at.astimezone(settings.tz)
        icon = "✅" if tk.status == TicketStatus.USED else "🎫"
        b.button(text=f"{icon} {local:%d.%m} · {tk.event.loc('title', tr.lang)} · {tk.code}"[:64], callback_data=MyCb(action="ticket", item_id=tk.id))
    b.adjust(1)
    await message.answer(tr("my_title"), reply_markup=b.as_markup())


@router.callback_query(MyCb.filter(F.action == "ticket"))
async def resend_ticket(cb: CallbackQuery, callback_data: MyCb, user: User, tr: Translator, session: AsyncSession, settings: Settings, bot: Bot) -> None:
    ticket = await session.get(Ticket, callback_data.item_id)
    if ticket is None or ticket.user_id != user.id:
        await cb.answer(tr("ci_not_found"), show_alert=True)
        return
    await cb.answer()
    await send_ticket(bot, session, settings, ticket)
