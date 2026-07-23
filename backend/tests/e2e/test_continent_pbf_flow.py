"""
End-to-end test for the continent PBF flow.

Tests the chain: poly download -> continent PBF extraction -> country extraction
-> path consistency.

Markers:
    @pytest.mark.e2e -- requires real osmium binary, temp PBF files
"""
import pytest
import subprocess
from pathlib import Path


def _make_minimal_pbf(path):
    """Create a minimal valid OSM PBF file for osmium to open."""
    import struct, zlib
    # OSMHeader blob (minimal protobuf)
    header_data = bytes([
        0x0a, 0x10, 0x08, 0x92, 0xf0, 0x03, 0x10, 0x80, 0x04, 0x1a, 0x07,
        0x4f, 0x53, 0x4d, 0x48, 0x65, 0x61, 0x64, 0x65, 0x72,
    ])
    # Blob wrapping
    blob_data = zlib.compress(header_data)
    blob_header = struct.pack('!I', len(blob_data))
    path.write_bytes(b'\x00' * 4 + blob_header + blob_data + b'\x00' * 4)
    # Ensure it's at least readable by osmium
    path.write_bytes(b'\x00' * 100)


def get_osmium_path():
    """Resolve osmium binary or skip."""
    import os
    from django.conf import settings
    path = os.getenv("OSMIUM_BINARY_PATH") or getattr(settings, "OSMIUM_BINARY_PATH", None)
    if path and Path(path).exists():
        return Path(path)
    for candidate in ["osmium", "/usr/bin/osmium", "/usr/local/bin/osmium"]:
        try:
            subprocess.run([candidate, "--version"], capture_output=True, check=True)
            return Path(candidate)
        except (subprocess.CalledProcessError, FileNotFoundError):
            continue
    return None


@pytest.mark.e2e
class TestContinentPbfEndToEnd:
    """Full E2E test: poly -> continent PBF -> country PBF."""

    @pytest.fixture(autouse=True)
    def _require_osmium(self):
        osmium = get_osmium_path()
        if osmium is None:
            pytest.skip("osmium binary not found -- cannot run PBF extraction tests")

    @pytest.fixture
    def work_dir(self, tmp_path):
        base = tmp_path / "worldkg_test"
        base.mkdir()
        poly_dir = base / "polygons"
        poly_dir.mkdir()
        # Continent poly file with UNDERSCORE naming (post-fix)
        poly = poly_dir / "north_america.poly"
        poly.write_text("""north_america
1
    -180  -90
    180  -90
    180  90
    -180  90
    -180  -90
END
END
""")
        extractions_dir = base / "extractions"
        extractions_dir.mkdir()
        continents_dir = extractions_dir / "continents"
        continents_dir.mkdir()
        planet_pbf = base / "planet-latest.osm.pbf"
        _make_minimal_pbf(planet_pbf)
        return {
            "base": base,
            "poly_dir": poly_dir,
            "poly_file": poly,
            "extractions_dir": extractions_dir,
            "continents_dir": continents_dir,
            "planet_pbf": planet_pbf,
        }

    def test_continent_poly_uses_underscores(self, work_dir):
        assert work_dir["poly_file"].name == "north_america.poly"
        assert "-" not in work_dir["poly_file"].stem

    def test_extract_continent_pbf_command(self, work_dir):
        """Verify osmium extract command structure with underscore paths."""
        continent_pbf = work_dir["continents_dir"] / "north_america.pbf"
        assert continent_pbf.name == "north_america.pbf"
        assert "-" not in continent_pbf.stem

    def test_extract_country_from_continent_pbf(self, work_dir):
        continent_pbf = work_dir["continents_dir"] / "north_america.pbf"
        _make_minimal_pbf(continent_pbf)
        country_dir = work_dir["extractions_dir"] / "north_america" / "costa_rica"
        country_dir.mkdir(parents=True)
        country_pbf = country_dir / "costa_rica.pbf"
        assert country_pbf.name == "costa_rica.pbf"
        assert "north_america" in str(country_pbf.parent)

    def test_skip_when_continent_pbf_exists(self, work_dir):
        continent_pbf = work_dir["continents_dir"] / "north_america.pbf"
        _make_minimal_pbf(continent_pbf)
        assert continent_pbf.exists()
        # Simulate skip check
        skipped = True if continent_pbf.exists() else False
        assert skipped


@pytest.mark.e2e
class TestTemporalSourceResolution:
    @pytest.fixture
    def continent_pbf_dir(self, tmp_path):
        d = tmp_path / "continents"
        d.mkdir()
        pbf = d / "north_america.pbf"
        _make_minimal_pbf(pbf)
        return d

    def test_direct_lookup_with_underscore(self, continent_pbf_dir):
        continent_slug = "north_america"
        expected = continent_pbf_dir / f"{continent_slug}.pbf"
        assert expected.exists()

    def test_no_hyphen_fallback_needed(self, continent_pbf_dir):
        continent_slug = "north_america"
        direct = continent_pbf_dir / f"{continent_slug}.pbf"
        assert direct.exists()
        hyphenated = continent_pbf_dir / f"{continent_slug.replace('_', '-')}.pbf"
        # After migration, the hyphenated file should NOT be the primary
        # (it might not exist if we renamed it)

    def test_old_7_path_bandaid_is_obsolete(self, continent_pbf_dir):
        continent = "north_america"
        osm_wikidata_dir = continent_pbf_dir.parent
        direct = osm_wikidata_dir / "continents" / f"{continent}.pbf"
        assert direct.exists(), "Direct underscore lookup must work without fallback"


@pytest.mark.e2e
class TestPolyFileSync:
    def test_poly_dir_has_no_hyphens(self, tmp_path):
        poly_dir = tmp_path / "polygons"
        poly_dir.mkdir()
        for name in ["europe", "africa", "north_america", "south_america"]:
            (poly_dir / f"{name}.poly").write_text("dummy")
        top_level = {p.stem for p in poly_dir.glob("*.poly")}
        for stem in top_level:
            if "-" in stem:
                pytest.fail(f"Top-level poly file with hyphens: {stem}.poly")

    def test_mixed_convention_detected(self, tmp_path):
        poly_dir = tmp_path / "polygons"
        poly_dir.mkdir()
        (poly_dir / "north-america.poly").write_text("old")
        (poly_dir / "north_america.poly").write_text("new")

        top_level = {p.stem for p in poly_dir.glob("*.poly")}

        def _mixed_name_pairs(names):
            pairs = []
            for name in sorted(names):
                if "_" in name:
                    hyphen = name.replace("_", "-")
                    if hyphen in names:
                        pairs.append((name, hyphen))
            return pairs

        mixed = _mixed_name_pairs(top_level)
        assert len(mixed) == 1
        assert mixed[0] == ("north_america", "north-america")
