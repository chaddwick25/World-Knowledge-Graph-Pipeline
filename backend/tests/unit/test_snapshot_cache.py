"""Unit tests for the get_latest_snapshot_id TTL cache.

The cold path queries the vectors DB (blocked in unit tests — the failure is
fail-soft to None). These tests exercise the cache contract: seeding, hits,
TTL expiry, and clear — no real DB required.
"""

import time

import pytest

from worldkg_nca import snapshot_utils


@pytest.fixture(autouse=True)
def _clean_cache(monkeypatch):
    snapshot_utils.clear_snapshot_cache()
    monkeypatch.delenv("SNAPSHOT_ID_CACHE_TTL_SECONDS", raising=False)
    yield
    snapshot_utils.clear_snapshot_cache()


class TestSnapshotCache:
    def test_hit_returns_seeded_value(self):
        snapshot_utils._SNAPSHOT_CACHE["latest"] = {
            "at": time.monotonic(), "value": "2025_12_31",
        }
        assert snapshot_utils.get_latest_snapshot_id() == "2025_12_31"

    def test_expired_entry_is_not_returned(self, monkeypatch):
        # Stale timestamp (long past TTL) → cache miss → cold path runs.
        # In the unit env the vectors DB is blocked, so the cold path
        # fail-softs to None and repopulates the cache — the stale value
        # must never leak through.
        snapshot_utils._SNAPSHOT_CACHE["latest"] = {
            "at": time.monotonic() - 9999.0, "value": "stale",
        }
        monkeypatch.setenv("SNAPSHOT_ID_CACHE_TTL_SECONDS", "0")
        assert snapshot_utils.get_latest_snapshot_id() is None
        assert snapshot_utils._SNAPSHOT_CACHE["latest"]["value"] is None

    def test_clear_removes_entry(self):
        snapshot_utils._SNAPSHOT_CACHE["latest"] = {
            "at": time.monotonic(), "value": "x",
        }
        snapshot_utils.clear_snapshot_cache()
        assert "latest" not in snapshot_utils._SNAPSHOT_CACHE

    def test_ttl_from_env(self, monkeypatch):
        assert snapshot_utils._snapshot_cache_ttl() == 120.0
        monkeypatch.setenv("SNAPSHOT_ID_CACHE_TTL_SECONDS", "5")
        assert snapshot_utils._snapshot_cache_ttl() == 5.0
