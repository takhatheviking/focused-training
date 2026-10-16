import pytest

from app.formatting import esc, fmt_card, fmt_money, parse_money_to_tiyin


@pytest.mark.parametrize(
    "text, tiyin",
    [("150000", 15_000_000), ("150 000", 15_000_000), ("150.000", 15_000_000), ("150,000", 15_000_000), ("0", 0), ("150000 сум", 15_000_000)],
)
def test_money_parser_reads_thousands_separators(text, tiyin):
    assert parse_money_to_tiyin(text) == tiyin


@pytest.mark.parametrize("text", ["nan", "-5", "1e9", "abc", "", "12345678901"])
def test_money_parser_rejects_garbage(text):
    assert parse_money_to_tiyin(text) is None


def test_display_helpers():
    assert fmt_money(14_999_800, "ru") == "149 998 сум"
    assert fmt_card("8600123412341234") == "8600 1234 1234 1234"
    assert esc('a"<b>') == "a&quot;&lt;b&gt;"
