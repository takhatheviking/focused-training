from contextlib import suppress

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, ChatMemberUpdated, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import LangCb
from app.bot.handlers import checkin, events
from app.bot.keyboards import lang_kb, main_menu
from app.config import Settings
from app.db.models import User
from app.formatting import esc
from app.i18n import Translator, all_variants
from app.services.tickets import CHECKIN_PREFIX

router = Router(name="common")
router.message.filter(F.chat.type == "private")


async def route_start_payload(
    message: Message, payload: str | None, *, user: User, tr: Translator, session: AsyncSession, settings: Settings, bot: Bot
) -> None:
    if payload and payload.startswith("ev_") and payload[3:].isdigit():
        await events.send_event_card(message, int(payload[3:]), user=user, tr=tr, session=session, settings=settings)
        return
    if payload and payload.startswith(CHECKIN_PREFIX):
        await checkin.handle_scan(
            message, payload[len(CHECKIN_PREFIX):], user=user, tr=tr, session=session, settings=settings, bot=bot
        )
        return
    await message.answer(tr("welcome", name=esc(user.first_name or "")), reply_markup=main_menu(tr, user))


@router.message(CommandStart())
async def cmd_start(
    message: Message,
    command: CommandObject,
    user: User,
    tr: Translator,
    session: AsyncSession,
    settings: Settings,
    state: FSMContext,
    bot: Bot,
) -> None:
    await state.clear()
    if user.lang is None:
        # First visit: ask for a language, then continue with the deep link (event or ticket).
        await state.update_data(start_payload=command.args)
        await message.answer(Translator(None)("choose_lang_all"), reply_markup=lang_kb())
        return
    await route_start_payload(message, command.args, user=user, tr=tr, session=session, settings=settings, bot=bot)


@router.message(Command("lang"))
@router.message(F.text.in_(all_variants("btn_lang")))
async def cmd_lang(message: Message) -> None:
    await message.answer(Translator(None)("choose_lang_all"), reply_markup=lang_kb())


@router.callback_query(LangCb.filter())
async def on_lang(
    cb: CallbackQuery,
    callback_data: LangCb,
    user: User,
    session: AsyncSession,
    settings: Settings,
    state: FSMContext,
    bot: Bot,
) -> None:
    user.lang = callback_data.lang
    await session.commit()
    tr = Translator(user.lang)
    await cb.answer()
    with suppress(TelegramBadRequest):
        await cb.message.delete()
    payload = (await state.get_data()).get("start_payload")
    await state.update_data(start_payload=None)
    if payload:
        # Came from a shared link: show the menu keyboard first, then the event or ticket.
        await cb.message.answer(tr("welcome", name=esc(user.first_name or "")), reply_markup=main_menu(tr, user))
    await route_start_payload(cb.message, payload, user=user, tr=tr, session=session, settings=settings, bot=bot)


@router.message(Command("help"))
@router.message(F.text.in_(all_variants("btn_help")))
async def cmd_help(message: Message, tr: Translator, user: User, settings: Settings) -> None:
    support = f"@{settings.support_username}" if settings.support_username else "—"
    await message.answer(tr("help", support=support), reply_markup=main_menu(tr, user))


@router.message(Command("cancel"))
@router.message(F.text.in_(all_variants("btn_cancel")))
async def cmd_cancel(message: Message, tr: Translator, user: User, state: FSMContext) -> None:
    await state.clear()
    await message.answer(tr("cancelled"), reply_markup=main_menu(tr, user))


# Private chat membership: the user blocked or unblocked the bot.
@router.my_chat_member(F.chat.type == "private")
async def on_private_member(event: ChatMemberUpdated, user: User, session: AsyncSession) -> None:
    user.bot_blocked = event.new_chat_member.status == "kicked"
    await session.commit()
