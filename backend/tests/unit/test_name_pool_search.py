"""Unit tests for the name-first pool tier (SpatialSearchMixin).

Regression: "Juici Patties within 150km of KFC" returned 1 result because
the FastText pool is built from tag embeddings and is name-blind for
out-of-vocabulary brands (pool_size: 1). Fix: when the OBJECT is a proper
name, merge the fuzzy name tier (trigram on name_romanized) into the pool
before the radius filter. The merge must dedupe by (osm_type, osm_id) so
an entity matched by both tiers appears once.
"""

import pytest

from semantic_search.services.query_executor_service.spatial_search import (
    SpatialSearchMixin,
)


def _r(osm_type, osm_id, name, **extra):
    r = {"osm_type": osm_type, "osm_id": osm_id, "name": name}
    r.update(extra)
    return r


class TestMergePools:
    """_merge_pools dedupes by (osm_type, osm_id), first occurrence wins."""

    def test_dedupes_overlapping_pools(self):
        name_pool = [
            _r("node", 1, "Juici Patties"),
            _r("node", 2, "Juici Patties"),
        ]
        fasttext_pool = [
            _r("node", 2, "Juici Patties", embedding_distance=0.48),
            _r("node", 3, "Juice patties", embedding_distance=0.49),
        ]
        merged = SpatialSearchMixin._merge_pools(name_pool, fasttext_pool)
        assert len(merged) == 3
        ids = {(r["osm_type"], r["osm_id"]) for r in merged}
        assert ids == {("node", 1), ("node", 2), ("node", 3)}

    def test_first_occurrence_wins(self):
        """The name tier entry is kept when both tiers matched the entity."""
        name_pool = [
            _r("node", 7, "Island Grill"),
        ]
        fasttext_pool = [
            _r("node", 7, "Island Grill", embedding_distance=0.42),
        ]
        merged = SpatialSearchMixin._merge_pools(name_pool, fasttext_pool)
        assert len(merged) == 1
        assert "embedding_distance" not in merged[0]

    def test_empty_pools(self):
        assert SpatialSearchMixin._merge_pools() == []
        assert SpatialSearchMixin._merge_pools(None, []) == []

    def test_osm_type_partition(self):
        """node and relation with the same osm_id are distinct entities."""
        merged = SpatialSearchMixin._merge_pools(
            [_r("node", 5, "X")],
            [_r("relation", 5, "X")],
        )
        assert len(merged) == 2
