import html
import re
from datetime import datetime
from zoneinfo import ZoneInfo

MONTHS = {
    "ru": ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"],
    "uz": ["yanvar", "fevral", "mart", "aprel", "may", "iyun", "iyul", "avgust", "sentabr", "oktabr", "noyabr", "dekabr"],
    "en": ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"],
}
WEEKDAYS = {
    "ru": ["пн", "вт", "ср", "чт", "пт", "сб", "вс"],
    "uz": ["dush", "sesh", "chor", "pay", "jum", "shan", "yak"],
    "en": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
}
CURRENCY = {"ru": "сум", "uz": "soʻm", "en": "UZS"}


def esc(value: object) -> str:
    # quote=True: the result is also used inside href="..." attributes.
    return html.escape(str(value), quote=True) if value is not None else ""


def fmt_money(tiyin: int, lang: str) -> str:
    sums = tiyin // 100
    rest = tiyin % 100
    whole = f"{sums:,}".replace(",", " ")
    return f"{whole}{f'.{rest:02d}' if rest else ''} {CURRENCY.get(lang, 'UZS')}"


def fmt_dt(dt: datetime, tz: ZoneInfo, lang: str, with_weekday: bool = True) -> str:
    local = dt.astimezone(tz)
    lang = lang if lang in MONTHS else "ru"
    month = MONTHS[lang][local.month - 1]
    wd = f", {WEEKDAYS[lang][local.weekday()]}" if with_weekday else ""
    if lang == "en":
        date = f"{month} {local.day}, {local.year}"
    elif lang == "uz":
        date = f"{local.day}-{month}, {local.year}"
    else:
        date = f"{local.day} {month} {local.year}"
    return f"{date}{wd} · {local:%H:%M}"


def fmt_card(number: str) -> str:
    digits = "".join(ch for ch in number if ch.isdigit())
    return " ".join(digits[i : i + 4] for i in range(0, len(digits), 4)) if digits else number


def parse_local_dt(text: str, tz: ZoneInfo) -> datetime | None:
    """Accepts '25.10.2026 19:00' (also with '-' or '/' separators) in the bot's timezone."""
    cleaned = text.strip().replace("/", ".").replace("-", ".")
    for pattern in ("%d.%m.%Y %H:%M", "%d.%m.%y %H:%M"):
        try:
            return datetime.strptime(cleaned, pattern).replace(tzinfo=tz)
        except ValueError:
            continue
    return None


def parse_money_to_tiyin(text: str) -> int | None:
    """Whole sums only. '150 000', '150.000', '150,000' and '150000' all mean 150 000 sum:
    in RU/UZ the dot and comma are thousands separators far more often than decimals."""
    cleaned = re.sub(r"[\s.,' ]", "", text.strip().lower().removesuffix("сум").removesuffix("soʻm").removesuffix("uzs"))
    if not re.fullmatch(r"\d{1,10}", cleaned):
        return None
    return int(cleaned) * 100
