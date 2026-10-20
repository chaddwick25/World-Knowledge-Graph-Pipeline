"""Regression tests for augmented-summary stats correctness.

Two bugs fixed 2026-09-09 (CV sanity-check session):

1. ``total_accepted`` double-counted links whose head entity fell inside
   overlapping subgraph bboxes — per-subgraph ``COUNT`` queries summed to
   MORE than the real country+snapshot link count (CV 2024: 1,018 vs 942
   real links). Fix: links are assigned to exactly one subgraph (first
   bbox match), with a residual country-level group for out-of-bbox links,
   so per-subgraph counts partition the total.

2. ``_count_entities`` ignored the country — ``WHERE snapshot_id = ...``
   leaked every other country's entities on a shared snapshot date into
   per-country stats (CV 2025 showed 40.3M entities instead of 212,009).
   Fix: counts are scoped by ``country_code`` too.
"""

import pytest
from django.contrib.gis.geos import Point

pytestmark = pytest.mark.django_db(transaction=False, databases=["default", "vectors"])


@pytest.fixture
def seed_summary_data(django_db_setup, django_db_blocker):
    """Seed two overlapping subgraphs + links for synthetic country TZ.

    Geometry layout (lat/lon):
        e101 @ (1, 1)     → inside bbox A only (boundary of B)
        e102 @ (1.5, 1.5) → inside BOTH bboxes (the double-count trap)
        e103 @ (2.5, 2.5) → inside bbox B only
        e104 @ (9, 9)     → outside every subgraph bbox (residual group)
    Country TX has one entity on the SAME snapshot (the leak trap).
    """
    from core.models import CountryPipelineProfile, SubgraphProfile
    from igea.models import SpatialTripletScore, SpatialTripletScoreRejected
    from worldkg_nca.models import OsmEntity

    with django_db_blocker.unblock():
        for cc in ("TZ", "TX"):
            OsmEntity.objects.using("vectors").filter(country_code=cc).delete()
            SpatialTripletScore.objects.filter(country_name=cc).delete()
            SpatialTripletScoreRejected.objects.filter(country_name=cc).delete()
            SubgraphProfile.objects.filter(country_profile__iso2=cc).delete()
            CountryPipelineProfile.objects.filter(iso2=cc).delete()

        prof = CountryPipelineProfile.objects.create(
            iso2="TZ",
            iso3="TZZ",
            canonical_name="Testzania",
            canonical_slug="testzania",
            embedding_slug="testzania",
            embedding_root_path="africa/testzania",
            continent_name="africa",
            has_subgraphs=True,
        )
        SubgraphProfile.objects.create(
            country_profile=prof,
            slug="tz_a",
            name="TZ A",
            bbox_min_lat=0.0, bbox_max_lat=2.0,
            bbox_min_lon=0.0, bbox_max_lon=2.0,
        )
        SubgraphProfile.objects.create(
            country_profile=prof,
            slug="tz_b",
            name="TZ B",
            bbox_min_lat=1.0, bbox_max_lat=3.0,
            bbox_min_lon=1.0, bbox_max_lon=3.0,
        )

        for osm_id, lat, lon in [
            (101, 1.0, 1.0),
            (102, 1.5, 1.5),
            (103, 2.5, 2.5),
            (104, 9.0, 9.0),
        ]:
            OsmEntity.objects.using("vectors").create(
                osm_type="node",
                osm_id=osm_id,
                snapshot_id="2025_12_31",
                country_code="TZ",
                tags={"name": f"e{osm_id}"},
                geom=Point(lon, lat, srid=4326),
            )

        # Leak trap: another country on the same snapshot date.
        OsmEntity.objects.using("vectors").create(
            osm_type="node",
            osm_id=201,
            snapshot_id="2025_12_31",
            country_code="TX",
            tags={"name": "e201"},
            geom=Point(9.0, 9.0, srid=4326),
        )

        # One accepted link per head entity.
        for i, head in enumerate([101, 102, 103, 104]):
            SpatialTripletScore.objects.create(
                head_osm_type="node",
                head_osm_id=head,
                tail_osm_type="node",
                tail_osm_id=9000 + i,
                relation="within_50m_of",
                geo_score=0.5, name_score=0.5, topo_score=0.5,
                unnormalized_score=1.5, normalized_score=0.5,
                geohash_precision=4,
                snapshot_id="2025_12_31",
                country_name="TZ",
                predicted=True,
            )

        # One rejected link with head in the overlap (rejected double-count trap).
        SpatialTripletScoreRejected.objects.create(
            head_osm_type="node",
            head_osm_id=102,
            tail_osm_type="node",
            tail_osm_id=9001,
            relation="within_50m_of",
            geo_score=0.1, name_score=0.1, topo_score=0.1,
            unnormalized_score=0.3, normalized_score=0.1,
            geohash_precision=4,
            snapshot_id="2025_12_31",
            country_name="TZ",
        )


def test_total_accepted_partitions_without_double_counting(seed_summary_data):
    """Per-subgraph sums must equal the distinct link counts, not exceed them."""
    from api.services.augmented_data_service import AugmentedDataService

    summary = AugmentedDataService().get_summary("Testzania", "2025_12_31")

    # 4 distinct accepted links + 1 rejected — regardless of which bbox
    # wins the first-match assignment, the partition must be exact.
    assert summary.total_accepted == 4
    assert summary.total_rejected == 1
    assert sum(g["accepted_count"] for g in summary.subgraph_groups) == 4
    assert sum(g["rejected_count"] for g in summary.subgraph_groups) == 1

    # The out-of-bbox link (head 104) must land in a residual
    # country-level group so the total covers the full link set.
    residual = [g for g in summary.subgraph_groups if g["subgraph_slug"] == "country-level"]
    assert len(residual) == 1
    assert residual[0]["accepted_count"] == 1


def test_count_entities_is_country_scoped(seed_summary_data):
    """Shared snapshots must not leak other countries' entities."""
    from api.services.augmented_data_service import AugmentedDataService

    counts = AugmentedDataService()._count_entities("Testzania", "TZ", "2025_12_31")
    # TZ has 4 entities; TX has 1 on the same snapshot — the old code
    # would have returned total=5 (cross-country leak).
    assert counts == {"total": 4, "with_wkg_class": 0}


def test_count_entities_without_snapshot_is_country_scoped(seed_summary_data):
    """The no-snapshot branch must also be country-scoped."""
    from api.services.augmented_data_service import AugmentedDataService

    counts = AugmentedDataService()._count_entities("Testzania", "TZ")
    assert counts["total"] == 4
