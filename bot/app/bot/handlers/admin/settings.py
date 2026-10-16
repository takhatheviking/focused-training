"""Admin: payment card, moderation chat, staff and admins."""

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    KeyboardButton,
    KeyboardButtonRequestUsers,
    Message,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import AdminCb
from app.bot.keyboards import admin_back, main_menu
from app.bot.states import CardSG, StaffSG
from app.config import Settings
from app.db.models import User, UserRole
from app.formatting import esc, fmt_card
from app.i18n import Translator, t
from app.services import app_settings
from app.services.app_settings import CardInfo
from app.services.notify import alert_admins, safe_send

router = Router(name="admin_settings")
SKIP = {"-", "—"}
REQUEST_STAFF, REQUEST_ADMIN = 1, 2


def is_super(user: User, settings: Settings) -> bool:
    """Money-routing settings (card, moderation chat, admins) belong to ADMIN_IDS only."""
    return user.tg_id in settings.admin_ids


def mask(number: str | None) -> str:
    return f"•••• {number[-4:]}" if number else "—"


def back_kb(tr: Translator, *extra: tuple[str, AdminCb]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for text, cb in extra:
        b.button(text=text, callback_data=cb)
    b.adjust(1)
    b.row(admin_back(tr))
    return b.as_markup()


# --- Payment card ---------------------------------------------------------------------

@router.callback_query(AdminCb.filter(F.action == "card"))
async def cb_card(cb: CallbackQuery, tr: Translator, session: AsyncSession, settings: Settings) -> None:
    card = await app_settings.get_card(session, settings)
    text = (
        tr("adm_card_current", number=fmt_card(card.number), holder=esc(card.holder or "—"), bank=esc(card.bank or "—"))
        if card
        else tr("adm_card_none")
    )
    await cb.answer()
    await cb.message.edit_text(text, reply_markup=back_kb(tr, (tr("adm_btn_card_edit"), AdminCb(action="card_edit"))))


@router.callback_query(AdminCb.filter(F.action == "card_edit"))
async def cb_card_edit(cb: CallbackQuery, user: User, tr: Translator, settings: Settings, state: FSMContext) -> None:
    if not is_super(user, settings):
        await cb.answer(tr("err_super_only"), show_alert=True)
        return
    await state.set_state(CardSG.number)
    await cb.answer()
    await cb.message.answer(tr("adm_card_prompt_number"))


@router.message(CardSG.number, F.text)
async def on_card_number(message: Message, tr: Translator, state: FSMContext) -> None:
    digits = "".join(ch for ch in message.text if ch.isdigit())
    if len(digits) != 16:
        await message.answer(tr("adm_card_err_number"))
        return
    await state.update_data(number=digits)
    await state.set_state(CardSG.holder)
    await message.answer(tr("adm_card_prompt_holder"))


@router.message(CardSG.holder, F.text)
async def on_card_holder(message: Message, tr: Translator, state: FSMContext) -> None:
    text = message.text.strip()
    await state.update_data(holder=None if text in SKIP else text[:128])
    await state.set_state(CardSG.bank)
    await message.answer(tr("adm_card_prompt_bank"))


@router.message(CardSG.bank, F.text)
async def on_card_bank(message: Message, user: User, tr: Translator, session: AsyncSession, settings: Settings, state: FSMContext, bot: Bot) -> None:
    text = message.text.strip()
    data = await state.get_data()
    await state.clear()
    if not is_super(user, settings):
        return
    old = await app_settings.get_card(session, settings)
    card = CardInfo(number=data["number"], holder=data.get("holder"), bank=None if text in SKIP else text[:64])
    await app_settings.set_card(session, card)
    await session.commit()
    # Everyone who handles money must notice a card swap.
    notice = t(settings.default_lang, "adm_card_changed", by=esc(user.full_name), old=mask(old.number if old else None), new=mask(card.number))
    await alert_admins(bot, session, settings, notice)
    chat_id, _ = await app_settings.get_moderation_chat(session, settings)
    if chat_id:
        await safe_send(bot, session, chat_id, notice)
    await message.answer(
        tr("adm_card_current", number=fmt_card(card.number), holder=esc(card.holder or "—"), bank=esc(card.bank or "—")),
        reply_markup=back_kb(tr),
    )


# --- Moderation chat ------------------------------------------------------------------

@router.callback_query(AdminCb.filter(F.action == "modchat"))
async def cb_modchat(cb: CallbackQuery, tr: Translator, session: AsyncSession, settings: Settings) -> None:
    chat_id, title = await app_settings.get_moderation_chat(session, settings)
    text = tr("adm_modchat_current", title=esc(title or str(chat_id))) if chat_id else tr("adm_modchat_none")
    extra = [(tr("adm_btn_modchat_test"), AdminCb(action="modchat_test"))] if chat_id else []
    await cb.answer()
    await cb.message.edit_text(text + "\n\n" + tr("adm_modchat_howto"), reply_markup=back_kb(tr, *extra))


@router.callback_query(AdminCb.filter(F.action == "modchat_test"))
async def cb_modchat_test(cb: CallbackQuery, tr: Translator, session: AsyncSession, settings: Settings, bot: Bot) -> None:
    chat_id, _ = await app_settings.get_moderation_chat(session, settings)
    try:
        await bot.send_message(chat_id, tr("adm_modchat_test_msg"))
    except Exception as e:  # noqa: BLE001 - shown to the admin as is
        await cb.answer(tr("adm_modchat_test_fail", error=str(e)[:150]), show_alert=True)
        return
    await cb.answer(tr("adm_modchat_test_ok"), show_alert=True)


@router.callback_query(AdminCb.filter(F.action == "set_modchat"))
async def cb_set_modchat(cb: CallbackQuery, callback_data: AdminCb, user: User, tr: Translator, session: AsyncSession, settings: Settings, bot: Bot) -> None:
    if not is_super(user, settings):
        await cb.answer(tr("err_super_only"), show_alert=True)
        return
    chat_id = int(callback_data.value)
    try:
        chat = await bot.get_chat(chat_id)
        presser = await bot.get_chat_member(chat_id, cb.from_user.id)
    except TelegramAPIError as e:
        await cb.answer(tr("adm_modchat_test_fail", error=str(e)[:150]), show_alert=True)
        return
    # Receipts carry names and phones: only a private chat that the presser administers.
    if chat.username or presser.status not in ("creator", "administrator"):
        await cb.answer(tr("adm_modchat_err_unsafe"), show_alert=True)
        return
    try:
        await bot.send_message(chat_id, tr("adm_modchat_test_msg"))
    except TelegramAPIError as e:
        await cb.answer(tr("adm_modchat_test_fail", error=str(e)[:150]), show_alert=True)
        return
    await app_settings.set_moderation_chat(session, chat_id, chat.title)
    await session.commit()
    await cb.answer(tr("saved"))
    await cb.message.edit_text(tr("adm_modchat_current", title=esc(chat.title or str(chat_id))))
    await alert_admins(bot, session, settings, t(settings.default_lang, "adm_modchat_changed", by=esc(user.full_name), title=esc(chat.title or str(chat_id))))


# --- Staff ----------------------------------------------------------------------------

async def staff_view(session: AsyncSession, tr: Translator, settings: Settings, me: User) -> tuple[str, InlineKeyboardMarkup]:
    rows = (await session.execute(select(User).where(User.role.in_((UserRole.ADMIN, UserRole.STAFF))).order_by(User.role, User.id))).scalars()
    b = InlineKeyboardBuilder()
    lines = [tr("adm_staff_title")]
    for u in rows:
        label = f"{u.full_name}" + (f" (@{u.username})" if u.username else "")
        lines.append(f"{'👑' if u.role == UserRole.ADMIN else '🎫'} {esc(label)}")
        removable = u.id != me.id and u.tg_id not in settings.admin_ids and (u.role != UserRole.ADMIN or is_super(me, settings))
        if removable:
            b.button(text=f"✖️ {label}"[:64], callback_data=AdminCb(action="staff_rm", item_id=u.id))
    b.button(text=tr("adm_btn_add_staff"), callback_data=AdminCb(action="staff_add", value="staff"))
    if is_super(me, settings):
        b.button(text=tr("adm_btn_add_admin"), callback_data=AdminCb(action="staff_add", value="admin"))
    b.adjust(1)
    b.row(admin_back(tr))
    return "\n".join(lines), b.as_markup()


@router.callback_query(AdminCb.filter(F.action == "staff"))
async def cb_staff(cb: CallbackQuery, user: User, tr: Translator, session: AsyncSession, settings: Settings) -> None:
    text, kb = await staff_view(session, tr, settings, user)
    await cb.answer()
    await cb.message.edit_text(text, reply_markup=kb)


@router.callback_query(AdminCb.filter(F.action == "staff_rm"))
async def cb_staff_rm(cb: CallbackQuery, callback_data: AdminCb, user: User, tr: Translator, session: AsyncSession, settings: Settings) -> None:
    target = await session.get(User, callback_data.item_id)
    allowed = target is not None and target.id != user.id and target.tg_id not in settings.admin_ids
    if allowed and (target.role != UserRole.ADMIN or is_super(user, settings)):
        target.role = UserRole.USER
        await session.commit()
    text, kb = await staff_view(session, tr, settings, user)
    await cb.answer(tr("saved"))
    await cb.message.edit_text(text, reply_markup=kb)


@router.callback_query(AdminCb.filter(F.action == "staff_add"))
async def cb_staff_add(cb: CallbackQuery, callback_data: AdminCb, user: User, tr: Translator, settings: Settings, state: FSMContext) -> None:
    if callback_data.value == "admin" and not is_super(user, settings):
        await cb.answer(tr("err_super_only"), show_alert=True)
        return
    request_id = REQUEST_ADMIN if callback_data.value == "admin" else REQUEST_STAFF
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=tr("adm_btn_pick_user"), request_users=KeyboardButtonRequestUsers(request_id=request_id, user_is_bot=False, max_quantity=1, request_name=True, request_username=True))],
            [KeyboardButton(text=tr("btn_cancel"))],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await state.set_state(StaffSG.pick)
    await cb.answer()
    await cb.message.answer(tr("adm_staff_pick_prompt"), reply_markup=kb)


@router.message(StaffSG.pick, F.users_shared)
async def on_staff_picked(message: Message, user: User, tr: Translator, session: AsyncSession, settings: Settings, state: FSMContext) -> None:
    await state.clear()
    role = UserRole.ADMIN if message.users_shared.request_id == REQUEST_ADMIN else UserRole.STAFF
    if role == UserRole.ADMIN and not is_super(user, settings):
        return
    for shared in message.users_shared.users:
        target = (await session.execute(select(User).where(User.tg_id == shared.user_id))).scalar_one_or_none()
        if target is None:
            # They have not opened the bot yet; the role is waiting for them when they do.
            target = User(tg_id=shared.user_id, first_name=shared.first_name, last_name=shared.last_name, username=shared.username)
            session.add(target)
        if target.tg_id in settings.admin_ids:
            continue  # super-admins keep their role
        target.role = role
    await session.commit()
    await message.answer(tr("saved"), reply_markup=main_menu(tr, user))
    text, kb = await staff_view(session, tr, settings, user)
    await message.answer(text, reply_markup=kb)
