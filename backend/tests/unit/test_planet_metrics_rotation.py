"""Planet rotation: the new metrics row must take over the shared hierarchy.

Regression cover for the 2026-11-12 outage. ``generate_planetary_metrics``
does ``update_or_create(pbf_file=...)``, so a rotated planet produces a *new*
``PlanetaryMetrics`` row. ``create_planet_hierarchy`` then assigns the shared
``slug='planet'`` hierarchy to it, but ``osm_wikidata_hierarchy`` is a
``OneToOneField`` and the superseded planet's row still owned it:

    IntegrityError: duplicate key value violates unique constraint
    "planetary_metrics_osm_wikidata_hierarchy_id_key"

``register_planet`` died there, ``finalize`` refused to stamp COMPLETED, and
``/api/system/status/`` reported ``ready: false`` for the whole site.
"""

import uuid

import pytest

from core.models import OSMWikiDataHierarchy, PbfFile, PlanetaryMetrics
from core.services.planet_init.planet_initialization_service import (
    PlanetInitializationService,
)

pytestmark = pytest.mark.django_db(transaction=False, databases=["default"])


@pytest.fixture
def made():
    """Track created rows so the reused test DB stays clean."""
    created = []
    yield created
    for obj in reversed(created):
        obj.delete()


def _service():
    return PlanetInitializationService(
        {"stages": {"planet_initialization": {"parameters": {}}}}
    )


def _pbf(made, tag):
    pbf = PbfFile.objects.create(
        pbf_file_type=PbfFile.PbfType.PLANET,
        path=f"/tmp/{tag}-{uuid.uuid4().hex}.osh.pbf",
    )
    made.append(pbf)
    return pbf


def _metrics(made, pbf):
    metrics = PlanetaryMetrics.objects.create(
        pbf_file=pbf,
        node_count=0,
        way_count=0,
        relation_count=0,
        bbox_min_lat=0.0,
        bbox_max_lat=0.0,
        bbox_min_lon=0.0,
        bbox_max_lon=0.0,
        area_km2=0.0,
        unique_tag_keys=0,
        unique_tag_values=0,
        top_tag_keys=[],
        orphan_nodes_ratio=0.0,
        incomplete_ways_ratio=0.0,
        estimated_processing_time_seconds=0,
        recommended_batch_size=0,
        recommended_worker_count=0,
    )
    made.append(metrics)
    return metrics


def test_rotated_planet_takes_over_the_planet_hierarchy(made):
    old_pbf = _pbf(made, "old")
    new_pbf = _pbf(made, "new")
    old_metrics = _metrics(made, old_pbf)
    new_metrics = _metrics(made, new_pbf)

    service = _service()

    hierarchy = service.create_planet_hierarchy(old_pbf, old_metrics)
    made.append(hierarchy)
    old_metrics.refresh_from_db()
    assert old_metrics.osm_wikidata_hierarchy_id == hierarchy.id

    # The rotation: this used to raise the UniqueViolation.
    service.create_planet_hierarchy(new_pbf, new_metrics)

    old_metrics.refresh_from_db()
    new_metrics.refresh_from_db()
    assert new_metrics.osm_wikidata_hierarchy_id == hierarchy.id
    assert old_metrics.osm_wikidata_hierarchy_id is None
    assert (
        PlanetaryMetrics.objects.filter(osm_wikidata_hierarchy=hierarchy).count() == 1
    )


def test_reassigning_the_same_metrics_row_is_idempotent(made):
    pbf = _pbf(made, "same")
    metrics = _metrics(made, pbf)

    service = _service()
    hierarchy = service.create_planet_hierarchy(pbf, metrics)
    made.append(hierarchy)
    service.create_planet_hierarchy(pbf, metrics)

    metrics.refresh_from_db()
    assert metrics.osm_wikidata_hierarchy_id == hierarchy.id


def test_planet_hierarchy_is_reused_not_duplicated(made):
    first_pbf = _pbf(made, "first")
    second_pbf = _pbf(made, "second")
    first_metrics = _metrics(made, first_pbf)
    second_metrics = _metrics(made, second_pbf)

    service = _service()
    first_hierarchy = service.create_planet_hierarchy(first_pbf, first_metrics)
    made.append(first_hierarchy)
    second_hierarchy = service.create_planet_hierarchy(second_pbf, second_metrics)

    assert second_hierarchy.pk == first_hierarchy.pk
    assert OSMWikiDataHierarchy.objects.filter(slug="planet").count() == 1
