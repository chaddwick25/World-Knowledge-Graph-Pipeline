"""Unit tests for the romanizing_names framework.

Tests each romanizer's detect() and romanize() methods, and the
RomanizerRegistry's auto_romanize() dispatch.
"""

import pytest

from semantic_search.services.romanizing_names.identity_romanizer import IdentityRomanizer
from semantic_search.services.romanizing_names.hangul_romanizer import HangulRomanizer
from semantic_search.services.romanizing_names.diacritic_romanizer import DiacriticRomanizer
from semantic_search.services.romanizing_names.registry import RomanizerRegistry


# ── IdentityRomanizer ────────────────────────────────────────────────

class TestIdentityRomanizer:
    def test_basic_lowercase(self):
        assert IdentityRomanizer.romanize("Paris Baguette") == "paris baguette"

    def test_strips_punctuation(self):
        assert IdentityRomanizer.romanize("McDonald's, Inc.") == "mcdonald's inc"

    def test_empty_string(self):
        assert IdentityRomanizer.romanize("") == ""

    def test_none(self):
        assert IdentityRomanizer.romanize(None) == ""

    def test_detect_always_true(self):
        assert IdentityRomanizer.detect("anything") is True
        assert IdentityRomanizer.detect("") is True

    def test_preserves_hyphens(self):
        assert IdentityRomanizer.romanize("Café-Restaurant") == "café-restaurant"

    def test_preserves_apostrophes(self):
        assert IdentityRomanizer.romanize("O'Brien's") == "o'brien's"


# ── HangulRomanizer ──────────────────────────────────────────────────

class TestHangulRomanizer:
    def test_paris_baguette(self):
        """The canonical test case — Korean Paris Baguette."""
        result = HangulRomanizer.romanize("파리바게뜨")
        assert result == "paribagetteu"

    def test_detect_korean(self):
        assert HangulRomanizer.detect("파리바게뜨") is True

    def test_detect_latin(self):
        assert HangulRomanizer.detect("Paris Baguette") is False

    def test_detect_mixed(self):
        """Mixed Korean+Latin text should be detected as Hangul."""
        assert HangulRomanizer.detect("파리바게뜨 (Paris Baguette)") is True

    def test_detect_empty(self):
        assert HangulRomanizer.detect("") is False
        assert HangulRomanizer.detect(None) is False

    def test_empty_string(self):
        assert HangulRomanizer.romanize("") == ""

    def test_mixed_korean_latin(self):
        """Mixed text should romanize the Korean part and keep Latin."""
        result = HangulRomanizer.romanize("파리바게뜨 (Paris Baguette)")
        assert "paribagetteu" in result
        assert "paris baguette" in result

    def test_seoul(self):
        result = HangulRomanizer.romanize("서울")
        assert result == "seoul"

    def test_normalizes_output(self):
        """Output should be lowercase and stripped of extra whitespace."""
        result = HangulRomanizer.romanize("서울역")
        assert result == result.lower()
        assert "  " not in result


# ── DiacriticRomanizer ───────────────────────────────────────────────

class TestDiacriticRomanizer:
    def test_french_cafe(self):
        assert DiacriticRomanizer.romanize("café") == "cafe"

    def test_french_éireann(self):
        assert DiacriticRomanizer.romanize("Éireann") == "eireann"

    def test_spanish_piñata(self):
        assert DiacriticRomanizer.romanize("piñata") == "pinata"

    def test_spanish_señor(self):
        assert DiacriticRomanizer.romanize("señor") == "senor"

    def test_irish_fada(self):
        """Irish fada (áéíóú) should be stripped."""
        assert DiacriticRomanizer.romanize("Banc na hÉireann") == "banc na heireann"

    def test_irish_gaeltacht(self):
        assert DiacriticRomanizer.romanize("Gaeltacht") == "gaeltacht"

    def test_german_umlaut(self):
        assert DiacriticRomanizer.romanize("München") == "munchen"

    def test_german_eszett(self):
        assert DiacriticRomanizer.romanize("Straße") == "strasse"

    def test_vietnamese_tones(self):
        assert DiacriticRomanizer.romanize("Hà Nội") == "ha noi"

    def test_danish_ae(self):
        assert DiacriticRomanizer.romanize("Ærhus") == "aerhus"

    def test_danish_oe(self):
        assert DiacriticRomanizer.romanize("Østerbro") == "osterbro"

    def test_icelandic_thorn(self):
        assert DiacriticRomanizer.romanize("Þingvellir") == "thingvellir"

    def test_detect_french(self):
        assert DiacriticRomanizer.detect("café") is True

    def test_detect_spanish(self):
        assert DiacriticRomanizer.detect("piñata") is True

    def test_detect_plain_latin(self):
        assert DiacriticRomanizer.detect("Paris") is False

    def test_detect_korean(self):
        """Korean text should NOT trigger the diacritic romanizer."""
        assert DiacriticRomanizer.detect("파리바게뜨") is False

    def test_detect_empty(self):
        assert DiacriticRomanizer.detect("") is False
        assert DiacriticRomanizer.detect(None) is False

    def test_empty_string(self):
        assert DiacriticRomanizer.romanize("") == ""


# ── RomanizerRegistry ────────────────────────────────────────────────

class TestRomanizerRegistry:
    def test_auto_romanize_korean(self):
        """Korean text should auto-route to HangulRomanizer."""
        result = RomanizerRegistry.auto_romanize("파리바게뜨")
        assert result == "paribagetteu"

    def test_auto_romanize_french(self):
        """French text should auto-route to DiacriticRomanizer."""
        result = RomanizerRegistry.auto_romanize("café")
        assert result == "cafe"

    def test_auto_romanize_english(self):
        """English text should auto-route to IdentityRomanizer."""
        result = RomanizerRegistry.auto_romanize("Paris Baguette")
        assert result == "paris baguette"

    def test_auto_romanize_spanish(self):
        """Spanish text should auto-route to DiacriticRomanizer."""
        result = RomanizerRegistry.auto_romanize("Señor Plaza")
        assert result == "senor plaza"

    def test_romanize_query_korean(self):
        result = RomanizerRegistry.romanize_query("파리바게뜨")
        assert result == "paribagetteu"

    def test_romanize_query_english(self):
        result = RomanizerRegistry.romanize_query("paris bagueete")
        assert result == "paris bagueete"

    def test_auto_romanize_empty(self):
        assert RomanizerRegistry.auto_romanize("") == ""
        assert RomanizerRegistry.auto_romanize(None) == ""

    def test_list_supported(self):
        """Registry should list all registered language codes."""
        supported = RomanizerRegistry.list_supported()
        assert "ko" in supported
        assert "diacritic" in supported
        assert "identity" in supported

    def test_get_by_code(self):
        """get() should return the correct romanizer class."""
        from semantic_search.services.romanizing_names.hangul_romanizer import HangulRomanizer
        assert RomanizerRegistry.get("ko") is HangulRomanizer

    def test_get_unknown_falls_back(self):
        """Unknown language code should fall back to IdentityRomanizer."""
        from semantic_search.services.romanizing_names.identity_romanizer import IdentityRomanizer
        assert RomanizerRegistry.get("xx") is IdentityRomanizer

    def test_mixed_korean_latin(self):
        """Mixed Korean+Latin should route to Hangul (registered first)."""
        result = RomanizerRegistry.auto_romanize("파리바게뜨 (Paris Baguette)")
        assert "paribagetteu" in result
        assert "paris baguette" in result
