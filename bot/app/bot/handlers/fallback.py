from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from app.bot.keyboards import main_menu
from app.db.models import User
from app.i18n import Translator

router = Router(name="fallback")


@router.message(F.chat.type == "private")
async def unknown_message(message: Message, user: User, tr: Translator) -> None:
    await message.answer(tr("unknown_input"), reply_markup=main_menu(tr, user))


@router.callback_query()
async def stale_callback(cb: CallbackQuery, tr: Translator) -> None:
    # Buttons from old messages (e.g. after a restart or role change).
    await cb.answer(tr("stale_button"), show_alert=True)
