"""Texts of records the plugin writes itself.

Incidents opened from monitoring outages and tasks opened from maintenance
events need titles, descriptions and journal entries that read naturally for
the operators. They come from the plugin's own locale files —
``locales/itsm.<locale>.json``, section ``auto`` — in the locale the operator
configured (setting ``auto_text_locale``), with English as the fallback, so
the backend and the React UI share one place for wording.

Placeholders use the i18next dialect of those files (``{{name}}``). The core
``t()`` is not used: it speaks ``%{name}`` and knows nothing about the
caller's locale on a bus event.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

LOCALES_DIR = Path(__file__).resolve().parents[2] / "locales"
NAMESPACE = "itsm"
SECTION = "auto"
DEFAULT_LOCALE = "de"
FALLBACK_LOCALE = "en"

_PLACEHOLDER_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")


class Texts:
    """``texts("incident_title", name="grafana", since="…")`` → localized string.

    Missing keys fall back to English, then to the key itself — a missing
    translation must never stop an incident from being opened.
    """

    def __init__(
        self, locale: str = DEFAULT_LOCALE, locales_dir: Path = LOCALES_DIR
    ) -> None:
        self.locale = locale
        self._dir = Path(locales_dir)
        self._sections: dict[str, dict[str, str]] = {}

    def available(self, locale: str) -> bool:
        return (self._dir / f"{NAMESPACE}.{locale}.json").is_file()

    def set_locale(self, locale: str) -> bool:
        """Switch the locale; returns False (and keeps the old one) when no
        locale file exists for it."""
        if not self.available(locale):
            return False
        self.locale = locale
        return True

    def _section(self, locale: str) -> dict[str, str]:
        cached = self._sections.get(locale)
        if cached is None:
            cached = {}
            path = self._dir / f"{NAMESPACE}.{locale}.json"
            try:
                with path.open("r", encoding="utf-8") as fh:
                    raw = json.load(fh).get(SECTION) or {}
                cached = {k: str(v) for k, v in raw.items()}
            except (OSError, ValueError, AttributeError):
                cached = {}
            self._sections[locale] = cached
        return cached

    def __call__(self, key: str, **values: object) -> str:
        for locale in (self.locale, FALLBACK_LOCALE):
            template = self._section(locale).get(key)
            if template:
                return _PLACEHOLDER_RE.sub(
                    lambda m: str(values.get(m.group(1), m.group(0))), template
                )
        return key
