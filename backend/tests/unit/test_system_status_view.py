"""
Unit tests for GET /api/system/status/ (SystemStatusView).

Covers the readiness semantics that were previously broken on the frontend:

* ``ready`` is driven by a *finalized* PlanetSnapshot (``snapshot_date_str``
  populated + COMPLETED) — the ``finalize`` step of the ``init_planet``
  Docker startup command — NOT by a COMPLETED planet PbfFile row, which
  ``register_planet`` (the first init step) creates before init finishes.
* The response carries everything the home page hydrates from in one ping:
  readiness, snapshot info, suggested planet path, extraction/embedding
  counts, and the snapshot-date range.
"""
import pytest
from datetime import date

from django.urls import reverse
from django.utils import timezone

from core.models import PlanetSnapshot, PbfFile, CountryPipelineProfile


@pytest.mark.django_db
class TestSystemStatusView:
    def _get(self, client):
        return client.get(reverse("system_status"))

    def test_not_ready_when_no_snapshot(self, client):
        resp = self._get(client)
        assert resp.status_code == 200
        data = resp.json()
        assert data["ready"] is False
        assert data["snapshot"] is None

    def test_not_ready_when_register_planet_row_only(self, client):
        # register_planet writes a COMPLETED row with snapshot_date_str NULL
        # before the rest of init runs — must NOT count as ready.
        PlanetSnapshot.objects.create(
            snapshot_date=date.today(),
            planet_osm_path="/data/planet.osm.pbf",
            status=PlanetSnapshot.SnapshotStatus.COMPLETED,
        )
        assert self._get(client).json()["ready"] is False

    def test_ready_when_finalized_snapshot(self, client):
        PlanetSnapshot.objects.create(
            snapshot_date_str="2026_08_25",
            snapshot_date=date(2026, 8, 25),
            planet_osm_path="/data/planet.osm.pbf",
            status=PlanetSnapshot.SnapshotStatus.COMPLETED,
            completed_at=timezone.now(),
        )
        data = self._get(client).json()
        assert data["ready"] is True
        assert data["snapshot"]["status"] == "COMPLETED"
        assert data["snapshot"]["snapshot_date"] == "2026_08_25"
        assert data["snapshot"]["completed_at"] is not None

    def test_not_ready_when_finalized_snapshot_failed(self, client):
        PlanetSnapshot.objects.create(
            snapshot_date_str="2026_08_25",
            snapshot_date=date(2026, 8, 25),
            planet_osm_path="/data/planet.osm.pbf",
            status=PlanetSnapshot.SnapshotStatus.FAILED,
        )
        assert self._get(client).json()["ready"] is False

    def test_prefers_latest_finalized_snapshot(self, client):
        PlanetSnapshot.objects.create(
            snapshot_date_str="2024_12_31",
            snapshot_date=date(2024, 12, 31),
            planet_osm_path="/data/old.osm.pbf",
            status=PlanetSnapshot.SnapshotStatus.COMPLETED,
        )
        PlanetSnapshot.objects.create(
            snapshot_date_str="2025_12_31",
            snapshot_date=date(2025, 12, 31),
            planet_osm_path="/data/new.osm.pbf",
            status=PlanetSnapshot.SnapshotStatus.COMPLETED,
        )
        data = self._get(client).json()
        assert data["ready"] is True
        assert data["snapshot"]["snapshot_date"] == "2025_12_31"

    def test_counts_and_hydration_fields_present(self, client):
        PlanetSnapshot.objects.create(
            snapshot_date_str="2026_08_25",
            snapshot_date=date(2026, 8, 25),
            planet_osm_path="/data/planet.osm.pbf",
            status=PlanetSnapshot.SnapshotStatus.COMPLETED,
        )
        data = self._get(client).json()
        assert "continents_extracted" in data
        assert "countries_with_embeddings" in data
        assert "total_countries" in data
        assert "suggested_planet_file_path" in data
        assert "snapshot_dates" in data
        assert "completed_dates" in data
        assert "default" in data
        assert isinstance(data["snapshot_dates"], list)
