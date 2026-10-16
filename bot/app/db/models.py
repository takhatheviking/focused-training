from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {dict[str, Any]: JSONB, datetime: DateTime(timezone=True)}


class UserRole(StrEnum):
    USER = "user"
    STAFF = "staff"  # can check tickets in at the entrance
    ADMIN = "admin"  # manages events, settings and staff


class EventStatus(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    CANCELLED = "cancelled"


class OrderStatus(StrEnum):
    PENDING = "pending"  # seats held, waiting for the transfer receipt
    UNDER_REVIEW = "under_review"  # receipt sent to moderators
    PAID = "paid"  # approved, tickets issued
    EXPIRED = "expired"  # hold ran out before a receipt arrived
    CANCELLED = "cancelled"
    REFUND_REQUIRED = "refund_required"  # money received but no seats left
    REFUNDED = "refunded"


ACTIVE_ORDER_STATUSES = (OrderStatus.PENDING, OrderStatus.UNDER_REVIEW)


class CancelReason(StrEnum):
    USER = "user"  # pressed "cancel booking" before sending a receipt
    EVENT = "event"  # the event was cancelled
    REJECTED = "rejected"  # receipt rejected too many times


class PaymentMethod(StrEnum):
    CARD_TRANSFER = "card_transfer"
    FREE = "free"
    MANUAL = "manual"  # issued by an admin: guests, speakers, cash payments
    # Planned: CLICK = "click", PAYME = "payme" (through the focused.uz API)


class PaymentStatus(StrEnum):
    SUBMITTED = "submitted"  # receipt waiting for a moderator
    APPROVED = "approved"
    REJECTED = "rejected"


class TicketStatus(StrEnum):
    VALID = "valid"
    USED = "used"
    CANCELLED = "cancelled"


class LocalizedMixin:
    """Russian text lives in the regular columns; Uzbek and English go to `i18n`,
    e.g. {"uz": {"title": "...", "description": "..."}, "en": {...}}."""

    i18n: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default=text("'{}'::jsonb"))

    def loc(self, field: str, lang: str | None) -> str | None:
        """The field in the viewer's language; falls back to Russian when not translated."""
        if lang and lang != "ru":
            value = (self.i18n or {}).get(lang, {}).get(field)
            if value:
                return value
        return getattr(self, field)

    def set_loc(self, field: str, values: dict[str, str | None]) -> None:
        """Set a field in several languages ({"ru": ..., "uz": ..., "en": ...})."""
        if "ru" in values:
            setattr(self, field, values["ru"])
        i18n = {lang: dict(fields) for lang, fields in (self.i18n or {}).items()}  # new dict: JSONB change tracking
        for lang, value in values.items():
            if lang == "ru":
                continue
            if value:
                i18n.setdefault(lang, {})[field] = value
            else:
                i18n.get(lang, {}).pop(field, None)
        self.i18n = i18n


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(128))
    last_name: Mapped[str | None] = mapped_column(String(128))
    phone: Mapped[str | None] = mapped_column(String(32))
    lang: Mapped[str | None] = mapped_column(String(2))
    role: Mapped[str] = mapped_column(String(16), default=UserRole.USER, server_default=UserRole.USER)
    bot_blocked: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))

    @property
    def full_name(self) -> str:
        return " ".join(p for p in (self.first_name, self.last_name) if p) or (self.username or str(self.tg_id))


class Event(LocalizedMixin, TimestampMixin, Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    poster_file_id: Mapped[str | None] = mapped_column(String(255))
    venue: Mapped[str | None] = mapped_column(String(255))  # place name and address
    map_url: Mapped[str | None] = mapped_column(String(512))
    starts_at: Mapped[datetime]
    ends_at: Mapped[datetime | None]
    status: Mapped[str] = mapped_column(String(16), default=EventStatus.DRAFT, server_default=EventStatus.DRAFT)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    ticket_types: Mapped[list["TicketType"]] = relationship(
        back_populates="event", order_by="TicketType.sort_order, TicketType.id", lazy="selectin"
    )

    __table_args__ = (Index("ix_events_status_starts_at", "status", "starts_at"),)


class TicketType(LocalizedMixin, Base):
    __tablename__ = "ticket_types"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    # Money is stored in minor units (tiyin, 1 sum = 100 tiyin), as Payme expects later.
    price_tiyin: Mapped[int] = mapped_column(BigInteger)
    capacity: Mapped[int] = mapped_column(Integer)
    # Seats taken by active holds plus paid orders. Changed only by conditional UPDATEs.
    reserved: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))

    event: Mapped[Event] = relationship(back_populates="ticket_types", lazy="joined", innerjoin=True)

    __table_args__ = (
        CheckConstraint("price_tiyin >= 0", name="price_non_negative"),
        CheckConstraint("reserved >= 0 AND reserved <= capacity", name="reserved_within_capacity"),
    )

    @property
    def available(self) -> int:
        return max(self.capacity - self.reserved, 0)


class Order(TimestampMixin, Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="RESTRICT"))
    ticket_type_id: Mapped[int] = mapped_column(ForeignKey("ticket_types.id", ondelete="RESTRICT"))
    quantity: Mapped[int] = mapped_column(Integer)
    unit_price_tiyin: Mapped[int] = mapped_column(BigInteger)
    amount_tiyin: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(20), default=OrderStatus.PENDING)
    payment_method: Mapped[str] = mapped_column(String(20))
    # True while this order's quantity is counted in ticket_types.reserved.
    seats_held: Mapped[bool] = mapped_column(Boolean, default=False)
    expires_at: Mapped[datetime | None]
    proof_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    paid_at: Mapped[datetime | None]
    # Last 4 digits of the card the buyer was told to pay to (the card can change later).
    card_last4: Mapped[str | None] = mapped_column(String(4))
    # Manual issuance: who issued it, and the guest when they have no Telegram account
    # (then the order belongs to the issuing admin, who forwards the ticket).
    issued_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    guest_name: Mapped[str | None] = mapped_column(String(255))
    guest_phone: Mapped[str | None] = mapped_column(String(32))
    cancel_reason: Mapped[str | None] = mapped_column(String(16))  # user | event | rejected
    refunded_at: Mapped[datetime | None]
    refunded_by_tg_id: Mapped[int | None] = mapped_column(BigInteger)

    # innerjoin keeps SELECT ... FOR UPDATE legal (Postgres refuses to lock outer-joined rows).
    user: Mapped[User] = relationship(lazy="joined", innerjoin=True, foreign_keys=[user_id])
    event: Mapped[Event] = relationship(lazy="joined", innerjoin=True)
    ticket_type: Mapped[TicketType] = relationship(lazy="joined", innerjoin=True)
    issued_by: Mapped[User | None] = relationship(lazy="selectin", foreign_keys=[issued_by_id])

    @property
    def buyer_name(self) -> str:
        return self.guest_name or self.user.full_name

    @property
    def buyer_phone(self) -> str | None:
        return self.guest_phone if self.guest_name else self.user.phone

    @property
    def buyer_username(self) -> str | None:
        return None if self.guest_name else self.user.username

    __table_args__ = (
        CheckConstraint("quantity > 0", name="quantity_positive"),
        Index("ix_orders_status_expires_at", "status", "expires_at"),
        Index("ix_orders_user_id_created_at", "user_id", "created_at"),
        # A user can have only one booking in progress per event (protects against double taps).
        Index(
            "uq_orders_one_active_per_user_event",
            "user_id",
            "event_id",
            unique=True,
            postgresql_where=text("status IN ('pending', 'under_review')"),
        ),
    )


class Payment(TimestampMixin, Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    method: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20))
    amount_tiyin: Mapped[int] = mapped_column(BigInteger)

    # Card transfer: the receipt and its moderation trail.
    proof_file_id: Mapped[str | None] = mapped_column(String(255))
    proof_file_unique_id: Mapped[str | None] = mapped_column(String(64), index=True)
    proof_kind: Mapped[str | None] = mapped_column(String(16))  # photo | document
    review_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    review_message_id: Mapped[int | None] = mapped_column(BigInteger)
    reviewed_by_tg_id: Mapped[int | None] = mapped_column(BigInteger)
    reviewed_by_name: Mapped[str | None] = mapped_column(String(128))
    reviewed_at: Mapped[datetime | None]
    reject_reason: Mapped[str | None] = mapped_column(String(32))
    # Last time admins were alerted that this receipt still waits for a decision.
    alerted_at: Mapped[datetime | None]

    # Online gateways (Click/Payme via focused.uz) will use these.
    external_id: Mapped[str | None] = mapped_column(String(128))
    raw: Mapped[dict[str, Any] | None]

    order: Mapped[Order] = relationship(lazy="joined", innerjoin=True)


class Ticket(Base):
    __tablename__ = "tickets"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="RESTRICT"), index=True)
    ticket_type_id: Mapped[int] = mapped_column(ForeignKey("ticket_types.id", ondelete="RESTRICT"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    # Short code people can read out loud at the door.
    code: Mapped[str] = mapped_column(String(16), unique=True)
    # Unguessable secret inside the QR code (t.me/<bot>?start=t_<token>).
    token: Mapped[str] = mapped_column(String(48), unique=True)
    holder_name: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16), default=TicketStatus.VALID)
    checked_in_at: Mapped[datetime | None]
    checked_in_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    image_file_id: Mapped[str | None] = mapped_column(String(255))
    # NULL until the ticket reached the user; the worker retries undelivered ones.
    delivered_at: Mapped[datetime | None]
    delivery_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    event: Mapped[Event] = relationship(lazy="joined", innerjoin=True)
    ticket_type: Mapped[TicketType] = relationship(lazy="joined", innerjoin=True)
    user: Mapped[User] = relationship(lazy="joined", innerjoin=True, foreign_keys=[user_id])
    order: Mapped["Order"] = relationship(lazy="joined", innerjoin=True)


class Notification(Base):
    """Remembers one-off messages (event reminders) so they are sent once."""

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(32))
    sent_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (UniqueConstraint("user_id", "event_id", "kind", name="uq_notifications_user_event_kind"),)


class AppSetting(Base):
    """Runtime settings editable from the admin panel (payment card, moderation chat)."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict[str, Any]]
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
