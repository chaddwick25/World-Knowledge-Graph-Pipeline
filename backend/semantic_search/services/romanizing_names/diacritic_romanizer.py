"""Diacritic romanizer for Latin-script text with accent marks.

Handles French, Spanish, Irish (fada), Vietnamese (tones), German
(umlauts), Portuguese, Italian, Czech, Slovak, Hungarian, Polish,
Romanian, Nordic languages, and any other Latin-script language with
combining diacritical marks.

Uses Unicode NFKD normalization to decompose accented characters into
base character + combining mark, then strips the combining marks.
This is a deterministic, standard Unicode technique — no lookup table
needed.

Examples:
  "Banc na hÉireann" → "banc na heireann"     (Irish)
  "café"             → "cafe"                  (French/Spanish)
  "piñata"           → "pinata"                (Spanish ñ)
  "München"          → "munchen"               (German ü)
  "Hà Nội"           → "ha noi"                (Vietnamese tones)
  "Århus"            → "arhus"                 (Danish)
"""

import re
import unicodedata

from .base_romanizer import AbstractRomanizer


class DiacriticRomanizer(AbstractRomanizer):
    """Romanizer for Latin-script text with diacritical marks.

    detect(): True if text contains Latin characters with combining marks
              or precomposed accented characters (but NOT Hangul or other
              non-Latin scripts).
    romanize(): NFKD decompose → strip combining marks → normalize.
    """

    language_code = "diacritic"

    ***REMOVED*** Unicode category for combining marks (Mn = Mark, Nonspacing)
    ***REMOVED*** These are the diacritical marks that NFKD decomposition separates
    ***REMOVED*** from the base character (e.g., é → e + ◌́ )
    ***REMOVED***
    ***REMOVED*** Special cases handled before NFKD:
    ***REMOVED***   ñ → n (Spanish) — NFKD decomposes to n + combining tilde, stripped
    ***REMOVED***   ß → ss (German) — NFKD does NOT decompose this, handled explicitly
    ***REMOVED***   æ → ae (Danish/Norwegian) — NFKD does NOT decompose, handled explicitly
    ***REMOVED***   ø → o (Danish/Norwegian) — NFKD does NOT decompose, handled explicitly
    ***REMOVED***   ð → d (Icelandic) — NFKD does NOT decompose, handled explicitly
    ***REMOVED***   þ → th (Icelandic) — NFKD does NOT decompose, handled explicitly
    _SPECIAL_DECOMPOSE = {
        'ß': 'ss',
        'æ': 'ae',
        'Æ': 'ae',
        'ø': 'o',
        'Ø': 'o',
        'ð': 'd',
        'Ð': 'd',
        'þ': 'th',
        'Þ': 'th',
        'œ': 'oe',
        'Œ': 'oe',
    }

    ***REMOVED*** Precomposed Latin characters with diacritics that NFKD handles:
    ***REMOVED*** U+00C0–U+024F (Latin Extended) — most accented Latin chars
    ***REMOVED*** We detect these to know if the diacritic romanizer should fire
    _DIACRITIC_RE = re.compile(r'[\u00c0-\u024f]')

    @classmethod
    def detect(cls, text: str) -> bool:
        """Return True if text has Latin diacritics but no non-Latin scripts.

        We check for precomposed accented Latin characters (U+00C0–U+024F)
        or special Latin characters (ß, æ, ø, etc.). If the text contains
        Hangul or other non-Latin scripts, those romanizers should fire
        instead (they're registered before us).
        """
        if not text:
            return False
        ***REMOVED*** Check for precomposed accented Latin chars
        if cls._DIACRITIC_RE.search(text):
            return True
        ***REMOVED*** Check for special chars that need explicit decomposition
        for ch in text:
            if ch in cls._SPECIAL_DECOMPOSE:
                return True
        return False

    @classmethod
    def romanize(cls, text: str) -> str:
        """Romanize accented Latin text by stripping diacritical marks.

        1. Replace special characters (ß→ss, æ→ae, ø→o, etc.)
        2. NFKD decompose (é → e + combining acute accent)
        3. Strip combining marks (category Mn)
        4. Normalize (lowercase, strip punctuation, collapse spaces)
        """
        if not text:
            return ""
        ***REMOVED*** Step 1: Replace special characters that NFKD doesn't handle
        result = []
        for ch in text:
            if ch in cls._SPECIAL_DECOMPOSE:
                result.append(cls._SPECIAL_DECOMPOSE[ch])
            else:
                result.append(ch)
        text = ''.join(result)

        ***REMOVED*** Step 2: NFKD decompose
        decomposed = unicodedata.normalize('NFKD', text)

        ***REMOVED*** Step 3: Strip combining marks (category Mn = Nonspacing Mark)
        stripped = []
        for ch in decomposed:
            if unicodedata.category(ch) != 'Mn':
                stripped.append(ch)
        text = ''.join(stripped)

        ***REMOVED*** Step 4: Normalize
        return cls._normalize(text)
