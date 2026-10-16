from aiogram import Router

from app.bot.handlers.admin import create, edit, events, gsheet, issue, queue, settings
from app.bot.middlewares import IsAdmin


def build_router() -> Router:
    router = Router(name="admin")
    router.message.filter(IsAdmin)
    router.callback_query.filter(IsAdmin)
    router.include_routers(events.router, create.router, edit.router, settings.router, queue.router, gsheet.router, issue.router)
    return router
