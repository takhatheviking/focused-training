from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine


def create_engine(database_url: str) -> AsyncEngine:
    # hide_parameters: errors would otherwise log phones and card numbers.
    return create_async_engine(database_url, pool_size=10, max_overflow=10, pool_pre_ping=True, hide_parameters=True)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
