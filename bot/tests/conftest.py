"""Tests run against a real PostgreSQL (row locks and partial indexes matter here).

Start it with `docker compose up -d db`; the test database is created automatically.
Override with TEST_DATABASE_URL.
"""

import os
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import Base, Event, EventStatus, TicketType, User

E2E_ADMIN, E2E_MOD_CHAT = 111, -100500
TEST_URL = os.environ.get("TEST_DATABASE_URL", "postgresql+asyncpg://focused:focused@localhost:5432/focused_bot_test")


async def _ensure_database(url: str) -> None:
    dsn = url.replace("postgresql+asyncpg", "postgresql")
    base, name = dsn.rsplit("/", 1)
    conn = await asyncpg.connect(f"{base}/postgres")
    try:
        if not await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", name):
            await conn.execute(f'CREATE DATABASE "{name}"')
    finally:
        await conn.close()


@pytest.fixture(scope="session")
async def engine():
    await _ensure_database(TEST_URL)
    eng = create_async_engine(TEST_URL, pool_size=20)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
async def factory(engine):
    async with engine.begin() as conn:
        tables = ", ".join(t.name for t in reversed(Base.metadata.sorted_tables))
        await conn.exec_driver_sql(f"TRUNCATE {tables} RESTART IDENTITY CASCADE")
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
async def session(factory):
    async with factory() as s:
        yield s


async def make_user(session: AsyncSession, tg_id: int, **kw) -> User:
    user = User(tg_id=tg_id, first_name=f"User{tg_id}", phone="+998900000000", lang="ru", **kw)
    session.add(user)
    await session.commit()
    return user


async def make_event(session: AsyncSession, *, price_tiyin: int = 15_000_000, capacity: int = 10, starts_in_days: int = 7) -> tuple[Event, TicketType]:
    event = Event(title="AI Meetup", starts_at=datetime.now(UTC) + timedelta(days=starts_in_days), status=EventStatus.PUBLISHED)
    session.add(event)
    await session.flush()
    tt = TicketType(event_id=event.id, name="Standard", price_tiyin=price_tiyin, capacity=capacity)
    session.add(tt)
    await session.commit()
    return event, tt


@pytest.fixture(scope="session")
def dispatcher(engine):
    """One dispatcher per test run: aiogram routers can be attached to a parent only once."""
    from app.bot.setup import create_dispatcher
    from app.config import Settings

    settings = Settings(bot_token="123456:TEST", admin_ids=str(E2E_ADMIN), moderation_chat_id=E2E_MOD_CHAT, _env_file=None)
    return create_dispatcher(settings, async_sessionmaker(engine, expire_on_commit=False))
