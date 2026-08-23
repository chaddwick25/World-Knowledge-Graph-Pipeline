"""Identity romanizer for Latin-script text.

Handles English, German (without umlauts), Dutch, Indonesian, etc.
Any text that doesn't match a more specific romanizer falls through here.
"""

from .base_romanizer import AbstractRomanizer


class IdentityRomanizer(AbstractRomanizer):
    """Pass-through romanizer for already-Latin text.

    Lowercases and strips punctuation. No character mapping needed.
    """

    language_code = "identity"

    @classmethod
    def detect(cls, text: str) -> bool:
        """Always returns True — identity is the fallback romanizer.

        It matches any text that doesn't contain non-Latin characters
        handled by a more specific romanizer. The registry tries other
        romanizers first and falls back to this one.
        """
        return True

    @classmethod
    def romanize(cls, text: str) -> str:
        """Lowercase and strip punctuation. No character mapping."""
        return cls._normalize(text)
