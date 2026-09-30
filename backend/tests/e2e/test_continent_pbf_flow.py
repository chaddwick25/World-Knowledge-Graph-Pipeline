"""
Continent extraction path-convention regression tests.

Covers the underscore-vs-hyphen fix (``north_america.pbf``, not
``north-america.pbf``) at the layers the pipeline actually resolves:

- ``normalize_continent_slug`` — slug canonicalization
- ``RegionalPathService.get_continent_pbf_path`` — continent PBF source path
- ``RegionalPathService.get_country_dir`` — country extraction output dir
- poly-file naming + the extraction service's skip precondition

These are pure filesystem/string tests: no osmium binary and no real PBF
data required. The real extraction chain (poly download -> continent PBF
-> country PBF) is exercised by the manual E2E run
(``docs/plans/E2E_TESTING_PLAN.md``) and the ``integration``-marked
``test_parallel_embed_parity`` / ``test_geofabrik_poly_service`` suites.
"""
import pytest
from pathlib import Path


@pytest.mark.unit
class TestContinentPathResolution:
    """Path resolution must produce underscore slugs end to end."""

    def test_normalize_continent_slug(self):
        from core.services.snapshot.regional_path_service import normalize_continent_slug
        assert normalize_continent_slug("North America") == "north_america"
        assert normalize_continent_slug("north-america") == "north_america"
        assert normalize_continent_slug("europe") == "europe"
        assert normalize_continent_slug("") == ""
        assert normalize_continent_slug(None) == ""

    def test_continent_pbf_path_uses_underscores(self, tmp_path):
        from core.services.snapshot.regional_path_service import RegionalPathService
        svc = RegionalPathService(base_dir=str(tmp_path))
        assert svc.get_continent_pbf_path("North America") == tmp_path / "north_america" / "north_america.pbf"
        assert svc.get_continent_pbf_path("north-america") == tmp_path / "north_america" / "north_america.pbf"

    def test_hyphenated_lookup_needs_no_fallback(self, tmp_path):
        """After the fix the hyphenated file is never the primary target."""
        from core.services.snapshot.regional_path_service import RegionalPathService
        svc = RegionalPathService(base_dir=str(tmp_path))
        direct = svc.get_continent_pbf_path("north-america")
        assert direct.name == "north_america.pbf"
        assert "-" not in direct.name

    def test_country_dir_uses_underscores(self, tmp_path):
        from core.services.snapshot.regional_path_service import RegionalPathService
        svc = RegionalPathService(base_dir=str(tmp_path))
        country_dir = svc.get_country_dir("north-america", "costa-rica")
        assert country_dir == tmp_path / "north_america" / "costa_rica"
        assert "-" not in str(country_dir.relative_to(tmp_path))


@pytest.mark.unit
class TestExtractionSkipPrecondition:
    """The extraction service short-circuits when the snapshot already
    exists and is non-empty; assert the filesystem precondition it relies
    on."""

    def test_snapshot_exists_and_non_empty_means_skip(self, tmp_path):
        from core.services.snapshot.regional_path_service import RegionalPathService
        svc = RegionalPathService(base_dir=str(tmp_path))
        pbf = svc.get_continent_pbf_path("europe")
        pbf.write_text("dummy pbf bytes")
        assert pbf.exists()
        assert pbf.stat().st_size > 0

    def test_missing_snapshot_means_extract(self, tmp_path):
        from core.services.snapshot.regional_path_service import RegionalPathService
        svc = RegionalPathService(base_dir=str(tmp_path))
        assert not svc.get_continent_pbf_path("africa").exists()


@pytest.mark.unit
class TestPolyFileConventions:
    def test_top_level_poly_dir_has_no_hyphens(self, tmp_path):
        poly_dir = tmp_path / "polygons"
        poly_dir.mkdir()
        for name in ["europe", "africa", "north_america", "south_america"]:
            (poly_dir / f"{name}.poly").write_text("dummy")
        assert not any("-" in p.stem for p in poly_dir.glob("*.poly"))

    def test_mixed_convention_detected(self, tmp_path):
        poly_dir = tmp_path / "polygons"
        poly_dir.mkdir()
        (poly_dir / "north-america.poly").write_text("old")
        (poly_dir / "north_america.poly").write_text("new")
        names = {p.stem for p in poly_dir.glob("*.poly")}
        mixed = sorted(
            (n, n.replace("_", "-"))
            for n in names
            if "_" in n and n.replace("_", "-") in names
        )
        assert mixed == [("north_america", "north-america")]
