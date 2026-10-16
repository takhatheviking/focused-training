import asyncio
import logging

from aiogram import Bot, Dispatcher

from app.bot.setup import create_bot, create_dispatcher, set_commands
from app.config import get_settings
from app.db.session import create_engine, create_session_factory
from app.worker import run_worker


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    bot = create_bot(settings)
    dp = create_dispatcher(settings, session_factory)
    worker_task: asyncio.Task | None = None

    @dp.startup()
    async def on_startup(bot: Bot, dispatcher: Dispatcher) -> None:
        nonlocal worker_task
        await set_commands(bot)
        worker_task = asyncio.create_task(run_worker(bot, session_factory, settings))

    @dp.shutdown()
    async def on_shutdown() -> None:
        if worker_task:
            worker_task.cancel()
        await engine.dispose()

    await bot.delete_webhook(drop_pending_updates=False)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
