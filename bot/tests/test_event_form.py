from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.db.models import Event
from app.services.event_form import blank_form, example_form, filled_form, parse_form, parse_names

TZ = ZoneInfo("Asia/Tashkent")
NOW = datetime(2026, 10, 8, tzinfo=UTC)


def parse(text: str, ui: str = "ru", with_ticket: bool = True):
    return parse_form(text, tz=TZ, ui_lang=ui, with_ticket=with_ticket, now=NOW)


def test_example_form_parses_in_every_interface_language():
    for ui in ("ru", "uz", "en"):
        form, errors = parse(example_form(ui), ui)
        assert errors == [], errors
        assert form.texts["title"]["uz"].startswith("AI Meetup")
        assert form.price_tiyin == 15_000_000 and form.capacity == 100
        assert form.starts_at == datetime(2026, 10, 25, 19, 0, tzinfo=TZ)
        assert form.missing == []


def test_labels_from_another_language_are_accepted():
    # An admin with an Uzbek interface pastes a form copied from a Russian one.
    form, errors = parse(example_form("ru"), ui="uz")
    assert errors == [] and form.capacity == 100


def test_blank_form_reports_required_fields():
    form, errors = parse(blank_form("ru"))
    assert form is None
    joined = " ".join(errors)
    for label in ("Название RU", "Место RU", "Дата и время", "Цена", "Мест"):
        assert label in joined


def test_missing_translations_fall_back_and_are_reported():
    text = example_form("ru").replace(
        "Three talks on launching AI products in Uzbekistan, followed by networking.\nFree coffee and snacks.", "-"
    )
    form, errors = parse(text)
    assert errors == []
    assert form.texts["description"]["en"] is None
    assert form.missing == ["description.en"]


def test_bad_values_and_unknown_headings():
    text = example_form("ru").replace("25.10.2026 19:00", "01.01.2020 10:00").replace("150000", "дорого") + "\n[Скидка]\n10%"
    form, errors = parse(text)
    assert form is None
    joined = " ".join(errors)
    assert "уже прошла" in joined and "Скидка" in joined and "150000" in joined  # price hint shows an example


def test_multiline_description_is_kept():
    form, _ = parse(example_form("ru"))
    assert "\n" in form.texts["description"]["ru"]


def test_edit_form_round_trip():
    event = Event(title="Встреча", venue="Офис", description=None, starts_at=datetime(2026, 11, 1, 14, 0, tzinfo=UTC), map_url=None, i18n={})
    event.set_loc("title", {"ru": "Встреча", "uz": "Uchrashuv", "en": "Meetup"})
    form, errors = parse(filled_form(event, "ru", TZ), with_ticket=False)
    assert errors == []
    assert form.texts["title"] == {"ru": "Встреча", "uz": "Uchrashuv", "en": "Meetup"}
    assert form.starts_at == event.starts_at


def test_localized_lookup_falls_back_to_russian():
    event = Event(title="Встреча", i18n={})
    event.set_loc("title", {"ru": "Встреча", "uz": "Uchrashuv", "en": None})
    assert (event.loc("title", "uz"), event.loc("title", "en"), event.loc("title", None)) == ("Uchrashuv", "Встреча", "Встреча")


def test_ticket_type_names():
    assert parse_names("VIP") == {"ru": "VIP", "uz": "VIP", "en": "VIP"}
    assert parse_names("Студенческий / Talaba / Student") == {"ru": "Студенческий", "uz": "Talaba", "en": "Student"}
    assert parse_names("Детский / / Kids") == {"ru": "Детский", "uz": "Детский", "en": "Kids"}
    assert parse_names("a / b") is None
