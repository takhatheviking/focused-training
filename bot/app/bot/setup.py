from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.base import BaseStorage
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeDefault
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.handlers import booking, checkin, common, events, fallback, moderation, my
from app.bot.handlers.admin import build_router as build_admin_router
from app.bot.middlewares import DbSessionMiddleware, MenuEscapeMiddleware, ThrottlingMiddleware, UserMiddleware
from app.config import Settings
from app.i18n import LANGS, all_variants, t

MENU_KEYS = ("btn_events", "btn_my", "btn_lang", "btn_help", "btn_admin", "btn_checkin", "btn_cancel")


def create_bot(settings: Settings) -> Bot:
    return Bot(settings.bot_token.get_secret_value(), default=DefaultBotProperties(parse_mode=ParseMode.HTML))


def create_storage(settings: Settings) -> BaseStorage:
    if settings.redis_url:
        from aiogram.fsm.storage.redis import RedisStorage

        return RedisStorage.from_url(settings.redis_url)
    return MemoryStorage()


def create_dispatcher(settings: Settings, session_factory: async_sessionmaker[AsyncSession]) -> Dispatcher:
    dp = Dispatcher(storage=create_storage(settings), settings=settings, session_factory=session_factory)
    throttle = dp["throttle"] = ThrottlingMiddleware()
    dp.message.outer_middleware(throttle)
    dp.callback_query.outer_middleware(throttle)
    dp.message.outer_middleware(MenuEscapeMiddleware({text for key in MENU_KEYS for text in all_variants(key)}))
    dp.update.outer_middleware(DbSessionMiddleware(session_factory))
    dp.update.outer_middleware(UserMiddleware())
    # Order matters: generic handlers (receipt photos, unknown text) must come last.
    dp.include_routers(
        common.router,
        build_admin_router(),
        checkin.router,
        moderation.router,
        events.router,
        my.router,
        booking.router,
        fallback.router,
    )
    return dp


async def set_commands(bot: Bot) -> None:
    for lang in LANGS:
        commands = [
            BotCommand(command="start", description=t(lang, "cmd_start")),
            BotCommand(command="my", description=t(lang, "cmd_my")),
            BotCommand(command="lang", description=t(lang, "cmd_lang")),
            BotCommand(command="help", description=t(lang, "cmd_help")),
        ]
        await bot.set_my_commands(commands, scope=BotCommandScopeDefault(), language_code=None if lang == "ru" else lang)
