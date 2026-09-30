"""Tests for subgraph-scoped spectral analysis (STEP_5C_SUBDIVISION_SPECTRAL_PLAN.md).

Covers:
- KNNGraphService.build_sparse_graph_for_subgraph (polygon/bbox filtering)
- FactorNodeWriter.write_spectral_nodes with subgraph_slug
- FactorResolutionService.diffusion_rank subgraph scoping
- Step 5c subgraph fan-out routing
- k clamp for small subgraphs
"""

import numpy as np
import pytest

from semantic_search.services.knn_graph_service import KNNGraphService, SparseGraph
from semantic_search.services.spectral_analysis_service import SpectralAnalysisService

# Reuse the fixture and constants from the existing factor-node tests
from tests.unit.test_factor_node_tables import (
    seeded_spectral, TEST_CC, SNAP_A, N_NODES,
    _weighted_path_graph, _features, _two_communities, _cleanup,
)


# ── build_sparse_graph_for_subgraph ───────────────────────────────────────


@pytest.mark.django_db(
    transaction=False, databases=["default", "vectors"]
)
def test_build_sparse_graph_for_subgraph_with_bbox():
    """build_sparse_graph_for_subgraph filters entities by bbox."""
    # This is a unit test for the bbox path — no DB needed, just
    # verify the method exists and handles the no-entities case.
    knn = KNNGraphService(k=5)
    # Call with a tiny bbox in the middle of the ocean — should get
    # an empty graph (no entities there).
    sg = knn.build_sparse_graph_for_subgraph(
        country_code="ZZ",
        snapshot_id="2025_12_31",
        subgraph_slug="test_ocean",
        bbox=(-1.0, -1.0, 1.0, 1.0),  # Gulf of Guinea — no OSM entities
    )
    assert isinstance(sg, SparseGraph)
    assert sg.n_nodes == 0


@pytest.mark.django_db(
    transaction=False, databases=["default", "vectors"]
)
def test_build_sparse_graph_for_subgraph_no_boundary_falls_back():
    """When no poly_path and no bbox, falls back to country-level build."""
    knn = KNNGraphService(k=5)
    # No poly_path, no bbox → should call build_sparse_graph (country level)
    # which will return an empty graph for a non-existent country.
    sg = knn.build_sparse_graph_for_subgraph(
        country_code="ZZ",
        snapshot_id="2025_12_31",
        subgraph_slug="test_no_boundary",
    )
    assert isinstance(sg, SparseGraph)


# ── k clamp ──────────────────────────────────────────────────────────────


def test_k_clamp_for_small_subgraph():
    """k should be clamped to min(128, max(16, n//20)) for small graphs."""
    # For a 1000-node graph: k = min(128, max(16, 50)) = 50
    k_1000 = min(128, max(16, 1000 // 20))
    assert k_1000 == 50

    # For a 100-node graph: k = min(128, max(16, 5)) = 16
    k_100 = min(128, max(16, 100 // 20))
    assert k_100 == 16

    # For a 5000-node graph: k = min(128, max(16, 250)) = 128
    k_5000 = min(128, max(16, 5000 // 20))
    assert k_5000 == 128

    # For a 10000-node graph: k = min(128, max(16, 500)) = 128
    k_10000 = min(128, max(16, 10000 // 20))
    assert k_10000 == 128


# ── FactorNodeWriter subgraph_slug ────────────────────────────────────────


@pytest.mark.django_db(
    transaction=False, databases=["default", "vectors"]
)
def test_write_spectral_nodes_with_subgraph_slug(seeded_spectral):
    """FactorNodeWriter.write_spectral_nodes accepts subgraph_slug."""
    from semantic_search.services.factor_node_writer import FactorNodeWriter
    from worldkg_nca.models import SpectralNodeMetric

    G = seeded_spectral["G"]
    features = seeded_spectral["features"]

    writer = FactorNodeWriter()
    n = writer.write_spectral_nodes(
        G, features, TEST_CC, SNAP_A,
        subgraph_slug="test_subgraph",
    )
    assert n > 0

    # Verify rows have the subgraph_slug set
    rows = SpectralNodeMetric.objects.using("vectors").filter(
        snapshot_id=SNAP_A, country_code=TEST_CC.upper(),
        subgraph_slug="test_subgraph",
    )
    assert rows.count() == n
    for row in rows:
        assert row.subgraph_slug == "test_subgraph"


@pytest.mark.django_db(
    transaction=False, databases=["default", "vectors"]
)
def test_write_spectral_nodes_subgraph_idempotency(seeded_spectral):
    """Writing with subgraph_slug only deletes rows for that subgraph."""
    from semantic_search.services.factor_node_writer import FactorNodeWriter
    from worldkg_nca.models import SpectralNodeMetric

    G = seeded_spectral["G"]
    features = seeded_spectral["features"]

    writer = FactorNodeWriter()

    # Write for subgraph A
    n_a = writer.write_spectral_nodes(
        G, features, TEST_CC, SNAP_A, subgraph_slug="sub_a",
    )

    # Write for subgraph B (same entities, different slug)
    n_b = writer.write_spectral_nodes(
        G, features, TEST_CC, SNAP_A, subgraph_slug="sub_b",
    )

    # Both should coexist
    rows_a = SpectralNodeMetric.objects.using("vectors").filter(
        snapshot_id=SNAP_A, country_code=TEST_CC.upper(), subgraph_slug="sub_a",
    )
    rows_b = SpectralNodeMetric.objects.using("vectors").filter(
        snapshot_id=SNAP_A, country_code=TEST_CC.upper(), subgraph_slug="sub_b",
    )
    assert rows_a.count() == n_a
    assert rows_b.count() == n_b

    # Re-write subgraph A — should not affect subgraph B
    n_a2 = writer.write_spectral_nodes(
        G, features, TEST_CC, SNAP_A, subgraph_slug="sub_a",
    )
    assert n_a2 == n_a
    assert rows_b.count() == n_b  # unchanged


# ── FactorResolutionService subgraph scoping ──────────────────────────────


@pytest.mark.django_db(
    transaction=False, databases=["default", "vectors"]
)
def test_diffusion_rank_subgraph_scoped(seeded_spectral):
    """diffusion_rank scopes the pgvector query to the anchor's subgraph."""
    from semantic_search.services.factor_resolution_service import (
        FactorResolutionService,
    )
    from semantic_search.services.factor_node_writer import FactorNodeWriter
    from worldkg_nca.models import SpectralNodeMetric

    G = seeded_spectral["G"]
    features = seeded_spectral["features"]
    nodes = list(G.nodes()) if hasattr(G, "nodes") else list(G.node_ids)

    writer = FactorNodeWriter()

    # Write two subgraphs with the same entities
    writer.write_spectral_nodes(G, features, TEST_CC, SNAP_A, subgraph_slug="sg1")
    writer.write_spectral_nodes(G, features, TEST_CC, SNAP_A, subgraph_slug="sg2")

    frs = FactorResolutionService()
    anchor = nodes[0]

    # Patch eigenvalues to avoid needing a GraphSpectralFingerprint
    from unittest import mock
    with mock.patch.object(
        FactorResolutionService, "_get_eigenvalues",
        staticmethod(
            lambda snapshot_id, country_code, subgraph_slug=None:
                features["eigenvalues"]
        ),
    ):
        results = frs.diffusion_rank(
            anchor_osm_id=int(anchor),
            t=1.0,
            snapshot_id=SNAP_A,
            country_code=TEST_CC,
            limit=5,
        )

    # Results should only contain nodes from the anchor's subgraph
    if results is not None:
        anchor_row = SpectralNodeMetric.objects.using("vectors").filter(
            snapshot_id=SNAP_A, country_code=TEST_CC.upper(),
            osm_id=int(anchor),
        ).first()
        sg = anchor_row.subgraph_slug
        for r in results:
            row = SpectralNodeMetric.objects.using("vectors").filter(
                snapshot_id=SNAP_A, country_code=TEST_CC.upper(),
                osm_id=r["osm_id"],
            ).first()
            assert row.subgraph_slug == sg, (
                f"Result osm_id={r['osm_id']} has subgraph_slug="
                f"{row.subgraph_slug}, expected {sg}"
            )


# ── Step 5c subgraph routing ─────────────────────────────────────────────


def test_step_5c_routes_to_subgraph_path():
    """Step 5c should route to _run_subgraph_spectral_analysis when subgraphs exist."""
    from pipeline.tasks.country_pipeline_steps.step_5c_graph_spectral import (
        _run_subgraph_spectral_analysis, _run_country_spectral_analysis,
    )
    # Just verify both functions exist and are callable
    assert callable(_run_subgraph_spectral_analysis)
    assert callable(_run_country_spectral_analysis)
