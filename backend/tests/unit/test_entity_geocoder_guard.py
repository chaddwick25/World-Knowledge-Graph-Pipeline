"""Tests for EntityGeocoder's truncated-span fragment guard.

Covers the false-positive class behind the Moher Cottage regression: a
short entity span ("Cliffs") matching a much longer name ("Cliffs of
Howth") via pg_trgm at low similarity. The guard rejects candidates
whose name is > 1.6x the query length at similarity < 0.6, while
preserving the equal-length typo case ("Shannon Bells" → "Shandon
Bells", sim 0.65).

Test data uses the live-Docker test DBs (per conftest.py) with
country_code='BZ', snapshot '2025_12_31' and osm_ids in the 7xxxxx
range, deleted in teardown. Requires pg_trgm on test_vector_db
(migration 0017; see AGENTS.md).

Run inside Docker:
  docker compose exec backend python -m pytest tests/unit/test_entity_geocoder_guard.py -v --reuse-db
"""

import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "backend.settings")
import django

django.setup()

import pytest

pytestmark = pytest.mark.django_db(
    transaction=False, databases=["default", "vectors"],
)

SNAP = "2025_12_31"

_ENTITY_ROWS = [
    ("Cliffs of Howth", {"tourism": "viewpoint"}, 53.3880, -6.0650, 700001),
    ("Cliffs of Moher", {"natural": "cliff"}, 52.9704, -9.4256, 700002),
    ("Shandon Bells", {"amenity": "place_of_worship"}, 51.9017, -8.4780, 700003),
    ("Moher Cottage", {"amenity": "cafe"}, 52.9530, -9.4211, 700004),
    ("Cliff Coast Coffee", {"amenity": "cafe"}, 53.0126, -9.3839, 700005),
    ("Real Cafe Near Shandon", {"amenity": "cafe"}, 51.9035, -8.4760, 700006),
]
_SEEDED_OSM_IDS = [row[4] for row in _ENTITY_ROWS]

# A coordinate-less way encoded as POINT(NaN NaN) — NOT NULL, so the
# spatial search's exclude(geom__isnull=True) misses it, and ST_Distance
# on NaN returns 0 (the "(0m away)" regression).
NAN_OSM_ID = 700099


@pytest.fixture(scope="module")
def seed_entities(django_db_setup, django_db_blocker):
    """Seed the test vectors DB with the guard-scenario entity set.

    Depends on ``django_db_setup`` so writes go to the TEST databases
    (module-scoped fixture without it would hit the real DBs — see the
    data-loss hazard note in AGENTS.md).
    """
    from django.contrib.gis.geos import Point
    from worldkg_nca.models import OsmEntity

    with django_db_blocker.unblock():
        OsmEntity.objects.using("vectors").filter(
            osm_id__in=_SEEDED_OSM_IDS
        ).delete()
        for name, tags, lat, lon, osm_id in _ENTITY_ROWS:
            full_tags = dict(tags)
            full_tags["name"] = name
            OsmEntity.objects.using("vectors").create(
                osm_type="node",
                osm_id=osm_id,
                snapshot_id=SNAP,
                country_code="BZ",
                tags=full_tags,
                # The geocoder's pg_trgm tier searches name_romanized
                # (populated by romanize_names in production) — identity
                # romanization for these English names.
                name_romanized=name.lower(),
                geom=Point(lon, lat, srid=4326),
            )
        # Coordinate-less way: POINT(NaN NaN) can't be built via the GEOS
        # wrapper — raw SQL, inside the unblock (writes must hit the TEST
        # DBs, never the real ones).
        import json as _json
        from django.db import connections as _connections
        with _connections["vectors"].cursor() as cur:
            cur.execute(
                "INSERT INTO semantic_search_osmentity "
                "(osm_type, osm_id, snapshot_id, country_code, tags, "
                " name_romanized, geom, gv_tags_version, gv_nle_trained, "
                " created_at, updated_at) "
                "VALUES ('way', %s, %s, 'BZ', %s, 'nan cafe', "
                " ST_GeomFromText('POINT(NaN NaN)', 4326), '0', false, "
                " NOW(), NOW())",
                [NAN_OSM_ID, SNAP,
                 _json.dumps({"name": "NaN Cafe", "amenity": "cafe"})],
            )
    yield
    with django_db_blocker.unblock():
        OsmEntity.objects.using("vectors").filter(
            osm_id__in=_SEEDED_OSM_IDS + [NAN_OSM_ID]
        ).delete()


class TestFragmentGuard:
    def test_fragment_not_matched_to_longer_name(self, seed_entities):
        """'Cliffs' must NOT resolve to 'Cliffs of Howth' (the regression)."""
        from semantic_search.services.entity_geocoder import EntityGeocoder
        result = EntityGeocoder.geocode("Cliffs", snapshot_date=SNAP)
        assert result is None or result["name"] != "Cliffs of Howth", (
            "truncated span matched a longer name"
        )

    def test_full_name_still_resolves(self, seed_entities):
        """The correct full name still resolves exactly."""
        from semantic_search.services.entity_geocoder import EntityGeocoder
        result = EntityGeocoder.geocode("Cliffs of Moher", snapshot_date=SNAP)
        assert result is not None
        assert result["name"] == "Cliffs of Moher"

    def test_equal_length_typo_still_matches(self, seed_entities):
        """Existing behavior preserved: 'Shannon Bells' → 'Shandon Bells'."""
        from semantic_search.services.entity_geocoder import EntityGeocoder
        result = EntityGeocoder.geocode("Shannon Bells", snapshot_date=SNAP)
        assert result is not None
        assert result["name"] == "Shandon Bells"

    def test_exact_anchor_still_resolves(self, seed_entities):
        """Moher Cottage resolves to itself (the compare-closer anchor)."""
        from semantic_search.services.entity_geocoder import EntityGeocoder
        result = EntityGeocoder.geocode("Moher Cottage", snapshot_date=SNAP)
        assert result is not None
        assert result["name"] == "Moher Cottage"

    def test_spatial_search_excludes_nan_geom(self, seed_entities):
        """Coordinate-less POINT(NaN NaN) ways must not rank as nearest
        (ST_Distance returns 0 on NaN — the '(0m away)' regression)."""
        from django.contrib.gis.geos import Point
        from semantic_search.services.query_executor_service import (
            QueryExecutorService,
        )
        anchor = Point(-8.4761616, 51.9032626, srid=4326)  # Shandon Bells
        results = QueryExecutorService._search_by_amenity_spatial(
            "cafe", None, SNAP, anchor_point=anchor, top_k=10,
        )
        names = [r.get("name") for r in results]
        assert "NaN Cafe" not in names, "NaN-geom entity ranked in spatial search"
        assert any(n == "Real Cafe Near Shandon" for n in names)
        assert all(
            (r.get("distance_m") or 0) > 0 for r in results
        ), "all distances must be real (> 0)"

    def test_long_partial_name_still_matches(self, seed_entities):
        """Legitimate partial-name queries (>= 8 chars) are unaffected:
        'Shandon Bell' → 'Shandon Bells' via the contains tier."""
        from semantic_search.services.entity_geocoder import EntityGeocoder
        result = EntityGeocoder.geocode("Shandon Bell", snapshot_date=SNAP)
        assert result is not None
        assert result["name"] == "Shandon Bells"
