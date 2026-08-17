"""
Integration tests for download_all_geofabrik_polygons().

Tests the critical fix: top-level continent poly files must now use
underscores (north_america.poly) instead of hyphens (north-america.poly).

Markers:
    @pytest.mark.integration -- uses temp filesystem + mocked HTTP
"""
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock


@pytest.fixture
def fake_geofabrik_index():
    return {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "properties": {"id": "europe", "name": "Europe", "parent": None}},
            {"type": "Feature", "properties": {"id": "africa", "name": "Africa", "parent": None}},
            {"type": "Feature", "properties": {"id": "north-america", "name": "North America", "parent": None}},
            {"type": "Feature", "properties": {"id": "central-america", "name": "Central America", "parent": None}},
            {"type": "Feature", "properties": {"id": "great-britain", "name": "Great Britain", "parent": "europe"}},
            {"type": "Feature", "properties": {"id": "algeria", "name": "Algeria", "parent": "africa"}},
        ],
    }


@pytest.fixture
def mock_geofabrik_index(fake_geofabrik_index):
    with patch("core.services.planet_init.geofabrik_poly_service.geofabrik_index_service") as mock_service:
        mock_service.fetch_index.return_value = fake_geofabrik_index
        mock_service.INDEX_URL = "https://download.geofabrik.de/index-v1.json"
        yield mock_service


@pytest.fixture
def mock_requests():
    with patch("core.services.planet_init.geofabrik_poly_service.requests") as mock_req:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b"dummy poly content\n"
        mock_req.get.return_value = mock_response
        yield mock_req


@pytest.fixture
def poly_base_dir(tmp_path):
    d = tmp_path / "osm_polygon_files"
    d.mkdir(parents=True)
    return d


# ====== 1. THE FIX -- top-level continents must use underscores ======


@pytest.mark.integration
class TestGeofabrikPolyServiceFix:

    def test_north_america_poly_uses_underscores(self, poly_base_dir, mock_geofabrik_index, mock_requests):
        from core.services.planet_init.geofabrik_poly_service import download_all_geofabrik_polygons
        stats = download_all_geofabrik_polygons(base_dir=str(poly_base_dir))
        expected = poly_base_dir / "north_america.poly"
        assert expected.exists(), (
            f"Expected north_america.poly to exist, found: {list(poly_base_dir.iterdir())}"
        )
        assert expected.read_text() == "dummy poly content\n"
        old = poly_base_dir / "north-america.poly"
        assert not old.exists(), "north-america.poly should NOT exist after fix"
        assert stats["downloaded"] >= 1

    def test_central_america_poly_uses_underscores(self, poly_base_dir, mock_geofabrik_index, mock_requests):
        from core.services.planet_init.geofabrik_poly_service import download_all_geofabrik_polygons
        download_all_geofabrik_polygons(base_dir=str(poly_base_dir))
        expected = poly_base_dir / "central_america.poly"
        assert expected.exists()
        old = poly_base_dir / "central-america.poly"
        assert not old.exists()

    def test_single_word_continents_unchanged(self, poly_base_dir, mock_geofabrik_index, mock_requests):
        from core.services.planet_init.geofabrik_poly_service import download_all_geofabrik_polygons
        download_all_geofabrik_polygons(base_dir=str(poly_base_dir))
        for name in ("europe", "africa"):
            assert (poly_base_dir / f"{name}.poly").exists(), f"{name}.poly should exist"

    def test_skip_existing_file(self, poly_base_dir, mock_geofabrik_index, mock_requests):
        from core.services.planet_init.geofabrik_poly_service import download_all_geofabrik_polygons
        existing = poly_base_dir / "north_america.poly"
        existing.write_text("existing content")
        mock_requests.get.reset_mock()
        stats = download_all_geofabrik_polygons(base_dir=str(poly_base_dir))
        assert stats["skipped"] >= 1
        assert existing.read_text() == "existing content"
        north_calls = [c for c in mock_requests.get.call_args_list if "north-america" in str(c)]
        assert len(north_calls) == 0, "requests.get should not be called when poly exists"


# ====== 2. Download stats ======


@pytest.mark.integration
class TestDownloadStats:
    def test_stats_counts(self, poly_base_dir, mock_geofabrik_index, mock_requests):
        from core.services.planet_init.geofabrik_poly_service import download_all_geofabrik_polygons
        stats = download_all_geofabrik_polygons(base_dir=str(poly_base_dir))
        assert stats["downloaded"] > 0
        total = stats["downloaded"] + stats["skipped"] + stats["not_found"] + stats["errors"]
        assert total >= 5


# ====== 3. Resilience ======


@pytest.mark.integration
class TestDownloadResilience:
    def test_404_does_not_stop_processing(self, poly_base_dir, mock_geofabrik_index):
        with patch("core.services.planet_init.geofabrik_poly_service.requests") as mock_req:
            def side_effect(url, **kwargs):
                mock_resp = MagicMock()
                mock_resp.status_code = 404 if "north-america" in url else 200
                mock_resp.content = b"dummy\n"
                return mock_resp
            mock_req.get.side_effect = side_effect
            from core.services.planet_init.geofabrik_poly_service import download_all_geofabrik_polygons
            stats = download_all_geofabrik_polygons(base_dir=str(poly_base_dir))
            assert stats["not_found"] >= 1
            assert stats["downloaded"] >= 2


# ====== 4. Backward compatibility ======


@pytest.mark.integration
class TestBackwardCompatibility:
    def test_existing_underscore_file_not_overwritten(self, poly_base_dir, mock_geofabrik_index, mock_requests):
        from core.services.planet_init.geofabrik_poly_service import download_all_geofabrik_polygons
        existing = poly_base_dir / "north_america.poly"
        existing.write_text("existing content")
        mock_requests.get.reset_mock()
        download_all_geofabrik_polygons(base_dir=str(poly_base_dir))
        assert existing.read_text() == "existing content"
