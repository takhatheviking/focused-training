"""Tiny i18n: one TOML file per language, `t(lang, key, **kwargs)` with str.format placeholders."""

import tomllib
from functools import lru_cache
from pathlib import Path

LANGS = ("ru", "uz", "en")
LANG_NAMES = {"ru": "🇷🇺 Русский", "uz": "🇺🇿 Oʻzbekcha", "en": "🇬🇧 English"}
_DIR = Path(__file__).parent / "locales"


@lru_cache
def _catalog(lang: str) -> dict[str, str]:
    path = _DIR / f"{lang}.toml"
    if not path.exists():
        return {}  # missing translation falls back to Russian key by key
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def t(lang: str | None, key: str, **kwargs) -> str:
    lang = lang if lang in LANGS else "ru"
    text = _catalog(lang).get(key) or _catalog("ru").get(key) or key
    return text.format(**kwargs) if kwargs else text


def all_variants(key: str) -> set[str]:
    """Every translation of a key — used to match reply-keyboard button presses."""
    return {t(lang, key) for lang in LANGS}


class Translator:
    def __init__(self, lang: str | None):
        self.lang = lang if lang in LANGS else "ru"

    def __call__(self, key: str, **kwargs) -> str:
        return t(self.lang, key, **kwargs)
