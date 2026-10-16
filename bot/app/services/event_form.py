"""The one-message event form.

The admin gets a template, fills every section and sends it back as a single message:

    [Название RU]
    AI Meetup Tashkent
    [Название UZ]
    ...
    [Дата и время]
    25.10.2026 19:00

Section labels are accepted in any interface language, so a form copied in Russian
works for an admin whose bot is in Uzbek. Texts (title, description, place) come in
RU / UZ / EN; Russian is required, an empty translation falls back to Russian.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from app.db.models import Event
from app.formatting import esc, parse_local_dt, parse_money_to_tiyin
from app.i18n import LANGS, t

TEXT_FIELDS = ("title", "description", "venue")
LANG_TAG = {"ru": "RU", "uz": "UZ", "en": "EN"}
LIMITS = {"title": 255, "description": 3500, "venue": 255}
SKIP = {"-", "—"}
HEADER_RE = re.compile(r"^[ \t]*\[([^\]\n]+)\][ \t]*$", re.MULTILINE)


@dataclass(frozen=True)
class Section:
    key: str  # "title.ru", "starts_at", "price", ...
    label_key: str  # i18n key of the label
    lang: str | None = None


def sections(with_ticket: bool) -> list[Section]:
    out = [Section(f"{f}.{lang}", f"form_{f}", lang) for f in TEXT_FIELDS for lang in LANGS]
    out += [Section("starts_at", "form_starts_at"), Section("map", "form_map")]
    if with_ticket:
        out += [Section("price", "form_price"), Section("capacity", "form_capacity")]
    return out


def header(section: Section, ui_lang: str) -> str:
    label = t(ui_lang, section.label_key)
    return f"[{label} {LANG_TAG[section.lang]}]" if section.lang else f"[{label}]"


def _norm(label: str) -> str:
    return " ".join(label.lower().replace("ё", "е").split())


def _label_index(with_ticket: bool) -> dict[str, Section]:
    return {_norm(header(s, ui)[1:-1]): s for s in sections(with_ticket) for ui in LANGS}


def blank_form(ui_lang: str, with_ticket: bool = True) -> str:
    return "\n\n".join(header(s, ui_lang) for s in sections(with_ticket))


def filled_form(event: Event, ui_lang: str, tz: ZoneInfo) -> str:
    """The current event as a form (no price/seats: those live in ticket types)."""
    values = {f"{f}.{lang}": (getattr(event, f) if lang == "ru" else (event.i18n or {}).get(lang, {}).get(f)) for f in TEXT_FIELDS for lang in LANGS}
    values["starts_at"] = event.starts_at.astimezone(tz).strftime("%d.%m.%Y %H:%M")
    values["map"] = event.map_url
    return "\n\n".join(f"{header(s, ui_lang)}\n{values.get(s.key) or '-'}" for s in sections(with_ticket=False))


@dataclass
class EventForm:
    texts: dict[str, dict[str, str | None]]  # field -> lang -> text
    starts_at: datetime
    map_url: str | None
    price_tiyin: int | None = None
    capacity: int | None = None
    missing: list[str] = field(default_factory=list)  # "description.en", ... (falls back to RU)

    def to_state(self) -> dict:
        return {
            "texts": self.texts,
            "starts_at": self.starts_at.isoformat(),
            "map_url": self.map_url,
            "price_tiyin": self.price_tiyin,
            "capacity": self.capacity,
            "missing": self.missing,
        }

    @classmethod
    def from_state(cls, data: dict) -> "EventForm":
        return cls(
            texts=data["texts"],
            starts_at=datetime.fromisoformat(data["starts_at"]),
            map_url=data.get("map_url"),
            price_tiyin=data.get("price_tiyin"),
            capacity=data.get("capacity"),
            missing=data.get("missing", []),
        )

    def apply_to(self, event: Event) -> None:
        for f in TEXT_FIELDS:
            event.set_loc(f, self.texts[f])
        event.starts_at = self.starts_at
        event.map_url = self.map_url


def label_of(key: str, ui_lang: str) -> str:
    section = next(s for s in sections(with_ticket=True) if s.key == key)
    return header(section, ui_lang)[1:-1]


def parse_form(text: str, *, tz: ZoneInfo, ui_lang: str, with_ticket: bool, now: datetime) -> tuple[EventForm | None, list[str]]:
    """Returns the parsed form, or None and a list of human-readable errors."""
    errors: list[str] = []
    matches = list(HEADER_RE.finditer(text))
    if not matches:
        return None, [t(ui_lang, "form_err_no_sections")]

    index = _label_index(with_ticket)
    raw: dict[str, str] = {}
    for i, m in enumerate(matches):
        section = index.get(_norm(m.group(1)))
        if section is None:
            errors.append(t(ui_lang, "form_err_unknown", label=esc(m.group(1))))
            continue
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        value = text[m.end():end].strip()
        raw[section.key] = "" if value in SKIP else value

    required = ["title.ru", "venue.ru", "starts_at"] + (["price", "capacity"] if with_ticket else [])
    for key in required:
        if not raw.get(key):
            errors.append(t(ui_lang, "form_err_missing", label=esc(label_of(key, ui_lang))))

    texts: dict[str, dict[str, str | None]] = {f: {} for f in TEXT_FIELDS}
    missing = []
    for f in TEXT_FIELDS:
        for lang in LANGS:
            value = raw.get(f"{f}.{lang}") or None
            if value and len(value) > LIMITS[f]:
                errors.append(t(ui_lang, "form_err_too_long", label=esc(label_of(f"{f}.{lang}", ui_lang)), max=LIMITS[f]))
            if f == "title" and value and len(value) < 3:
                errors.append(t(ui_lang, "wiz_err_title"))
            texts[f][lang] = value
            if lang != "ru" and not value and texts[f].get("ru"):
                missing.append(f"{f}.{lang}")

    starts_at = None
    if raw.get("starts_at"):
        starts_at = parse_local_dt(raw["starts_at"], tz)
        if starts_at is None:
            errors.append(t(ui_lang, "wiz_err_date"))
        elif starts_at <= now:
            errors.append(t(ui_lang, "wiz_err_past"))

    map_url = raw.get("map") or None
    if map_url and not map_url.startswith(("http://", "https://")):
        errors.append(t(ui_lang, "wiz_err_location"))

    price = capacity = None
    if with_ticket:
        if raw.get("price"):
            price = parse_money_to_tiyin(raw["price"])
            if price is None:
                errors.append(t(ui_lang, "wiz_err_price"))
        if raw.get("capacity"):
            cap = raw["capacity"].replace(" ", "")
            if cap.isdigit() and 1 <= int(cap) <= 100_000:
                capacity = int(cap)
            else:
                errors.append(t(ui_lang, "wiz_err_capacity"))

    if errors:
        return None, list(dict.fromkeys(errors))
    return EventForm(texts, starts_at, map_url[:512] if map_url else None, price, capacity, missing), []


def parse_names(text: str) -> dict[str, str] | None:
    """Ticket type names as "RU / UZ / EN"; a single name is used for all three."""
    parts = [p.strip() for p in text.split("/")]
    if len(parts) == 1 and parts[0]:
        return dict.fromkeys(LANGS, parts[0][:64])
    if len(parts) == 3 and parts[0]:
        return {lang: (p or parts[0])[:64] for lang, p in zip(LANGS, parts)}
    return None


DEFAULT_TICKET_NAMES = {"ru": "Стандарт", "uz": "Standart", "en": "Standard"}

EXAMPLE_VALUES = {
    "title.ru": "AI Meetup Tashkent: продукты на LLM",
    "title.uz": "AI Meetup Tashkent: LLM asosidagi mahsulotlar",
    "title.en": "AI Meetup Tashkent: building products with LLMs",
    "description.ru": "Три доклада о запуске AI-продуктов в Узбекистане и нетворкинг после.\nКофе и снеки — бесплатно.",
    "description.uz": "Oʻzbekistonda AI mahsulotlarini ishga tushirish haqida uchta maʼruza va networking.\nKofe va gazaklar — bepul.",
    "description.en": "Three talks on launching AI products in Uzbekistan, followed by networking.\nFree coffee and snacks.",
    "venue.ru": "IT Park, Ташкент, ул. Мирзо Улугбека 4, 3 этаж",
    "venue.uz": "IT Park, Toshkent, Mirzo Ulugʻbek koʻchasi 4, 3-qavat",
    "venue.en": "IT Park, Tashkent, 4 Mirzo Ulugbek St, 3rd floor",
    "starts_at": "25.10.2026 19:00",
    "map": "https://maps.app.goo.gl/...",
    "price": "150000",
    "capacity": "100",
}


def example_form(ui_lang: str) -> str:
    return "\n\n".join(f"{header(s, ui_lang)}\n{EXAMPLE_VALUES[s.key]}" for s in sections(with_ticket=True))
