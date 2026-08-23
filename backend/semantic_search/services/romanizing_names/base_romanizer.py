"""Base class for script-specific romanizers.

All romanizers are pure functions: text in, Latin string out.
No state, no I/O, no dependencies — deterministic and testable.
"""

import re
import unicodedata


class AbstractRomanizer:
    """Base class for script-specific romanization.

    Subclasses must implement:
      - detect(text) -> bool: True if text contains this script's characters
      - romanize(text) -> str: Latin-character representation of text

    The romanize() output should be:
      - Lowercase
      - ASCII-only (no combining marks, no non-Latin characters)
      - Whitespace-preserved (spaces between words retained)
      - Punctuation stripped (except hyphens and apostrophes in names)
    """

    language_code: str = ""

    # Characters to strip during normalization (punctuation, symbols)
    # Hyphens and apostrophes are kept — they appear in names like "O'Brien"
    _STRIP_RE = re.compile(r"[^\w\s'\-]", re.UNICODE)

    @classmethod
    def detect(cls, text: str) -> bool:
        """Return True if *text* contains characters in this romanizer's script."""
        raise NotImplementedError

    @classmethod
    def romanize(cls, text: str) -> str:
        """Romanize *text* to a lowercase Latin string."""
        raise NotImplementedError

    @classmethod
    def _normalize(cls, text: str) -> str:
        """Common post-processing: lowercase, strip non-word chars, collapse spaces."""
        if not text:
            return ""
        # Lowercase
        result = text.lower()
        # Strip punctuation (keep word chars, spaces, hyphens, apostrophes)
        result = cls._STRIP_RE.sub(" ", result)
        # Collapse multiple spaces
        result = re.sub(r"\s+", " ", result).strip()
        return result
