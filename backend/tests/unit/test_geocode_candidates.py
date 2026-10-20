"""Unit tests for EntityGeocoder.geocode_candidates / seed_suggestions.

The live seed resolution (ISSUE-48) needs a ranked candidate list, not
the single `_pick` result: multi-hit names ("Jerk Center") render every
hit as a marker and the user clicks to select. Trigram tier only (the
~50ms indexed path); the unindexed tiers stay finalize-only.

Pure-Python tests — no DB required: the queryset chain is faked and
`_trigram_rows` is stubbed, so what is under test here is the
orchestration (variant order, head-row `_pick` semantics, dedupe,
ordering, floors). The SQL shape itself is the proven tier-1 query
(see test_entity_geocoder_guard.py for the DB-backed coverage).
"""

import pytest

from semantic_search.services.entity_geocoder import EntityGeocoder


class _FakeGeom:
    def __init__(self, x, y):
        self.x = x
        self.y = y
        self.point_on_surface = self


class _FakeEntity:
    def __init__(self, name, tags=None, geom=None, osm_id=1,
                 osm_type="node", wkg_class=None, country_code="JM"):
        self.tags = dict(tags or {})
        self.tags.setdefault("name", name)
        self.geom = geom
        self.osm_id = osm_id
        self.osm_type = osm_type
        self.wkg_class = wkg_class
        self.country_code = country_code


class _FakeManager:
    """Chainable stand-in for OsmEntity.objects (using/filter only)."""

    def using(self, db):
        return self

    def filter(self, **kw):
        return self


@pytest.fixture
def fake_objects(monkeypatch):
    from worldkg_nca.models import OsmEntity
    manager = _FakeManager()
    monkeypatch.setattr(OsmEntity, "objects", manager, raising=False)
    return manager


def _stub_rows(monkeypatch, rows_by_variant):
    """Stub `_trigram_rows` to return rows per queried variant."""

    def fake_trigram_rows(name_variant, qs, limit, min_sim):
        return rows_by_variant.get(name_variant, [])

    monkeypatch.setattr(
        EntityGeocoder, "_trigram_rows",
        staticmethod(fake_trigram_rows),
    )


def test_head_row_keeps_pick_semantics(fake_objects, monkeypatch):
    # candidates[0] by similarity is a coordinate-less row; the head
    # must be the `_pick` choice (coordinates first), deduped.
    no_coords = _FakeEntity("Jerk Center", tags={"amenity": "fast_food"},
                            geom=None, osm_id=11)
    with_coords = _FakeEntity("Jerk Center", tags={"amenity": "restaurant"},
                              geom=_FakeGeom(-77.9, 18.4), osm_id=12)
    _stub_rows(monkeypatch, {"Jerk Center": [no_coords, with_coords]})
    res = EntityGeocoder.geocode_candidates(
        "Jerk Center", "JM", "2025_12_31",
    )
    assert res["asked"] == "Jerk Center"
    assert [c["osm_id"] for c in res["candidates"]] == [12, 11]
    assert res["candidates"][0]["lat"] == 18.4


def test_head_row_is_candidates_first_when_pick_agrees(
    fake_objects, monkeypatch,
):
    row = _FakeEntity("The Friars Tavern", tags={"amenity": "pub"},
                      geom=_FakeGeom(-8.12, 53.09), osm_id=21)
    _stub_rows(monkeypatch, {"Friars Tavern": [row]})
    res = EntityGeocoder.geocode_candidates(
        "The Friars Tavern", "IE", "2025_12_31",
    )
    assert len(res["candidates"]) == 1
    assert res["candidates"][0]["osm_id"] == 21


def test_empty_on_no_rows(fake_objects, monkeypatch):
    _stub_rows(monkeypatch, {})
    res = EntityGeocoder.geocode_candidates(
        "Nowhere Place", "JM", "2025_12_31",
    )
    assert res == {"asked": "Nowhere Place", "candidates": []}


def test_first_variant_hit_skips_second_query(fake_objects, monkeypatch):
    # The cleaned name ("Friars Tavern") hits: the original variant
    # ("The Friars Tavern") must not run a second query.
    calls = []
    row = _FakeEntity("The Friars Tavern", tags={"amenity": "pub"},
                      geom=_FakeGeom(-8.12, 53.09), osm_id=21)

    def fake_trigram_rows(name_variant, qs, limit, min_sim):
        calls.append(name_variant)
        return [row] if name_variant == "Friars Tavern" else []

    monkeypatch.setattr(
        EntityGeocoder, "_trigram_rows", staticmethod(fake_trigram_rows),
    )
    res = EntityGeocoder.geocode_candidates(
        "The Friars Tavern", "IE", "2025_12_31",
    )
    assert calls == ["Friars Tavern"]
    assert res["candidates"][0]["osm_id"] == 21


def test_second_variant_falls_back(fake_objects, monkeypatch):
    row = _FakeEntity("Shandon Bells", tags={"amenity": "place_of_worship"},
                      geom=_FakeGeom(-8.47, 51.9), osm_id=31)
    _stub_rows(monkeypatch, {
        "Shannon Bells": [],
        "Shannon Bells ".strip(): [],
    })
    # The article-stripped variant misses, the original hits.
    def fake_trigram_rows(name_variant, qs, limit, min_sim):
        return [row] if name_variant == "Shannon Bells" else []

    monkeypatch.setattr(
        EntityGeocoder, "_trigram_rows", staticmethod(fake_trigram_rows),
    )
    res = EntityGeocoder.geocode_candidates(
        "Shannon Bells", "IE", "2025_12_31",
    )
    assert res["candidates"][0]["osm_id"] == 31


def test_no_snapshot_is_fail_soft(fake_objects, monkeypatch):
    import semantic_search.services.entity_geocoder as eg
    monkeypatch.setattr(eg, "get_latest_snapshot_id", lambda: None)
    _stub_rows(monkeypatch, {"X": []})
    res = EntityGeocoder.geocode_candidates("X", "JM", None)
    assert res["candidates"] == []


def test_seed_suggestions_lower_floor_and_limit(fake_objects, monkeypatch):
    captured = {}

    def fake_geocode_candidates(name, cc=None, sd=None, limit=5,
                                min_sim=0.4):
        captured["limit"] = limit
        captured["min_sim"] = min_sim
        return {"asked": name, "candidates": [
            {"osm_id": 41, "osm_type": "node", "name": "Moher Cottage",
             "lat": 52.95, "lon": -9.42, "tags": {}},
        ]}

    monkeypatch.setattr(
        EntityGeocoder, "geocode_candidates",
        staticmethod(fake_geocode_candidates),
    )
    out = EntityGeocoder.seed_suggestions("Moher Cottag", "IE", "2025_12_31")
    assert captured == {"limit": 3, "min_sim": 0.3}
    assert out[0]["name"] == "Moher Cottage"


def test_empty_name_short_circuits(fake_objects):
    res = EntityGeocoder.geocode_candidates("", "JM", "2025_12_31")
    assert res == {"asked": "", "candidates": []}
