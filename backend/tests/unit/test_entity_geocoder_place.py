"""Unit tests for EntityGeocoder's place-preference ranking.

The Belfast anchor regression (2026-09-19): an exact-name match on a
bicycle destination sign ("Belfast" the NCN 93 signpost) beat the city,
so every Belfast-anchored spatial question radiated from a countryside
signpost. The geocoder now picks among top candidates by
(has_coordinates, place_score) instead of returning the first row.

Pure-Python tests — no DB required (fake entities stand in for
OsmEntity rows).
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
                 osm_type="node", wkg_class=None, country_code="IE"):
        self.tags = dict(tags or {})
        self.tags.setdefault("name", name)
        self.geom = geom
        self.osm_id = osm_id
        self.osm_type = osm_type
        self.wkg_class = wkg_class
        self.country_code = country_code


def _place(name, **tags):
    return _FakeEntity(name, tags=tags, geom=_FakeGeom(-5.9, 54.65))


class TestPlaceScore:
    def test_settlement_scores_high(self):
        assert EntityGeocoder._place_score({"place": "city"}) == 2
        assert EntityGeocoder._place_score(
            {"place": "city", "capital": "yes", "admin_level": "2"},
        ) == 3  # place=city (+2) + admin-ness (+1)
        assert EntityGeocoder._place_score({"place": "village"}) == 2

    def test_marker_scores_low(self):
        assert EntityGeocoder._place_score(
            {"type": "destination_sign", "destination": "Belfast"},
        ) == -2
        assert EntityGeocoder._place_score({"type": "guidepost"}) == -2

    def test_plain_poi_is_zero(self):
        assert EntityGeocoder._place_score({"amenity": "pub", "name": "X"}) == 0
        assert EntityGeocoder._place_score(None) == 0


class TestPick:
    def test_city_beats_same_name_signpost(self):
        """The regression: both named 'Belfast', both geocodable — the
        city must win the tie."""
        signpost = _place(
            "Belfast", type="destination_sign", destination="Belfast",
            distance="5.5mi",
        )
        city = _place(
            "Belfast", place="city", capital="yes", admin_level="2",
        )
        picked = EntityGeocoder._pick([signpost, city])
        assert picked is city

    def test_signpost_alone_still_matches(self):
        """Fallback preserved: when no place entity exists, the marker
        still resolves (an exact-name match beats nothing)."""
        signpost = _place(
            "Belfast", type="destination_sign", destination="Belfast",
        )
        assert EntityGeocoder._pick([signpost]) is signpost

    def test_coordinates_beat_place_score(self):
        """A coordinate-less settlement must not win over a geocodable
        marker — the executor needs lat/lon, not a 'better' tag set."""
        city_no_geom = _FakeEntity("Belfast", tags={"place": "city"})
        signpost = _place(
            "Belfast", type="destination_sign", destination="Belfast",
        )
        assert EntityGeocoder._pick([city_no_geom, signpost]) is signpost

    def test_empty_and_none(self):
        assert EntityGeocoder._pick([]) is None
        assert EntityGeocoder._pick(None) is None
