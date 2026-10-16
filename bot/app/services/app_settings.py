"""Settings admins can change from the bot. Values in the DB win over .env fallbacks."""

from dataclasses import dataclass

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import AppSetting

PAYMENT_CARD = "payment_card"
MODERATION_CHAT = "moderation_chat"


@dataclass
class CardInfo:
    number: str
    holder: str | None = None
    bank: str | None = None


async def get_value(session: AsyncSession, key: str) -> dict | None:
    row = await session.get(AppSetting, key)
    return row.value if row else None


async def set_value(session: AsyncSession, key: str, value: dict) -> None:
    stmt = insert(AppSetting).values(key=key, value=value)
    stmt = stmt.on_conflict_do_update(index_elements=[AppSetting.key], set_={"value": value, "updated_at": stmt.excluded.updated_at})
    await session.execute(stmt)


async def get_card(session: AsyncSession, settings: Settings) -> CardInfo | None:
    value = await get_value(session, PAYMENT_CARD)
    if value and value.get("number"):
        return CardInfo(value["number"], value.get("holder"), value.get("bank"))
    if settings.payment_card_number:
        return CardInfo(settings.payment_card_number, settings.payment_card_holder, settings.payment_card_bank)
    return None


async def set_card(session: AsyncSession, card: CardInfo) -> None:
    await set_value(session, PAYMENT_CARD, {"number": card.number, "holder": card.holder, "bank": card.bank})


async def get_moderation_chat(session: AsyncSession, settings: Settings) -> tuple[int | None, str | None]:
    value = await get_value(session, MODERATION_CHAT)
    if value and value.get("chat_id"):
        return int(value["chat_id"]), value.get("title")
    return settings.moderation_chat_id, None


async def set_moderation_chat(session: AsyncSession, chat_id: int, title: str | None) -> None:
    await set_value(session, MODERATION_CHAT, {"chat_id": chat_id, "title": title})
