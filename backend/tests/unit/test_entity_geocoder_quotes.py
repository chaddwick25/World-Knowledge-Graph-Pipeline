"""Tests for EntityGeocoder quote normalization.

OSM names and user input both carry curly apostrophes (U+2018/U+2019 -
keyboards, autocorrect), and every exact/ILIKE tier fails when one side
is curly ("Ice N' Beans" not found in the dataset, 2026-10-19). The
fix normalizes the query AND the stored name in SQL.

Two layers:
- pure-Python: `_normalize_quotes` maps every curly quote to straight.
- DB-backed (live-Docker test DBs, same pattern as
  test_entity_geocoder_guard.py): a straight-apostrophe row resolves
  when the query carries the curly form, via the quote-normalized
  exact tier (name_romanized is NULL so the trigram tier is skipped
  and the SQL normalization is what is under test).
"""

import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "backend.settings")
import django

django.setup()

import pytest

from semantic_search.services.entity_geocoder import EntityGeocoder

SNAP = "2025_12_31"
OSM_ID = 700042  # 7xxxxx range, no collision with the guard suite


def test_normalize_quotes_maps_curly_to_straight():
    assert EntityGeocoder._normalize_quotes(
        "\u2018Ice N\u2019 Beans\u201b"
    ) == "'Ice N' Beans'"
    assert EntityGeocoder._normalize_quotes(
        "\u201chello\u201d world"
    ) == "\"hello\" world"
    assert EntityGeocoder._normalize_quotes(None) is None
    assert EntityGeocoder._normalize_quotes("no quotes") == "no quotes"


@pytest.mark.django_db(transaction=False, databases=["default", "vectors"])
def test_geocode_curly_query_matches_straight_stored(
    django_db_setup, django_db_blocker
):
    from django.contrib.gis.geos import Point
    from worldkg_nca.models import OsmEntity

    with django_db_blocker.unblock():
        OsmEntity.objects.using("vectors").filter(osm_id=OSM_ID).delete()
        OsmEntity.objects.using("vectors").create(
            osm_type="node",
            osm_id=OSM_ID,
            snapshot_id=SNAP,
            country_code="BZ",
            tags={"name": "Ice N' Beans", "amenity": "cafe"},
            geom=Point(-88.5, 17.25),
            name_romanized=None,  # trigram tier skipped; tiers 2/3 tested
        )
        try:
            result = EntityGeocoder.geocode(
                "Ice N\u2019 Beans", "BZ", SNAP,
            )
            assert result is not None
            assert result["osm_id"] == OSM_ID
        finally:
            OsmEntity.objects.using("vectors").filter(
                osm_id=OSM_ID
            ).delete()