"""Hangul (Korean) romanizer.

Extracted from worldkg_nca/views/search.py and adapted to the
AbstractRomanizer interface.

Uses Unicode NFKD normalization to decompose Hangul syllables into
Jamo (U+1100–U+1112 choseong/consonants, U+1161–U+1175 jungseong/vowels),
then maps each Jamo to a Latin approximation per the Revised Romanization
of Korean (2000, South Korean Ministry of Culture).

This is NOT a proper transliteration (use a library like `hangul-romanize`
for that), but it produces a stable Latin-character fingerprint sufficient
for character-level similarity comparison between a Korean name and a
Latin query via pg_trgm.

King Sejong the Great invented Hangul in 1443 as a phonetic script where
each character maps to a sound — which is exactly what makes this
romanization + n-gram matching work.
"""

import re
import unicodedata

from .base_romanizer import AbstractRomanizer

# Hangul Unicode block ranges
# U+AC00–U+D7AF: Hangul Syllables (가-힣)
# U+1100–U+11FF: Hangul Jamo (modern decomposition targets)
# U+3130–U+318F: Hangul Compatibility Jamo
_HANGUL_RE = re.compile(r'[\uac00-\ud7af\u1100-\u11ff\u3130-\u318f]')


class HangulRomanizer(AbstractRomanizer):
    """Romanizer for Hangul (Korean) text.

    detect(): True if text contains any Hangul characters.
    romanize(): NFKD decompose → Jamo → Latin mapping → normalize.
    """

    language_code = "ko"

    # Choseong (initial consonants): U+1100–U+1112
    _CHOSEONG = {
        'ᄀ': 'g', 'ᄁ': 'kk', 'ᄂ': 'n', 'ᄃ': 'd', 'ᄄ': 'tt',
        'ᄅ': 'r', 'ᄆ': 'm', 'ᄇ': 'b', 'ᄈ': 'pp', 'ᄉ': 's',
        'ᄊ': 'ss', 'ᄋ': '', 'ᄌ': 'j', 'ᄍ': 'jj', 'ᄎ': 'ch',
        'ᄏ': 'k', 'ᄐ': 't', 'ᄑ': 'p', 'ᄒ': 'h',
    }
    # Jungseong (vowels): U+1161–U+1175
    _JUNGSEONG = {
        'ᅡ': 'a', 'ᅢ': 'ae', 'ᅣ': 'ya', 'ᅤ': 'yae', 'ᅥ': 'eo',
        'ᅦ': 'e', 'ᅧ': 'yeo', 'ᅨ': 'ye', 'ᅩ': 'o', 'ᅪ': 'wa',
        'ᅫ': 'wae', 'ᅬ': 'oe', 'ᅭ': 'yo', 'ᅮ': 'u', 'ᅯ': 'wo',
        'ᅰ': 'we', 'ᅱ': 'wi', 'ᅲ': 'yu', 'ᅳ': 'eu', 'ᅴ': 'ui',
        'ᅵ': 'i',
    }
    # Jongseong (final consonants): U+11A8–U+11C2
    # These are the same consonants as choseong but at different Unicode
    # codepoints. NFKD decomposition produces these for syllables with
    # final consonants (e.g., 울 → ᄋ + ᅮ + ᆯ).
    _JONGSEONG = {
        'ᆨ': 'k', 'ᆩ': 'kk', 'ᆪ': 'ks', 'ᆫ': 'n', 'ᆬ': 'nj',
        'ᆭ': 'n', 'ᆮ': 't', 'ᆯ': 'l', 'ᆰ': 'lg', 'ᆱ': 'lm',
        'ᆲ': 'lb', 'ᆳ': 'ls', 'ᆴ': 'lt', 'ᆵ': 'lp', 'ᆶ': 'lh',
        'ᆷ': 'm', 'ᆸ': 'p', 'ᆹ': 'ps', 'ᆺ': 't', 'ᆻ': 'ss',
        'ᆼ': 'ng', 'ᆽ': 't', 'ᆾ': 'ch', 'ᆿ': 'k', 'ᇀ': 't',
        'ᇁ': 'p', 'ᇂ': 'h',
    }
    # Compatibility jamo (U+3130–U+318F) for robustness
    _COMPAT = {
        'ㄱ': 'g', 'ㄲ': 'kk', 'ㄴ': 'n', 'ㄷ': 'd', 'ㄸ': 'tt',
        'ㄹ': 'r', 'ㅁ': 'm', 'ㅂ': 'b', 'ㅃ': 'pp', 'ㅅ': 's',
        'ㅆ': 'ss', 'ㅇ': '', 'ㅈ': 'j', 'ㅉ': 'jj', 'ㅊ': 'ch',
        'ㅋ': 'k', 'ㅌ': 't', 'ㅍ': 'p', 'ㅎ': 'h',
        'ㅏ': 'a', 'ㅐ': 'ae', 'ㅑ': 'ya', 'ㅒ': 'yae', 'ㅓ': 'eo',
        'ㅔ': 'e', 'ㅕ': 'yeo', 'ㅖ': 'ye', 'ㅗ': 'o', 'ㅛ': 'yo',
        'ㅜ': 'u', 'ㅠ': 'yu', 'ㅡ': 'eu', 'ㅣ': 'i',
    }
    _ALL_MAP = {**_CHOSEONG, **_JUNGSEONG, **_JONGSEONG, **_COMPAT}

    @classmethod
    def detect(cls, text: str) -> bool:
        """Return True if *text* contains any Hangul (Korean) characters."""
        return bool(_HANGUL_RE.search(text or ''))

    @classmethod
    def romanize(cls, text: str) -> str:
        """Romanize Hangul text to a lowercase Latin string.

        NFKD decomposes Hangul syllables into Jamo, then each Jamo is
        mapped to its Latin equivalent. Non-Hangul characters (Latin,
        digits, spaces) are preserved and lowercased.
        """
        if not text:
            return ""
        decomposed = unicodedata.normalize('NFKD', text)
        result = []
        for ch in decomposed:
            if ch in cls._ALL_MAP:
                result.append(cls._ALL_MAP[ch])
            elif ch.isascii():
                result.append(ch.lower())
            # Skip non-ASCII non-Hangul chars (e.g., CJK ideographs)
        raw = ''.join(result)
        return cls._normalize(raw)
