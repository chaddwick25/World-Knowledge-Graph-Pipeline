"""Romanizer registry — pluggable per-script romanization.

Adding a new language:
  1. Write a romanizer module (one file, ~30-100 lines)
  2. Register it: RomanizerRegistry.register("ko", HangulRomanizer)
  3. Re-run romanize_names command — auto-detect handles the rest

No query-path or pipeline code changes needed.
"""

from .identity_romanizer import IdentityRomanizer
from .hangul_romanizer import HangulRomanizer
from .diacritic_romanizer import DiacriticRomanizer


class RomanizerRegistry:
    """Registry of romanizers by language code.

    The registry is ordered — romanizers are tried in registration order
    during auto_romanize(). More specific romanizers should be registered
    before less specific ones (e.g., Hangul before Diacritic before Identity).
    """

    # Ordered list of (language_code, romanizer_class) pairs
    # Order matters: first detect() match wins during auto_romanize()
    _romanizers: list = []

    @classmethod
    def register(cls, language_code: str, romanizer_cls):
        """Register a romanizer. More specific romanizers should be registered first."""
        # Remove any existing registration for this language code
        cls._romanizers = [
            (code, r) for code, r in cls._romanizers if code != language_code
        ]
        cls._romanizers.append((language_code, romanizer_cls))

    @classmethod
    def get(cls, language_code: str):
        """Get a romanizer by language code. Falls back to IdentityRomanizer."""
        for code, romanizer in cls._romanizers:
            if code == language_code:
                return romanizer
        return IdentityRomanizer

    @classmethod
    def auto_romanize(cls, text: str) -> str:
        """Auto-detect script and romanize. No country config needed.

        Tries each registered romanizer's detect() in registration order.
        First match wins. Falls back to IdentityRomanizer for Latin text.
        """
        if not text:
            return ""
        for lang_code, romanizer in cls._romanizers:
            if romanizer.detect(text):
                return romanizer.romanize(text)
        return IdentityRomanizer.romanize(text)

    @classmethod
    def romanize_query(cls, text: str) -> str:
        """Romanize a query string. Auto-detects script from the text."""
        return cls.auto_romanize(text)

    @classmethod
    def list_supported(cls) -> list:
        """Return list of supported language codes."""
        return [code for code, _ in cls._romanizers]


# ── Register romanizers in specificity order ──────────────────────────
# Most specific scripts first, identity last (it matches everything).
# Hangul is the most specific (only Korean text triggers it).
# Diacritic is medium (only Latin text with combining marks triggers it).
# Identity is the fallback (matches any remaining Latin text).
RomanizerRegistry.register("ko", HangulRomanizer)
RomanizerRegistry.register("diacritic", DiacriticRomanizer)
RomanizerRegistry.register("identity", IdentityRomanizer)
