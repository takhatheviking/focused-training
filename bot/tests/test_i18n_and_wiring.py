import re
import tomllib
from pathlib import Path

import pytest

from app.bot.handlers.admin.edit import EVENT_FIELDS, TYPE_FIELDS
from app.bot.keyboards import REJECT_REASONS
from app.db.models import EventStatus, OrderStatus, PaymentMethod, TicketStatus
from app.i18n import LANGS
from app.services.event_form import sections

ROOT = Path(__file__).resolve().parents[1]
LOCALES = ROOT / "app" / "i18n" / "locales"
PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")
KEY_PREFIXES = (
    "adm_", "ask_", "booking_", "btn_", "ci_", "cmd_", "edit_", "err_", "event_", "field_", "mod_", "my_",
    "form_", "xls_", "gs_", "issue_", "order_", "pay_", "payment_", "price_", "receipt_", "reject_", "reminder_", "ticket_", "tt_", "wiz_",
)


def catalog(lang: str) -> dict[str, str]:
    with open(LOCALES / f"{lang}.toml", "rb") as fh:
        return tomllib.load(fh)


def dynamic_keys() -> set[str]:
    keys = {f"order_status_{s}" for s in OrderStatus}
    keys |= {f"adm_status_{s}" for s in EventStatus}
    keys |= {f"ci_status_{s}" for s in TicketStatus}
    keys |= {f"reject_{r}" for r in REJECT_REASONS} | {f"reject_btn_{r}" for r in REJECT_REASONS}
    keys |= {f"field_{f}" for f in EVENT_FIELDS + TYPE_FIELDS} | {f"edit_prompt_{f}" for f in EVENT_FIELDS + TYPE_FIELDS}
    keys |= {s.label_key for s in sections(with_ticket=True)}
    keys |= {f"xls_ticket_{s}" for s in TicketStatus} | {f"xls_method_{m}" for m in PaymentMethod}
    return keys


def static_keys() -> set[str]:
    found = set()
    for path in (ROOT / "app").rglob("*.py"):
        for literal in re.findall(r"[\"']([a-z][a-z0-9_]+)[\"']", path.read_text(encoding="utf-8")):
            if literal.startswith(KEY_PREFIXES) and "_" in literal:
                found.add(literal)
    return found


def test_every_used_key_exists_in_russian():
    ru = catalog("ru")
    # Literals that share a prefix with i18n keys but are callback actions, columns or settings.
    not_keys = {
        "edit_field", "event_from_user", "event_id", "order_id", "payment_card", "price_non_negative", "price_tiyin",
        "ticket_type", "ticket_types", "tt_", "tt_add", "tt_capacity", "tt_name", "tt_price", "tt_toggle", "wiz_save",
        "edit_form", "form_example", "form_fix", "form_no_poster", "form_text", "gs_set", "gs_sync",
        "issue_go", "issue_qty", "issue_tt", "tt_id",
    }
    missing = sorted((static_keys() | dynamic_keys()) - set(ru) - not_keys)
    assert missing == []


@pytest.mark.parametrize("lang", [lang for lang in LANGS if lang != "ru"])
def test_translations_match_russian(lang):
    path = LOCALES / f"{lang}.toml"
    if not path.exists():
        pytest.skip(f"{lang}.toml not written yet")
    ru, other = catalog("ru"), catalog(lang)
    assert set(other) == set(ru)
    for key, text in ru.items():
        assert set(PLACEHOLDER.findall(other[key])) == set(PLACEHOLDER.findall(text)), key
    assert other["choose_lang_all"] == ru["choose_lang_all"]


@pytest.mark.parametrize("lang", LANGS)
def test_menu_buttons_are_distinct(lang):
    path = LOCALES / f"{lang}.toml"
    if not path.exists():
        pytest.skip(f"{lang}.toml not written yet")
    c = catalog(lang)
    labels = [c[k] for k in ("btn_events", "btn_my", "btn_lang", "btn_help", "btn_admin", "btn_checkin", "btn_cancel")]
    assert len(set(labels)) == len(labels)


def test_dispatcher_builds(dispatcher):
    from app.config import Settings

    assert Settings(bot_token="123456:TEST", admin_ids="1, 2", _env_file=None).admin_ids == [1, 2]
    used = set(dispatcher.resolve_used_update_types())
    assert {"message", "callback_query", "my_chat_member"} <= used
