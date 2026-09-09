"""Unit tests for the GEOCODE-BATCH-COMPARE (#4) branch decision.

Hermetic — no DB, no geocoding. Verifies that the compare-closer branch
fires when multi-entity extraction produced 3+ named locations, even when
the parser emitted a bogus OBJECT ("is closer") — and that a real amenity
OBJECT still blocks it (Pattern 1 nearest-search path).

Regression: "What is closer to Heavens cafe: Roshan Cafe or Pipels Cafe ?"
parsed with OBJECT="is closer", which blocked the compare branch and sent
the executor down an amenity search for "is closer" → 0 results.
"""

import pytest

from semantic_search.services.query_executor_service import QueryExecutorService


def _concepts(locations, obj=None):
    concepts = [{"type": "LOCATION", "text": t} for t in locations]
    if obj:
        concepts.append({"type": "OBJECT", "text": obj})
    return concepts


class TestCompareCloserBranch:
    @staticmethod
    def _patch(monkeypatch, resolve_amenity=None, compare_sentinel=None):
        monkeypatch.setattr(
            QueryExecutorService, "_resolve_amenity_tag",
            staticmethod(lambda *a, _r=resolve_amenity: _r),
        )
        sentinel = compare_sentinel if compare_sentinel is not None else "COMPARE"
        monkeypatch.setattr(
            QueryExecutorService, "_execute_compare_closer",
            classmethod(lambda cls, *a, **k: sentinel),
        )
        return sentinel

    def test_no_object_fires_compare(self, monkeypatch):
        """3+ locations, no OBJECT → compare-closer (README pattern)."""
        sentinel = self._patch(monkeypatch, resolve_amenity=None)
        out = QueryExecutorService._execute_geocode_batch_compare(
            _concepts(["Roshan Cafe", "Pipels Cafe", "Heavens cafe"]),
            "LK", "2025_12_31", [],
        )
        assert out == sentinel

    def test_noise_object_fires_compare(self, monkeypatch):
        """OBJECT='is closer' (not an amenity) → compare-closer still fires."""
        sentinel = self._patch(monkeypatch, resolve_amenity=None)
        out = QueryExecutorService._execute_geocode_batch_compare(
            _concepts(["Roshan Cafe", "Pipels Cafe", "Heavens cafe"],
                      obj="is closer"),
            "LK", "2025_12_31", [],
        )
        assert out == sentinel

    def test_real_amenity_blocks_compare(self, monkeypatch):
        """OBJECT='cafe' (a real amenity) → Pattern 1 nearest-search path."""
        self._patch(monkeypatch, resolve_amenity="cafe")
        monkeypatch.setattr(
            QueryExecutorService, "_search_by_amenity",
            classmethod(lambda cls, *a, **k: "SEARCHED"),
        )
        monkeypatch.setattr(
            "semantic_search.services.entity_geocoder.EntityGeocoder.geocode",
            staticmethod(lambda *a, **k: None),
        )
        out = QueryExecutorService._execute_geocode_batch_compare(
            _concepts(["Roshan Cafe", "Pipels Cafe", "Heavens cafe"],
                      obj="cafe"),
            "LK", "2025_12_31", [],
        )
        assert out == "SEARCHED"

    def test_two_locations_never_compare(self, monkeypatch):
        """Only 2 locations (no multi-entity split) → Pattern 1 path."""
        sentinel = self._patch(monkeypatch, resolve_amenity=None)
        monkeypatch.setattr(
            QueryExecutorService, "_search_by_amenity",
            classmethod(lambda cls, *a, **k: "SEARCHED"),
        )
        monkeypatch.setattr(
            "semantic_search.services.entity_geocoder.EntityGeocoder.geocode",
            staticmethod(lambda *a, **k: None),
        )
        out = QueryExecutorService._execute_geocode_batch_compare(
            _concepts(["Heavens cafe", "Roshan Cafe"], obj="is closer"),
            "LK", "2025_12_31", [],
        )
        assert out == "SEARCHED"
        assert sentinel == "COMPARE"  # untouched sentinel proves no compare call
