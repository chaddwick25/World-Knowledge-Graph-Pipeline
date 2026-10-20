"""
Tests for the 2026-10-11 tavern-run fixes: nearest-by-class for
OBJECT-FIELD-MEASURE (#2), and degenerate-distance stripping in the
primary digest.

The run's evidence: "How far is the nearest tavern from Dublin?"
geocoded the PHRASE as a place name (the fuzzy name tier matched
"tavern"-named entities 146-302km away for what the data answers at
meters: Wallace's Taverna 0.5km, Dame Tavern 0.7km), and the assembler
wrote "clustered in the same location" over a degenerate result set
even with the prompt rule live. Fixes: a superlative class phrase
resolves to nearest-by-class from the other endpoint (case-sensitive
qualifier strip, so "The Top Bar" stays a place), and the digest strips
degenerate distances entirely (remove the data, not just forbid claims
about it). Heavy deps are stubbed per the geo_uslp test's pattern; the
full integration was verified live in the container. No DB / network
required.
"""

import pytest

from semantic_search.services.query_enrichment_service import (
    QueryEnrichmentService,
    _distances_degenerate,
)
from semantic_search.services.query_executor_service.spatial_search import (
    SpatialSearchMixin,
)
from semantic_search.services.query_executor_service import QueryExecutorService
from semantic_search.services.query_executor_service.spatial_search import (
    SpatialSearchMixin,
)
from semantic_search.services.query_executor_service.template_executors import (
    TemplateExecutorsMixin,
)
from semantic_search.services.entity_geocoder import EntityGeocoder

EXECUTE = QueryExecutorService._execute_object_field_measure
STRIP = TemplateExecutorsMixin._strip_generic_qualifiers


def _concepts(*texts):
    return [{"type": "LOCATION", "text": t} for t in texts]


@pytest.fixture
def _stub_geocode(monkeypatch):
    calls = []

    def fake_geocode(name, country_code=None, snapshot_date=None):
        calls.append(name)
        return {
            "Dublin": {"osm_id": 1, "osm_type": "node", "name": "Dublin",
                       "lat": 53.34938, "lon": -6.26056, "tags": {}},
            "Galway": {"osm_id": 2, "osm_type": "node", "name": "Galway",
                       "lat": 53.27090, "lon": -9.05590, "tags": {}},
        }.get(name)

    monkeypatch.setattr(
        EntityGeocoder, "geocode", staticmethod(fake_geocode)
    )
    return calls


@pytest.fixture
def _stub_class_hits(monkeypatch):
    calls = []
    hits = [
        {"name": "Wallace's Taverna", "lat": 53.34500, "lon": -6.26500,
         "osm_id": 10, "osm_type": "node", "distance_m": 500.0},
        {"name": "Dame Tavern", "lat": 53.34300, "lon": -6.26300,
         "osm_id": 11, "osm_type": "node", "distance_m": 700.0},
    ]

    def fake_search(amenity_type, country_code, snapshot_date,
                    anchor_point=None, radius_m=None, top_k=50,
                    trace=None, name_pool_query=None, tag_target=None):
        calls.append({
            "amenity_type": amenity_type, "tag_target": tag_target,
            "top_k": top_k, "radius_m": radius_m,
            "anchor": (anchor_point.x, anchor_point.y) if anchor_point else None,
        })
        return hits[:top_k]

    monkeypatch.setattr(
        SpatialSearchMixin, "_search_by_amenity_spatial",
        classmethod(lambda cls, *a, **k: fake_search(*a, **k)),
    )
    return calls


def test_strip_generic_qualifiers():
    assert STRIP("the nearest tavern") == ("tavern", 0)
    assert STRIP("the second nearest tavern") == ("tavern", 1)
    assert STRIP("the closest cafe") == ("cafe", 0)
    assert STRIP("the top tavern") == ("tavern", 0)
    assert STRIP("Dublin") == ("Dublin", 0)
    assert STRIP("The Top Bar") == ("The Top Bar", 0)


def test_nearest_by_class_resolves_from_other_endpoint(
        _stub_geocode, _stub_class_hits):
    out = EXECUTE(
        _concepts("the nearest tavern", "Dublin"), "IE", None, [],
    )
    assert len(out) == 1
    r = out[0]
    assert r["from_resolved"] == "Wallace's Taverna"
    assert r["from"] == "the nearest tavern"
    assert 400 < r["distance_m"] < 600
    assert _stub_class_hits[0]["tag_target"] == ("amenity", "pub")
    assert _stub_class_hits[0]["top_k"] == 1
    assert _stub_class_hits[0]["radius_m"] is None
    # The class side never geocodes; the place side geocodes once.
    assert _stub_geocode == ["Dublin"]


def test_second_nearest_uses_ordinal(_stub_geocode, _stub_class_hits):
    out = EXECUTE(
        _concepts("the second nearest tavern", "Dublin"), "IE", None, [],
    )
    assert out[0]["from_resolved"] == "Dame Tavern"
    assert _stub_class_hits[0]["top_k"] == 2


def test_both_places_regression(_stub_geocode, _stub_class_hits):
    out = EXECUTE(_concepts("Galway", "Dublin"), "IE", None, [])
    assert len(out) == 1
    r = out[0]
    assert "from_resolved" not in r and "to_resolved" not in r
    assert _stub_class_hits == []
    assert sorted(_stub_geocode) == ["Dublin", "Galway"]
    # Galway to Dublin is ~187 km.
    assert 180000 < r["distance_m"] < 195000


def test_both_class_phrases_error_honestly(_stub_geocode, _stub_class_hits):
    out = EXECUTE(
        _concepts("the nearest tavern", "the closest cafe"),
        "IE", None, [],
    )
    assert out == [{"error": "Could not geocode one or both entities"}]


def test_distances_degenerate_detector():
    assert _distances_degenerate([
        {"distance_m": 0.0}, {"distance_m": 0.0}, {"distance_m": 0.0},
    ]) is True
    assert _distances_degenerate([
        {"distance_m": 0.0}, {"distance_m": 14.2},
    ]) is False
    assert _distances_degenerate([{"name": "x"}]) is False
    assert _distances_degenerate("nope") is False


def test_digest_strips_degenerate_distances():
    results = [
        {"name": "A", "wkg_class": "wkgs:Amenity", "distance_m": 0.0},
        {"name": "B", "wkg_class": "wkgs:Amenity", "distance_m": 0.0},
    ]
    digest = QueryEnrichmentService._primary_digest(results)
    assert "0m" not in digest
    assert "distances: unavailable" in digest
    assert "- A" in digest and "- B" in digest


def test_digest_keeps_real_distances():
    results = [
        {"name": "A", "wkg_class": "wkgs:Amenity", "distance_m": 14.2},
        {"name": "B", "wkg_class": "wkgs:Amenity", "distance_m": 50.7},
    ]
    digest = QueryEnrichmentService._primary_digest(results)
    assert "(14m)" in digest
    assert "(51m)" in digest
    assert "distances: unavailable" not in digest
