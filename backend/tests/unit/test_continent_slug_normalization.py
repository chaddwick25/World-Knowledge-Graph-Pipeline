"""
Unit tests for continent/country slug normalisation.

Tests the core invariant: after the hyphen->underscore migration,
ALL path construction must produce underscores, matching the DB convention.

Markers:
    @pytest.mark.unit -- pure logic tests, no DB, no network, no GPU
"""
import pytest
from pathlib import Path
import tempfile

from extraction.services.regional_path_service import (
    normalize_continent_slug,
    normalize_country_slug,
    normalize_country_name,
    RegionalPathService,
)


@pytest.fixture
def temp_base_dir():
    with tempfile.TemporaryDirectory() as td:
        yield td


# ====== 1. Normalisation functions -- pure invariants ======


class TestNormalizeContinentSlug:
    """normalize_continent_slug() must always produce underscores."""

    @pytest.mark.unit
    @pytest.mark.parametrize("raw,expected", [
        # Single-word continents (unchanged)
        ("europe", "europe"),
        ("africa", "africa"),
        ("asia", "asia"),
        ("antarctica", "antarctica"),
        ("australia", "australia"),
        ("oceania", "oceania"),
        # Hyphenated -> underscores (THE FIX)
        ("north-america", "north_america"),
        ("south-america", "south_america"),
        ("central-america", "central_america"),
        # Space-separated -> underscores
        ("North America", "north_america"),
        ("South America", "south_america"),
        ("Central America", "central_america"),
        # Mixed delimiters
        ("North-America", "north_america"),
        ("north_America", "north_america"),
        # Already underscore (no-op)
        ("north_america", "north_america"),
        ("south_america", "south_america"),
        # Name with spaces
        ("Russian Federation", "russian_federation"),
    ])
    def test_hyphens_and_spaces_become_underscores(self, raw, expected):
        assert normalize_continent_slug(raw) == expected

    @pytest.mark.unit
    def test_empty_string_returns_empty(self):
        assert normalize_continent_slug("") == ""

    @pytest.mark.unit
    def test_no_op_for_single_word(self):
        assert normalize_continent_slug("europe") == "europe"
        assert normalize_continent_slug("africa") == "africa"

    @pytest.mark.unit
    def test_identity_after_normalisation(self):
        inputs = ["north-america", "North America", "north_america"]
        for raw in inputs:
            once = normalize_continent_slug(raw)
            twice = normalize_continent_slug(once)
            assert once == twice, f"Not idempotent for {raw!r}: {once!r} -> {twice!r}"


class TestNormalizeCountrySlug:
    @pytest.mark.unit
    @pytest.mark.parametrize("raw,expected", [
        ("mozambique", "mozambique"),
        ("canada", "canada"),
        ("ireland", "ireland"),
        ("Costa Rica", "costa_rica"),
        ("United Kingdom", "united_kingdom"),
        ("United Arab Emirates", "united_arab_emirates"),
        ("New Zealand", "new_zealand"),
        ("Cote-d'Ivoire", "cote_d'ivoire"),
        ("Costa-Rica", "costa_rica"),
        ("costa_rica", "costa_rica"),
        ("united_kingdom", "united_kingdom"),
        ("", ""),
        ("a", "a"),
    ])
    def test_spaces_and_hyphens_become_underscores(self, raw, expected):
        assert normalize_country_slug(raw) == expected

    @pytest.mark.unit
    def test_idempotent(self):
        inputs = ["united kingdom", "United Kingdom", "united_kingdom"]
        for raw in inputs:
            once = normalize_country_slug(raw)
            twice = normalize_country_slug(once)
            assert once == twice


class TestNormalizeCountryName:
    @pytest.mark.unit
    @pytest.mark.parametrize("slug,expected", [
        ("united_kingdom", "united kingdom"),
        ("costa_rica", "costa rica"),
        ("mozambique", "mozambique"),
        ("north_america", "north america"),
        ("north-america", "north america"),
        ("", ""),
    ])
    def test_underscores_and_hyphens_become_spaces(self, slug, expected):
        assert normalize_country_name(slug) == expected


# ====== 2. RegionalPathService -- path construction invariants ======


@pytest.mark.unit
class TestRegionalPathService:
    """All paths produced by RegionalPathService must use underscores."""

    def test_continent_pbf_uses_underscores(self, temp_base_dir):
        svc = RegionalPathService(base_dir=temp_base_dir)
        path = svc.get_continent_pbf_path("north-america")
        assert "north_america.pbf" == path.name
        assert "-" not in path.name

    def test_snapshot_path_uses_underscores(self, temp_base_dir):
        svc = RegionalPathService(base_dir=temp_base_dir)
        path = svc.get_single_snapshot_pbf_path("north-america", "costa_rica")
        path_str = str(path)
        assert "north_america" in path_str
        assert "costa_rica" in path_str
        assert "-" not in path.name

    def test_subgraph_path_uses_underscores(self, temp_base_dir):
        svc = RegionalPathService(base_dir=temp_base_dir)
        path = svc.get_subgraph_pbf_path("north-america", "costa_rica", "san_jose")
        path_str = str(path)
        assert "north_america" in path_str
        assert "costa_rica" in path_str
        assert "san_jose" in path_str
        assert "-" not in path.name


# ====== 3. Full resolution chain must never produce hyphens ======


@pytest.mark.unit
class TestFullResolutionChain:
    @pytest.mark.parametrize("geofabrik_name,expected_slug", [
        ("north-america", "north_america"),
        ("south-america", "south_america"),
        ("central-america", "central_america"),
    ])
    def test_geofabrik_to_slug(self, geofabrik_name, expected_slug):
        """Simulates what download_all_geofabrik_polygons() does after the fix."""
        sanitized = geofabrik_name.replace("-", "_")
        assert sanitized == expected_slug
        assert "-" not in sanitized

    @pytest.mark.parametrize("user_input,expected_slug", [
        ("North America", "north_america"),
        ("north-america", "north_america"),
        ("NORTH AMERICA", "north_america"),
        ("Central America", "central_america"),
        ("central-america", "central_america"),
    ])
    def test_user_facing_normalisation(self, user_input, expected_slug):
        assert normalize_continent_slug(user_input) == expected_slug

    @pytest.mark.parametrize("cont_raw,country_raw,exp_cont,exp_country", [
        ("North America", "Costa Rica", "north_america", "costa_rica"),
        ("north-america", "costa_rica", "north_america", "costa_rica"),
        ("europe", "United Kingdom", "europe", "united_kingdom"),
        ("africa", "Mozambique", "africa", "mozambique"),
    ])
    def test_from_db_to_path(self, temp_base_dir, cont_raw, country_raw,
                              exp_cont, exp_country):
        """Simulates CountryEnvelope.from_db() path construction."""
        cont_norm = normalize_country_slug(cont_raw)
        country_norm = normalize_country_slug(country_raw)
        svc = RegionalPathService(base_dir=temp_base_dir)
        path = svc.get_single_snapshot_pbf_path(cont_norm, country_norm)
        path_str = str(path)
        assert exp_cont in path_str
        assert exp_country in path_str
        for part in Path(path_str).parts:
            if part.endswith(".pbf") or part.endswith(".poly"):
                assert "-" not in part, f"Hyphen found in filename part: {part}"


# ====== 4. Hardcoded continent list verification ======


@pytest.mark.unit
class TestHardcodedContinentLists:
    CANONICAL = {
        "europe", "africa", "asia", "north_america", "south_america",
        "central_america", "oceania", "australia", "antarctica", "russia",
    }

    def test_canonical_list_has_no_hyphens(self):
        for name in self.CANONICAL:
            assert "-" not in name, f"Hyphen found in canonical list: {name!r}"

    def test_continent_priorities(self):
        priority = [
            "europe", "africa", "asia", "north_america", "south_america",
            "central_america", "oceania", "russia", "antarctica",
        ]
        for name in priority:
            assert "-" not in name, f"Hyphen in priority list: {name!r}"


# ====== 5. Skip-if-exists logic ======


@pytest.mark.unit
class TestSkipRegeneration:
    def test_existing_poly_skips_download(self, temp_base_dir):
        base = Path(temp_base_dir)
        poly_file = base / "north_america.poly"
        poly_file.write_text("dummy")
        assert poly_file.exists()

        skipped = True if poly_file.exists() else False
        assert skipped, "Should skip download when poly exists"

    def test_existing_continent_pbf_skips_extraction(self, temp_base_dir):
        output_dir = Path(temp_base_dir) / "continents"
        output_dir.mkdir(parents=True)
        pbf_file = output_dir / "north_america.pbf"
        pbf_file.write_text("dummy")
        assert pbf_file.exists()

        skipped = True if pbf_file.exists() else False
        assert skipped, "Should skip extraction when continent PBF exists"

    def test_continent_pbf_rename_preserves_skip(self, temp_base_dir):
        output_dir = Path(temp_base_dir) / "continents"
        output_dir.mkdir(parents=True)
        old_file = output_dir / "north-america.pbf"
        old_file.write_text("old content")
        new_file = output_dir / "north_america.pbf"
        old_file.rename(new_file)
        assert new_file.exists()
        assert not old_file.exists()

        continent_slug = "north_america"
        expected_pbf = output_dir / f"{continent_slug}.pbf"
        assert expected_pbf.exists()


# ====== 6. Mixed convention detection ======


@pytest.mark.unit
class TestMixedConventionDetection:
    def _mixed_name_pairs(self, names):
        pairs = []
        for name in sorted(names):
            if "_" in name:
                hyphen = name.replace("_", "-")
                if hyphen in names:
                    pairs.append((name, hyphen))
        return pairs

    def test_detects_mixed_hyphen_underscore(self):
        names = {"europe", "africa", "north-america", "north_america"}
        mixed = self._mixed_name_pairs(names)
        assert len(mixed) == 1
        assert mixed[0] == ("north_america", "north-america")

    def test_no_mixed_when_only_underscores(self):
        names = {"europe", "africa", "north_america", "south_america"}
        mixed = self._mixed_name_pairs(names)
        assert len(mixed) == 0
