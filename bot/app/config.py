from functools import lru_cache
from typing import Annotated
from zoneinfo import ZoneInfo

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: SecretStr
    database_url: str = "postgresql+asyncpg://focused:focused@localhost:5432/focused_bot"
    redis_url: str | None = None

    # Telegram ids of super-admins. They get the admin role on /start and are never demoted.
    admin_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)

    timezone: str = "Asia/Tashkent"
    default_lang: str = "ru"

    # How long a booking keeps seats while the user transfers money and sends the receipt.
    hold_minutes: int = 30
    # After this many rejected receipts the order is cancelled and seats are released.
    max_proof_attempts: int = 3
    max_tickets_per_order: int = 5
    # Shave 1..99 sum off each card-transfer amount so moderators can match transfers to orders.
    unique_amounts: bool = True
    # Alert admins when a receipt waits for a decision longer than this.
    review_alert_minutes: int = 120

    # Fallbacks; admins can override both from the bot (/admin -> Settings).
    moderation_chat_id: int | None = None
    payment_card_number: str | None = None
    payment_card_holder: str | None = None
    payment_card_bank: str | None = None

    # Google Sheets mirror (docs/GOOGLE_SHEETS.md). The spreadsheet link can also be set from the bot.
    google_service_account_file: str | None = "secrets/google-service-account.json"
    google_sheet_id: str | None = None
    sheets_sync_minutes: int = 2

    support_username: str | None = None
    ticket_font_path: str | None = None

    @field_validator("admin_ids", mode="before")
    @classmethod
    def _split_ids(cls, v):
        if isinstance(v, str):
            return [int(x) for x in v.replace(" ", "").split(",") if x]
        if isinstance(v, int):
            return [v]
        return v

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


@lru_cache
def get_settings() -> Settings:
    return Settings()
