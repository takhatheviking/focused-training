"""Admin: Google Sheets mirror — status, linking a spreadsheet, "sync now"."""

import asyncio

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import AdminCb
from app.bot.handlers.admin.settings import is_super
from app.bot.keyboards import admin_back
from app.bot.states import GSheetSG
from app.config import Settings
from app.db.models import User
from app.formatting import esc, fmt_dt
from app.i18n import Translator, t
from app.services import app_settings
from app.services.gsheets import SETTINGS_KEY, describe_error, parse_sheet_id, service_account_email, sheet_url, sheets
from app.services.notify import alert_admins

router = Router(name="admin_gsheet")


async def status_view(session: AsyncSession, tr: Translator, settings: Settings, user: User) -> tuple[str, InlineKeyboardMarkup]:
    email = service_account_email(settings)
    b = InlineKeyboardBuilder()
    if email is None:
        b.row(admin_back(tr))
        return tr("gs_status_no_key"), b.as_markup()
    sheet_id = await sheets.spreadsheet_id(session, settings)
    last = fmt_dt(sheets.last_sync_at, settings.tz, tr.lang, with_weekday=False) if sheets.last_sync_at else tr("gs_never")
    text = tr(
        "gs_status",
        email=esc(email),
        sheet=f'<a href="{sheet_url(sheet_id)}">{tr("gs_btn_open")}</a>' if sheet_id else tr("gs_no_sheet"),
        last=last,
    )
    if sheets.last_error:
        text += "\n" + tr("gs_error_line", error=esc(sheets.last_error))
    if sheet_id:
        b.row(InlineKeyboardButton(text=tr("gs_btn_sync"), callback_data=AdminCb(action="gs_sync").pack()))
        b.row(InlineKeyboardButton(text=tr("gs_btn_open"), url=sheet_url(sheet_id)))
    if is_super(user, settings):
        b.row(InlineKeyboardButton(text=tr("gs_btn_set"), callback_data=AdminCb(action="gs_set").pack()))
    b.row(admin_back(tr))
    return text, b.as_markup()


@router.callback_query(AdminCb.filter(F.action == "gsheet"))
async def cb_gsheet(cb: CallbackQuery, user: User, tr: Translator, session: AsyncSession, settings: Settings) -> None:
    text, kb = await status_view(session, tr, settings, user)
    await cb.answer()
    await cb.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)


@router.callback_query(AdminCb.filter(F.action == "gs_sync"))
async def cb_sync(cb: CallbackQuery, user: User, tr: Translator, session: AsyncSession, settings: Settings) -> None:
    await cb.answer(tr("gs_syncing"))
    try:
        written = await sheets.sync(session, settings, force=True)
    except Exception as e:
        sheets.last_error = describe_error(e)
        await session.rollback()
    else:
        await cb.message.answer(tr("gs_synced", n=written))
    text, kb = await status_view(session, tr, settings, user)
    await cb.message.answer(text, reply_markup=kb, disable_web_page_preview=True)


@router.callback_query(AdminCb.filter(F.action == "gs_set"))
async def cb_set(cb: CallbackQuery, user: User, tr: Translator, settings: Settings, state: FSMContext) -> None:
    if not is_super(user, settings):
        await cb.answer(tr("err_super_only"), show_alert=True)
        return
    await state.set_state(GSheetSG.link)
    await cb.answer()
    await cb.message.answer(tr("gs_prompt_link", email=esc(service_account_email(settings) or "—")))


@router.message(GSheetSG.link, F.text)
async def on_link(message: Message, user: User, tr: Translator, session: AsyncSession, settings: Settings, state: FSMContext, bot: Bot) -> None:
    if not is_super(user, settings):
        await state.clear()
        return
    sheet_id = parse_sheet_id(message.text)
    if sheet_id is None:
        await message.answer(tr("gs_err_link"))
        return
    try:
        title = await asyncio.to_thread(sheets.check_access, settings, sheet_id)
    except Exception as e:
        await message.answer(tr("gs_err_access", error=esc(describe_error(e)), email=esc(service_account_email(settings) or "—")))
        return
    await state.clear()
    await app_settings.set_value(session, SETTINGS_KEY, {"id": sheet_id, "tabs": {}})
    await session.commit()
    await message.answer(tr("gs_connected", title=esc(title)))
    await alert_admins(bot, session, settings, t(settings.default_lang, "gs_changed", by=esc(user.full_name), url=sheet_url(sheet_id)))
    try:
        written = await sheets.sync(session, settings, force=True)
        await message.answer(tr("gs_synced", n=written))
    except Exception as e:
        sheets.last_error = describe_error(e)
        await session.rollback()
        await message.answer(tr("gs_error_line", error=esc(sheets.last_error)))
