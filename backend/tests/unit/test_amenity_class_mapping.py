"""Tests for the DB-driven amenity → WorldKG class mapping (rule 6.2).

Covers GRAPH_SPECTRAL_FEEDBACK_HARDENING_PLAN Phase 5:
- ``compute_amenity_class_mappings`` command: data tier aggregates
  OsmEntity amenity values into (amenity → most common wkg_class,
  support_count); idempotent delete-then-insert.
- Ontology tier skips gracefully when the TTL is absent; precedence: data
  wins, ontology fills gaps.
- ``FactorResolutionService.amenity_class`` resolves mapped values and
  returns None for unknowns (runtime falls back to PostGIS as today).

Test data uses country_code='ZZ' and snapshot '9999_01_01' and is deleted
in teardown (tests run against the live Docker DBs per conftest.py).
"""

import pytest

from semantic_search.services.factor_resolution_service import (
    FactorResolutionService,
)
from worldkg_nca.models import AmenityClassMapping, OsmEntity

pytestmark = pytest.mark.django_db(
    transaction=False, databases=["vectors"],
)

TEST_CC = "ZZ"
SNAP = "9999_01_01"


def _seed_entities():
    """Insert OsmEntity rows carrying amenity tags + wkg_class."""
    rows = [
        # (osm_id, amenity, wkg_class)
        (9001, "cafe", "wkgs:Cafe"),
        (9002, "cafe", "wkgs:Cafe"),
        (9003, "cafe", "wkgs:Restaurant"),  # minority class — ignored
        (9004, "bar", "wkgs:Bar"),
        (9005, "fuel", "wkgs:FuelStation"),
    ]
    for osm_id, amenity, wkg_class in rows:
        OsmEntity.objects.using("vectors").create(
            osm_type="node",
            osm_id=osm_id,
            tags={"amenity": amenity, "name": f"entity-{osm_id}"},
            snapshot_id=SNAP,
            country_code=TEST_CC,
            wkg_class=wkg_class,
        )


def _cleanup():
    AmenityClassMapping.objects.using("vectors").filter(
        source__in=("data", "ontology"),
    ).delete()
    OsmEntity.objects.using("vectors").filter(
        country_code=TEST_CC,
        osm_id__in=(9001, 9002, 9003, 9004, 9005),
    ).delete()


@pytest.fixture
def seeded_amenity_entities():
    _cleanup()
    _seed_entities()
    yield
    _cleanup()


def _patch_ttl_missing(monkeypatch):
    """Point WORLDKG_ONTOLOGY_PATH at a non-existent file so the ontology
    tier is skipped (test env has no TTL)."""
    from pathlib import Path

    from django.conf import settings

    monkeypatch.setattr(
        settings, "WORLDKG_ONTOLOGY_PATH",
        str(Path("/nonexistent/WorldKG_Ontolgy.ttl")),
    )


def test_command_populates_data_tier(seeded_amenity_entities, monkeypatch):
    """Data tier: most common wkg_class per amenity with support count."""
    _patch_ttl_missing(monkeypatch)
    from django.core.management import call_command

    call_command("compute_amenity_class_mappings")

    cafe = AmenityClassMapping.objects.using("vectors").get(
        amenity_text="cafe",
    )
    assert cafe.wkg_class == "wkgs:Cafe"   # majority class wins
    assert cafe.support_count == 2
    assert cafe.source == "data"

    bar = AmenityClassMapping.objects.using("vectors").get(
        amenity_text="bar",
    )
    assert bar.wkg_class == "wkgs:Bar"
    assert bar.support_count == 1

    fuel = AmenityClassMapping.objects.using("vectors").get(
        amenity_text="fuel",
    )
    assert fuel.wkg_class == "wkgs:FuelStation"


def test_command_idempotent(seeded_amenity_entities, monkeypatch):
    """Delete-then-insert: running twice yields the same row set."""
    _patch_ttl_missing(monkeypatch)
    from django.core.management import call_command

    call_command("compute_amenity_class_mappings")
    first = list(
        AmenityClassMapping.objects.using("vectors")
        .values_list("amenity_text", "wkg_class", "support_count")
        .order_by("amenity_text")
    )
    call_command("compute_amenity_class_mappings")
    second = list(
        AmenityClassMapping.objects.using("vectors")
        .values_list("amenity_text", "wkg_class", "support_count")
        .order_by("amenity_text")
    )
    assert first == second
    assert len(first) == 3


def test_resolver_mapped_and_unknown(seeded_amenity_entities, monkeypatch):
    """amenity_class resolves mapped values; None for unknowns."""
    _patch_ttl_missing(monkeypatch)
    from django.core.management import call_command

    call_command("compute_amenity_class_mappings")

    frs = FactorResolutionService()
    assert frs.amenity_class("cafe") == "wkgs:Cafe"
    assert frs.amenity_class("CAFE") == "wkgs:Cafe"      # case-insensitive
    assert frs.amenity_class("nonexistent") is None      # → PostGIS fallback


def test_command_without_data_is_empty(monkeypatch):
    """No OsmEntity rows + no TTL → zero mappings, no crash."""
    _cleanup()
    try:
        _patch_ttl_missing(monkeypatch)
        from django.core.management import call_command

        call_command("compute_amenity_class_mappings")
        assert AmenityClassMapping.objects.using("vectors").count() == 0
    finally:
        _cleanup()
