"""Tests for non-sovereign territory utilities."""
import pytest
from extraction.services.non_sovereign_territories import (
    NON_SOVEREIGN_TERRITORIES,
    is_non_sovereign_synthetic_iso,
    resolve_non_sovereign_iso,
    synthetic_iso_to_info,
)


class TestNonSovereignTerritories:
    """Test non-sovereign territory resolution."""

    def test_resolve_wales(self):
        assert resolve_non_sovereign_iso("Wales") == "WL"
        assert resolve_non_sovereign_iso("wales") == "WL"

    def test_resolve_scotland(self):
        assert resolve_non_sovereign_iso("Scotland") == "XS"
        assert resolve_non_sovereign_iso("scotland") == "XS"

    def test_resolve_england(self):
        assert resolve_non_sovereign_iso("England") == "EN"
        assert resolve_non_sovereign_iso("england") == "EN"

    def test_resolve_unknown(self):
        assert resolve_non_sovereign_iso("Atlantis") is None
        assert resolve_non_sovereign_iso("Ireland") is None
        assert resolve_non_sovereign_iso("") is None

    def test_is_synthetic_iso(self):
        assert is_non_sovereign_synthetic_iso("WL") is True
        assert is_non_sovereign_synthetic_iso("XS") is True
        assert is_non_sovereign_synthetic_iso("EN") is True
        assert is_non_sovereign_synthetic_iso("GB") is False
        assert is_non_sovereign_synthetic_iso("US") is False
        assert is_non_sovereign_synthetic_iso("AA") is False
        assert is_non_sovereign_synthetic_iso("") is False

    def test_synthetic_iso_to_info(self):
        info = synthetic_iso_to_info("WL")
        assert info is not None
        assert info["name"] == "Wales"
        assert info["slug"] == "wales"
        assert info["continent"] == "europe"

        info = synthetic_iso_to_info("XS")
        assert info is not None
        assert info["name"] == "Scotland"
        assert info["slug"] == "scotland"
        assert info["continent"] == "europe"

        info = synthetic_iso_to_info("EN")
        assert info is not None
        assert info["name"] == "England"
        assert info["slug"] == "england"
        assert info["continent"] == "europe"

        assert synthetic_iso_to_info("GB") is None
        assert synthetic_iso_to_info("ZZ") is None

    def test_non_sovereign_territories_structure(self):
        """Verify the data structure of NON_SOVEREIGN_TERRITORIES."""
        for iso, info in NON_SOVEREIGN_TERRITORIES.items():
            assert len(iso) == 2, f"ISO code {iso!r} must be 2 chars"
            assert "name" in info, f"{iso} missing 'name'"
            assert "slug" in info, f"{iso} missing 'slug'"
            assert "continent" in info, f"{iso} missing 'continent'"
            # Slug should be a lowercase, URL-friendly version of the name
            # (may differ if the source data uses a specific slug convention)
            assert info["slug"] == info["slug"].lower(), (
                f"Slug for {iso} must be lowercase, got {info['slug']!r}"
            )
            assert " " not in info["slug"], (
                f"Slug for {iso} must not contain spaces, got {info['slug']!r}"
            )
            assert info["slug"] != "", f"Slug for {iso} must not be empty"

    def test_known_synthetic_codes(self):
        """Ensure specific known synthetic codes are present."""
        assert "WL" in NON_SOVEREIGN_TERRITORIES
        assert "XS" in NON_SOVEREIGN_TERRITORIES
        assert "EN" in NON_SOVEREIGN_TERRITORIES

    def test_no_clash_with_real_codes(self):
        """Synthetic codes should not overlap with real ISO 3166-1 alpha-2 codes."""
        real_codes = {"GB", "US", "FR", "DE", "IE", "IT", "ES", "PT", "NL", "BE"}
        for code in NON_SOVEREIGN_TERRITORIES:
            assert code not in real_codes, (
                f"Synthetic code {code!r} clashes with real ISO code"
            )

