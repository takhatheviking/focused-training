import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.filters import Filter
from aiogram.types import TelegramObject
from aiogram.types import User as TgUser
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.models import User, UserRole
from app.i18n import Translator

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]


class DbSessionMiddleware(BaseMiddleware):
    """One session per update; handlers commit explicitly before any side effect."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self.session_factory = session_factory

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        async with self.session_factory() as session:
            data["session"] = session
            return await handler(event, data)


async def upsert_user(session: AsyncSession, tg: TgUser, settings: Settings) -> User:
    user = (await session.execute(select(User).where(User.tg_id == tg.id))).scalar_one_or_none()
    changed = False
    if user is None:
        # A new user's first updates can arrive in parallel: ON CONFLICT instead of a racing INSERT.
        await session.execute(insert(User).values(tg_id=tg.id, role=UserRole.USER).on_conflict_do_nothing(index_elements=[User.tg_id]))
        user = (await session.execute(select(User).where(User.tg_id == tg.id))).scalar_one()
        changed = True
    for attr, value in (("username", tg.username), ("first_name", tg.first_name), ("last_name", tg.last_name)):
        if getattr(user, attr) != value:
            setattr(user, attr, value)
            changed = True
    if tg.id in settings.admin_ids and user.role != UserRole.ADMIN:
        user.role = UserRole.ADMIN
        changed = True
    if user.bot_blocked:
        user.bot_blocked = False
        changed = True
    if changed:
        await session.commit()
    return user


class UserMiddleware(BaseMiddleware):
    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        tg_user: TgUser | None = data.get("event_from_user")
        settings: Settings = data["settings"]
        if tg_user is None or tg_user.is_bot:
            data["tr"] = Translator(settings.default_lang)
            return await handler(event, data)
        user = await upsert_user(data["session"], tg_user, settings)
        data["user"] = user
        data["tr"] = Translator(user.lang or settings.default_lang)
        return await handler(event, data)


class ThrottlingMiddleware(BaseMiddleware):
    """Drops a user's updates beyond `limit` per `window` seconds (flood and script abuse)."""

    def __init__(self, limit: int = 20, window: float = 10.0):
        self.limit, self.window = limit, window
        self.hits: dict[int, deque[float]] = defaultdict(deque)

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        tg_user: TgUser | None = data.get("event_from_user")
        if tg_user is not None:
            now = time.monotonic()
            hits = self.hits[tg_user.id]
            while hits and hits[0] < now - self.window:
                hits.popleft()
            if len(hits) >= self.limit:
                return None
            hits.append(now)
        return await handler(event, data)


class MenuEscapeMiddleware(BaseMiddleware):
    """Reply-keyboard buttons and commands always work: they end whatever dialog step the
    user is in (check-in mode, admin wizards), so "🎟 Events" is never taken as input."""

    def __init__(self, menu_texts: set[str]):
        self.menu_texts = menu_texts

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        text = getattr(event, "text", None)
        if text and data.get("raw_state") and (text in self.menu_texts or text.startswith("/")):
            await data["state"].clear()
            data["raw_state"] = None
        return await handler(event, data)


class RoleFilter(Filter):
    def __init__(self, *roles: UserRole):
        self.roles = set(roles)

    async def __call__(self, event: TelegramObject, user: User | None = None) -> bool:
        return user is not None and user.role in self.roles


IsAdmin = RoleFilter(UserRole.ADMIN)
IsStaff = RoleFilter(UserRole.ADMIN, UserRole.STAFF)
