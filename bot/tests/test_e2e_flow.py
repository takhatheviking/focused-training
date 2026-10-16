"""Drives the real dispatcher with fake Telegram updates and a fake API session.

Covers the whole journey: admin sets up a card and an event, a user books and sends a
receipt, a moderator approves it, the ticket image goes out, staff let the user in.
Every Telegram API call is recorded instead of hitting the network.
"""

from datetime import UTC, datetime, timedelta
from itertools import count
from typing import Any

import pytest
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramForbiddenError
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import (
    AnswerCallbackQuery,
    GetChatMember,
    GetMe,
    SendDocument,
    SendMessage,
    SendPhoto,
    TelegramMethod,
)
from aiogram.types import (
    CallbackQuery,
    Chat,
    ChatMemberOwner,
    InlineKeyboardMarkup,
    Contact,
    Message,
    PhotoSize,
    SharedUser,
    UsersShared,
    Update,
    User as TgUser,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Settings
from app.db.models import Event, Order, OrderStatus, Payment, Ticket, TicketStatus
from app.services.tickets import CHECKIN_PREFIX

from tests.conftest import E2E_ADMIN as ADMIN, E2E_MOD_CHAT as MOD_CHAT

BUYER = 222
BOT_ID = 999


class FakeSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod] = []
        self.ids = count(1000)
        self.blocked: set[int] = set()  # chats that never opened the bot

    async def close(self) -> None:
        pass

    async def stream_content(self, *args: Any, **kwargs: Any):  # pragma: no cover
        raise NotImplementedError

    async def make_request(self, bot: Bot, method: TelegramMethod, timeout: int | None = None) -> Any:
        self.calls.append(method)
        if isinstance(method, GetMe):
            return TgUser(id=BOT_ID, is_bot=True, first_name="Focused", username="focused_test_bot")
        if isinstance(method, GetChatMember):
            return ChatMemberOwner(user=TgUser(id=method.user_id, is_bot=False, first_name="x"), is_anonymous=False)
        if isinstance(method, (SendMessage, SendPhoto, SendDocument)) and int(method.chat_id) in self.blocked:
            raise TelegramForbiddenError(method=method, message="Forbidden: bot can't initiate conversation with a user")
        if isinstance(method, (SendMessage, SendPhoto, SendDocument)):
            chat = Chat(id=method.chat_id, type="private" if int(method.chat_id) > 0 else "channel")
            extra: dict[str, Any] = {}
            if isinstance(method, SendMessage):
                extra["text"] = method.text
            else:
                extra["caption"] = method.caption
            if isinstance(method, SendPhoto):
                n = next(self.ids)
                extra["photo"] = [PhotoSize(file_id=f"sent-photo-{n}", file_unique_id=f"u{n}", width=1000, height=1450)]
            markup = method.reply_markup if isinstance(method.reply_markup, InlineKeyboardMarkup) else None
            return Message(message_id=next(self.ids), date=datetime.now(UTC), chat=chat, reply_markup=markup, **extra)
        return True  # answerCallbackQuery, edits, setMyCommands, deleteMessage...

    def sent_to(self, chat_id: int, kind: type = SendMessage) -> list[Any]:
        return [c for c in self.calls if isinstance(c, kind) and int(c.chat_id) == chat_id]

    def last_markup(self, chat_id: int, kind: type = SendMessage):
        msgs = [m for m in self.sent_to(chat_id, kind) if isinstance(m.reply_markup, InlineKeyboardMarkup)]
        return msgs[-1].reply_markup


class Harness:
    def __init__(self, dp, bot: Bot, session: FakeSession):
        self.dp, self.bot, self.api = dp, bot, session
        self.update_ids = count(1)
        self.msg_ids = count(1)

    def user(self, uid: int) -> TgUser:
        return TgUser(id=uid, is_bot=False, first_name=f"Name{uid}", last_name="Test", username=f"user{uid}", language_code="ru")

    def message(self, uid: int, **kw) -> Message:
        return Message(message_id=next(self.msg_ids), date=datetime.now(UTC), chat=Chat(id=uid, type="private"), from_user=self.user(uid), **kw)

    async def send(self, uid: int, text: str | None = None, **kw) -> None:
        await self.dp.feed_update(self.bot, Update(update_id=next(self.update_ids), message=self.message(uid, text=text, **kw)))

    async def press(self, uid: int, data: str, chat_id: int | None = None, with_photo: bool = False) -> None:
        msg_kw: dict[str, Any] = {"text": "x"}
        if with_photo:
            msg_kw = {"photo": [PhotoSize(file_id="p", file_unique_id="p", width=1, height=1)], "caption": "<b>review</b>"}
        msg = Message(message_id=next(self.msg_ids), date=datetime.now(UTC), chat=Chat(id=chat_id or uid, type="private" if (chat_id or uid) > 0 else "channel"), **msg_kw)
        cb = CallbackQuery(id=str(next(self.update_ids)), from_user=self.user(uid), chat_instance="ci", data=data, message=msg)
        await self.dp.feed_update(self.bot, Update(update_id=next(self.update_ids), callback_query=cb))

    def answers(self, uid: int) -> list[str]:
        return [c.text for c in self.api.calls if isinstance(c, AnswerCallbackQuery) and c.text]

    def button(self, chat_id: int, startswith: str, kind: type = SendMessage) -> str:
        markup = self.api.last_markup(chat_id, kind)
        for row in markup.inline_keyboard:
            for b in row:
                if b.callback_data and b.callback_data.startswith(startswith):
                    return b.callback_data
        raise AssertionError(f"no button {startswith!r} in {markup}")


@pytest.fixture
async def h(factory: async_sessionmaker, dispatcher):
    # Routers are module-level singletons, so the dispatcher is shared; FSM state is not.
    dispatcher.fsm.storage = MemoryStorage()
    dispatcher["throttle"].limit = 10_000  # scripted users click faster than humans
    api = FakeSession()
    bot = Bot("123456:TEST", session=api, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    return Harness(dispatcher, bot, api)


async def test_full_purchase_journey(h: Harness, factory):
    # --- admin: first start, language, card, event ---------------------------------
    await h.send(ADMIN, "/start")
    await h.press(ADMIN, "lg:ru")
    await h.send(ADMIN, "⚙️ Админка")
    await h.press(ADMIN, "ad:card_edit:0:0:")
    await h.send(ADMIN, "8600 1234 5678 9012")
    await h.send(ADMIN, "FOCUSED LLC")
    await h.send(ADMIN, "Kapitalbank")

    await h.press(ADMIN, "ad:new_event:0:0:")
    assert "[Название RU]" in h.api.sent_to(ADMIN)[-1].text  # the blank form
    starts = (datetime.now(UTC) + timedelta(days=3)).astimezone(Settings(bot_token="1:a", _env_file=None).tz)
    form = f"""[Название RU]
AI Meetup по-русски
[Nomi UZ]
AI Meetup oʻzbekcha
[Title EN]
AI Meetup in English
[Описание RU]
Доклады про LLM <script>
[Описание UZ]
-
[Описание EN]
Talks about LLMs
[Место RU]
IT Park, Ташкент
[Место UZ]
IT Park, Toshkent
[Место EN]
-
[Дата и время]
{starts:%d.%m.%Y} 19:00
[Карта]
-
[Цена]
150 000
[Мест]
50"""
    await h.send(ADMIN, form)
    await h.press(ADMIN, "ad:form_no_poster:0:0:")
    preview = h.api.sent_to(ADMIN)[-1].text
    assert "Описание UZ" in preview  # flagged as untranslated
    await h.press(ADMIN, "ad:wiz_save:0:0:publish")

    async with factory() as s:
        event = (await s.execute(select(Event))).scalar_one()
        assert event.status == "published" and event.ticket_types[0].price_tiyin == 15_000_000
        assert event.loc("title", "uz") == "AI Meetup oʻzbekcha" and event.loc("venue", "en") == "IT Park, Ташкент"

    # --- buyer: start from the shared event link, choose language ---------------------
    await h.send(BUYER, f"/start ev_{event.id}")
    await h.press(BUYER, "lg:uz")
    card = h.api.sent_to(BUYER)[-1]
    assert "AI Meetup oʻzbekcha" in card.text and "IT Park, Toshkent" in card.text
    assert "&lt;script&gt;" in card.text  # no UZ description: the Russian one, escaped
    await h.press(BUYER, h.button(BUYER, "ev:buy"))
    await h.press(BUYER, h.button(BUYER, "qt:"))  # 1 ticket
    await h.send(BUYER, contact=Contact(phone_number="998901112233", first_name="Buyer", user_id=BUYER))
    await h.press(BUYER, h.button(BUYER, "bk:confirm"))
    instructions = h.api.sent_to(BUYER)[-1].text
    assert "8600 1234 5678 9012" in instructions and "149 998" in instructions  # unique amount for order #1

    # --- buyer sends the receipt; it lands in the moderation chat ---------------------
    await h.send(BUYER, photo=[PhotoSize(file_id="receipt-1", file_unique_id="rcpt-1", width=800, height=1600)])
    posted = h.api.sent_to(MOD_CHAT, SendPhoto)
    assert len(posted) == 1 and posted[0].photo == "receipt-1"
    approve = h.button(MOD_CHAT, "md:ok", SendPhoto)

    # A random user pressing Approve is refused (not admin, and the fake API says "owner" only in the mod chat).
    async with factory() as s:
        assert (await s.execute(select(Order))).scalar_one().status == OrderStatus.UNDER_REVIEW

    # --- moderator approves: ticket image goes to the buyer -----------------------------
    await h.press(ADMIN, approve, chat_id=MOD_CHAT, with_photo=True)
    async with factory() as s:
        order = (await s.execute(select(Order))).scalar_one()
        ticket = (await s.execute(select(Ticket))).scalar_one()
        payment = (await s.execute(select(Payment))).scalar_one()
    assert order.status == OrderStatus.PAID and payment.status == "approved"
    assert ticket.delivered_at is not None and ticket.image_file_id
    ticket_photos = h.api.sent_to(BUYER, SendPhoto)
    assert len(ticket_photos) == 1 and ticket.code in ticket_photos[0].caption

    # Second press on the same button: nothing new is issued.
    await h.press(ADMIN, approve, chat_id=MOD_CHAT, with_photo=True)
    async with factory() as s:
        assert len((await s.execute(select(Ticket))).scalars().all()) == 1

    # --- "My tickets" re-sends the cached image --------------------------------------
    await h.send(BUYER, "🎫 Chiptalarim")
    await h.press(BUYER, h.button(BUYER, "my:ticket"))
    assert len(h.api.sent_to(BUYER, SendPhoto)) == 2

    # --- entrance: the buyer scanning their own QR just gets the ticket; staff lets in -----
    await h.send(ADMIN, f"/start {CHECKIN_PREFIX}{ticket.token}")
    await h.press(ADMIN, h.button(ADMIN, "ci:"))
    await h.press(ADMIN, f"ci:{ticket.token}")  # double scan
    async with factory() as s:
        t = await s.get(Ticket, ticket.id)
        assert t.status == TicketStatus.USED and t.checked_in_at is not None

    # --- admin views the event card, exports participants ------------------------------
    await h.press(ADMIN, f"ad:ev:{event.id}:0:")
    await h.press(ADMIN, f"ad:export:{event.id}:0:")
    assert any(isinstance(c, SendDocument) for c in h.api.calls)


async def test_rejected_receipt_and_retry(h: Harness, factory):
    await h.send(ADMIN, "/start")
    await h.press(ADMIN, "lg:ru")
    async with factory() as s:
        from tests.conftest import make_event

        event, tt = await make_event(s, capacity=2)
    from app.services import app_settings

    async with factory() as s:
        await app_settings.set_card(s, app_settings.CardInfo("8600123412341234"))
        await s.commit()

    await h.send(BUYER, "/start")
    await h.press(BUYER, "lg:en")
    await h.press(BUYER, f"ev:buy:{event.id}:0")
    await h.press(BUYER, h.button(BUYER, "qt:"))
    await h.send(BUYER, contact=Contact(phone_number="+998901112233", first_name="B", user_id=BUYER))
    await h.press(BUYER, h.button(BUYER, "bk:confirm"))
    await h.send(BUYER, photo=[PhotoSize(file_id="r1", file_unique_id="r1", width=1, height=1)])

    reject = h.button(MOD_CHAT, "md:no", SendPhoto)
    await h.press(ADMIN, reject, chat_id=MOD_CHAT, with_photo=True)
    payment_id = reject.split(":")[2]
    await h.press(ADMIN, f"md:reason:{payment_id}:amount", chat_id=MOD_CHAT, with_photo=True)

    async with factory() as s:
        order = (await s.execute(select(Order))).scalar_one()
    assert order.status == OrderStatus.PENDING
    assert "amount" in h.api.sent_to(BUYER)[-1].text.lower() or "summa" in h.api.sent_to(BUYER)[-1].text.lower()

    # The user just drops a new screenshot into the chat, no button needed.
    await h.send(BUYER, photo=[PhotoSize(file_id="r2", file_unique_id="r2", width=1, height=1)])
    async with factory() as s:
        order = (await s.execute(select(Order))).scalar_one()
    assert order.status == OrderStatus.UNDER_REVIEW and order.proof_attempts == 2
    assert len(h.api.sent_to(MOD_CHAT, SendPhoto)) == 2


async def test_menu_button_leaves_checkin_mode(h: Harness, factory):
    await h.send(ADMIN, "/start")
    await h.press(ADMIN, "lg:ru")
    await h.send(ADMIN, "📷 Проверка билетов")  # enters "type a ticket code" mode
    await h.send(ADMIN, "🎟 Ивенты")  # must open the event list, not look up a ticket called "🎟 Ивенты"
    assert "Билет не найден" not in h.api.sent_to(ADMIN)[-1].text


async def test_admin_issues_ticket_to_guest_without_telegram(h: Harness, factory):
    from tests.conftest import make_event

    await h.send(ADMIN, "/start")
    await h.press(ADMIN, "lg:ru")
    async with factory() as s:
        event, _ = await make_event(s, capacity=5)
    await h.press(ADMIN, f"ad:issue:{event.id}:0:")
    await h.press(ADMIN, h.button(ADMIN, "ad:issue_qty"))  # 1 ticket
    await h.send(ADMIN, "Иван Петров, +998 90 123 45 67")
    await h.press(ADMIN, h.button(ADMIN, "ad:issue_go"))
    assert len(h.api.sent_to(ADMIN, SendPhoto)) == 1  # the ticket comes to the admin to forward
    async with factory() as s:
        order = (await s.execute(select(Order))).scalar_one()
        ticket = (await s.execute(select(Ticket))).scalar_one()
    assert (order.guest_name, order.guest_phone, order.amount_tiyin) == ("Иван Петров", "+998901234567", 0)
    assert ticket.holder_name == "Иван Петров" and ticket.delivered_at is not None


async def test_issue_to_telegram_user_who_never_opened_the_bot(h: Harness, factory):
    from tests.conftest import make_event

    guest = 333
    h.api.blocked.add(guest)
    await h.send(ADMIN, "/start")
    await h.press(ADMIN, "lg:ru")
    async with factory() as s:
        event, _ = await make_event(s, capacity=5)
    await h.press(ADMIN, f"ad:issue:{event.id}:0:")
    await h.press(ADMIN, h.button(ADMIN, "ad:issue_qty"))
    await h.send(ADMIN, users_shared=UsersShared(request_id=3, users=[SharedUser(user_id=guest, first_name="Гость", username="guest")]))
    await h.press(ADMIN, h.button(ADMIN, "ad:issue_go"))
    assert len(h.api.sent_to(ADMIN, SendPhoto)) == 1  # Telegram refused the guest, so the admin got it
    assert any("ещё не запускал бота" in m.text for m in h.api.sent_to(ADMIN))
    async with factory() as s:
        ticket = (await s.execute(select(Ticket))).scalar_one()
    assert ticket.user.tg_id == guest and ticket.holder_name == "Гость"
