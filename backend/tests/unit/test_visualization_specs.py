"""Tests for the deck.gl visualization data endpoints (v2 Phase 1b).

DECKGL_VISUALIZATION_INTEGRATION_PLAN_V2.md §3.2.6–3.2.7:

- ``GET /api/nca/class-centroids/`` — WKG class labels aggregated to their
  geometric centroid (PostGIS ``ST_Centroid(ST_Collect(geom))``), sized by
  entity count, partition-pruned by ``country_code`` + ``snapshot_id``.
- ``GET /api/nca/entities/`` — ``worldkg_entities_by_class`` now scopes by
  ``country_code``/``snapshot_date`` and clamps ``limit`` to [1, 5000].

Test data uses country_code 'ZZ'/'YX' and snapshot ids '9999_01_01' /
'9999_02_01' and is deleted in teardown (tests run against the live
Docker DBs per conftest.py).
"""

import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "backend.settings")
import django

django.setup()

from unittest import mock

import pytest
from django.contrib.auth.models import User
from django.contrib.gis.geos import Point
from django.urls import reverse

from semantic_search.services.worldkg_enrichment_service import (
    WorldKGEnrichmentService,
)
from worldkg_nca.models import OsmEntity
from worldkg_nca.snapshot_utils import clear_snapshot_cache
from worldkg_nca.views import enrichment as enrichment_views
from worldkg_nca.views.visualizations import humanize_class_label

pytestmark = pytest.mark.django_db(
    transaction=False, databases=["default", "vectors"],
)

TEST_CC = "ZZ"
OTHER_CC = "YX"
SNAP_A = "9999_01_01"
SNAP_B = "9999_02_01"

# Seeded rows: (osm_id, wkg_class, lon, lat, country_code, snapshot_id)
SEED = [
    # Cafe x3 in scope — centroid (2/3, 2/3), count 3 (top class)
    (9101, "wkgs:Cafe", 0.0, 0.0, TEST_CC, SNAP_A),
    (9102, "wkgs:Cafe", 2.0, 0.0, TEST_CC, SNAP_A),
    (9103, "wkgs:Cafe", 0.0, 2.0, TEST_CC, SNAP_A),
    # Restaurant x2 in scope — centroid (0, 1), count 2
    (9104, "wkgs:Restaurant", 1.0, 1.0, TEST_CC, SNAP_A),
    (9105, "wkgs:Restaurant", -1.0, 1.0, TEST_CC, SNAP_A),
    # NULL geometry — must be excluded from centroids
    (9106, "wkgs:Cafe", None, None, TEST_CC, SNAP_A),
    # NULL wkg_class — must be excluded from centroids
    (9107, None, 3.0, 3.0, TEST_CC, SNAP_A),
    # Out-of-scope rows — must not leak into ZZ/SNAP_A results
    (9108, "wkgs:Hotel", 5.0, 5.0, TEST_CC, SNAP_B),
    (9109, "wkgs:Bar", 7.0, 7.0, OTHER_CC, SNAP_A),
]


def _seed():
    for osm_id, wkg_class, lon, lat, cc, snap in SEED:
        OsmEntity.objects.using("vectors").create(
            osm_type="node",
            osm_id=osm_id,
            tags={"name": f"seed-{osm_id}"},
            wkg_class=wkg_class,
            geom=Point(lon, lat, srid=4326) if lon is not None else None,
            snapshot_id=snap,
            country_code=cc,
        )


def _cleanup():
    OsmEntity.objects.using("vectors").filter(
        country_code__in=(TEST_CC, OTHER_CC),
        snapshot_id__in=(SNAP_A, SNAP_B),
    ).delete()


@pytest.fixture(autouse=True)
def _seeded():
    clear_snapshot_cache()  # class-centroids default-snapshot path reads it
    _seed()
    yield
    _cleanup()
    clear_snapshot_cache()


@pytest.fixture
def client(client):
    """Logged-in test client (shadows pytest-django's built-in).

    PublicAuthGuardMiddleware (backend/middleware.py) turns any /api/* request
    on a non-local host (testserver) with 401 unless the session is
    authenticated, exactly like the SPA's login gate.
    """
    user = User.objects.create_user(username="viz-test-user", password="viz-test-pass")
    client.force_login(user)
    yield client
    User.objects.filter(username="viz-test-user").delete()


# ── Label humanization ───────────────────────────────────────────────────


def test_humanize_class_label():
    # Generic pluralization
    assert humanize_class_label("wkgs:Cafe") == "Cafes"
    assert humanize_class_label("wkgs:Restaurant") == "Restaurants"
    assert humanize_class_label("wkgs:BusStation") == "Bus Stations"
    assert humanize_class_label("wkgs:Bar") == "Bars"
    assert humanize_class_label("wkgs:Pharmacy") == "Pharmacies"  # y → ies
    assert humanize_class_label("wkgs:Church") == "Churches"  # sibilant → es
    # GIS key-class overrides (DECKGL v2 — the label view surfaces these)
    assert humanize_class_label("wkgs:Amenity") == "Amenities"
    assert humanize_class_label("wkgs:Natural") == "Natural Features"
    assert humanize_class_label("wkgs:Power") == "Power Lines"
    assert humanize_class_label("wkgs:Highway") == "Highways"
    assert humanize_class_label("wkgs:Waterway") == "Waterways"
    assert humanize_class_label("wkgs:Landuse") == "Land Use"
    assert humanize_class_label("wkgs:Man_made") == "Man-Made"
    assert humanize_class_label("wkgs:Public_transport") == "Public Transport"
    # Non-wkgs input passes through deterministically
    assert humanize_class_label("wkgs:") == "wkgs:"


# ── class-centroids endpoint ─────────────────────────────────────────────


def test_class_centroids_grouping_and_centroid(client):
    resp = client.get(
        reverse("nca_class_centroids"),
        {"country_code": TEST_CC, "snapshot_date": SNAP_A},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["country_code"] == TEST_CC
    assert data["snapshot_date"] == SNAP_A

    by_class = {c["wkg_class"]: c for c in data["classes"]}
    assert set(by_class) == {"wkgs:Cafe", "wkgs:Restaurant"}

    cafe = by_class["wkgs:Cafe"]
    assert cafe["label"] == "Cafes"
    assert cafe["count"] == 3
    assert cafe["centroid"][0] == pytest.approx(2 / 3, abs=1e-6)
    assert cafe["centroid"][1] == pytest.approx(2 / 3, abs=1e-6)

    restaurant = by_class["wkgs:Restaurant"]
    assert restaurant["label"] == "Restaurants"
    assert restaurant["count"] == 2
    assert restaurant["centroid"] == pytest.approx([0.0, 1.0], abs=1e-6)


def test_class_centroids_ordered_by_count_desc(client):
    resp = client.get(
        reverse("nca_class_centroids"),
        {"country_code": TEST_CC, "snapshot_date": SNAP_A},
    )
    classes = resp.json()["classes"]
    counts = [c["count"] for c in classes]
    assert counts == sorted(counts, reverse=True)
    assert classes[0]["wkg_class"] == "wkgs:Cafe"  # count 3 > 2


def test_class_centroids_scoped_to_partition(client):
    """Rows in other snapshots/countries and NULL-geom/class rows are excluded."""
    resp = client.get(
        reverse("nca_class_centroids"),
        {"country_code": TEST_CC, "snapshot_date": SNAP_A},
    )
    classes = resp.json()["classes"]
    by_class = {c["wkg_class"]: c for c in classes}
    # 9106 (NULL geom) and 9107 (NULL class) are inside scope but excluded
    assert by_class["wkgs:Cafe"]["count"] == 3  # not 4
    # 9108 (SNAP_B) and 9109 (OTHER_CC) never appear
    assert "wkgs:Hotel" not in by_class
    assert "wkgs:Bar" not in by_class


def test_class_centroids_requires_country(client):
    resp = client.get(reverse("nca_class_centroids"))
    assert resp.status_code == 400
    assert "country_code" in resp.json()["error"]


def test_class_centroids_defaults_to_latest_snapshot(client):
    """Without snapshot_date, the latest backfilled snapshot is used.

    The seed spans two snapshots (SNAP_A with Cafe/Restaurant, SNAP_B with
    Hotel), so the default must resolve to SNAP_B and return only its rows.
    """
    resp = client.get(
        reverse("nca_class_centroids"),
        {"country_code": TEST_CC},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["snapshot_date"] == SNAP_B  # '9999_02_01' > '9999_01_01'
    by_class = {c["wkg_class"]: c for c in data["classes"]}
    assert set(by_class) == {"wkgs:Hotel"}
    assert by_class["wkgs:Hotel"]["count"] == 1


# ── entities endpoint scoping + limit cap ────────────────────────────────


def _fake_service():
    """A real WorldKGEnrichmentService instance without Redis/ontology load.

    ``get_entities_by_class`` only touches ``self.ontology`` on the
    include_subclasses=True branch; the mock ontology's ``get_subclasses``
    returns [] by default, so the class filter stays the requested class.
    """
    service = WorldKGEnrichmentService.__new__(WorldKGEnrichmentService)
    service.ontology = mock.Mock()
    service.ontology.get_subclasses.return_value = []
    return service


def test_entities_endpoint_scoping(client):
    """country_code/snapshot_date params reach the query and filter rows."""
    with mock.patch.object(
        enrichment_views, "get_worldkg_enrichment_service", return_value=_fake_service()
    ):
        resp = client.get(
            reverse("nca_query_entities"),
            {
                "class": "wkgs:Cafe",
                "include_subclasses": "false",
                "country_code": TEST_CC,
                "snapshot_date": SNAP_A,
            },
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["country_code"] == TEST_CC
    assert data["snapshot_date"] == SNAP_A
    assert data["total"] == 4  # 9101–9103 + 9106 (NULL geom is still a row)
    ids = {e["osm_id"] for e in data["entities"]}
    assert ids == {9101, 9102, 9103, 9106}


def test_entities_endpoint_limit_clamped(client):
    """limit is clamped to [1, 5000] — 0 → 1, huge → 5000 (no 500 error)."""
    with mock.patch.object(
        enrichment_views, "get_worldkg_enrichment_service", return_value=_fake_service()
    ):
        small = client.get(
            reverse("nca_query_entities"),
            {
                "class": "wkgs:Cafe",
                "include_subclasses": "false",
                "limit": "1",
                "country_code": TEST_CC,
                "snapshot_date": SNAP_A,
            },
        )
        zero = client.get(
            reverse("nca_query_entities"),
            {
                "class": "wkgs:Cafe",
                "include_subclasses": "false",
                "limit": "0",
                "country_code": TEST_CC,
                "snapshot_date": SNAP_A,
            },
        )
        huge = client.get(
            reverse("nca_query_entities"),
            {
                "class": "wkgs:Cafe",
                "include_subclasses": "false",
                "limit": "99999",
                "country_code": TEST_CC,
                "snapshot_date": SNAP_A,
            },
        )
    assert small.status_code == 200 and small.json()["total"] == 1
    assert zero.status_code == 200 and zero.json()["total"] == 1  # clamped up
    assert huge.status_code == 200 and huge.json()["total"] == 4  # clamped, not error


def test_entities_endpoint_requires_class(client):
    resp = client.get(reverse("nca_query_entities"))
    assert resp.status_code == 400


# ── service method (subclass branch + scoping) ───────────────────────────


def test_get_entities_by_class_scoping_with_subclasses():
    """include_subclasses=True expands via ontology, then scopes by partition."""
    service = _fake_service()
    service.ontology.get_subclasses.return_value = ["wkgs:Bar"]
    # Seed a subclass row inside scope (Bar lives in OTHER_CC/SNAP_A too)
    OsmEntity.objects.using("vectors").create(
        osm_type="node",
        osm_id=9201,
        tags={"name": "seed-bar"},
        wkg_class="wkgs:Bar",
        geom=Point(7.0, 7.0, srid=4326),
        snapshot_id=SNAP_A,
        country_code=TEST_CC,
    )
    try:
        entities = service.get_entities_by_class(
            "wkgs:Cafe",
            include_subclasses=True,
            limit=100,
            country_code=TEST_CC,
            snapshot_date=SNAP_A,
        )
    finally:
        OsmEntity.objects.using("vectors").filter(osm_id=9201).delete()

    ids = {e.osm_id for e in entities}
    # Cafe (9101–9103, + 9106 with NULL geom) + Bar (9201) in scope;
    # never Hotel (9108) or OTHER_CC Bar (9109)
    assert ids == {9101, 9102, 9103, 9106, 9201}
