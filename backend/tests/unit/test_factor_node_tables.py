"""Tests for the factor-node tables (FACTOR_NODE_RUNTIME_JOINS_PLAN.md).

Covers:
- FactorNodeWriter: SpectralNodeMetric rows match the eigenbasis returned
  by SpectralAnalysisService; idempotent rewrite; structural fields.
- FactorNodeWriter.write_drift_nodes: eigenvector sign alignment across a
  snapshot pair, community/degree deltas.
- FactorResolutionService: G4 availability check, metric joins, community
  summary, and the centerpiece — heat-kernel diffusion as a pgvector
  inner-product query, verified against a direct numpy eigen-expansion.
- compute_amenity_embeddings command + AmenityEmbedding lookup.
- Executor EVENT-DIFFUSION table path (table path is authoritative;
  legacy graph path removed).

Test data uses country_code='ZZ' and snapshot ids '9999_01_01' /
'9999_02_01' and is deleted in teardown (tests run against the live
Docker DBs per conftest.py).
"""

import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "backend.settings")
import django

django.setup()

import networkx as nx
import numpy as np
import pytest
from django.core.management import call_command
from unittest import mock

from semantic_search.services.factor_node_writer import FactorNodeWriter
from semantic_search.services.factor_resolution_service import (
    FactorResolutionService,
)
from semantic_search.services.spectral_analysis_service import (
    SpectralAnalysisService,
)
from worldkg_nca.models import (
    EIGEN_LOADING_DIM,
    AmenityEmbedding,
    DriftNodeMetric,
    SpectralNodeMetric,
)

# Factor tables live on the vectors DB; fingerprints/Snapshots on default.
pytestmark = pytest.mark.django_db(
    transaction=False, databases=["default", "vectors"],
)

TEST_CC = "ZZ"
SNAP_A = "9999_01_01"
SNAP_B = "9999_02_01"

N_NODES = 15
K_EIGEN = 10  # k_request = 11 < N — keeps eigsh happy on tiny graphs


def _weighted_path_graph(n=N_NODES):
    """Path graph with GeoVectors-style weights + a couple of wkg_class attrs."""
    G = nx.path_graph(n)
    for u, v in G.edges():
        G[u][v]["weight"] = 3.0 + 0.1 * abs(u - v)
    G.nodes[0]["wkg_class"] = "wkgs:Cafe"
    G.nodes[1]["wkg_class"] = "wkgs:Cafe"
    G.nodes[2]["wkg_class"] = "wkgs:Bar"
    return G


def _features(G, k=K_EIGEN):
    return SpectralAnalysisService().compute_spectral_features(G, k=k)


def _two_communities(G):
    """Deterministic two-way split for seeding louvain_community."""
    nodes = sorted(G.nodes())
    return {node: (0 if i < len(nodes) // 2 else 1)
            for i, node in enumerate(nodes)}


def _cleanup():
    for model in (SpectralNodeMetric, DriftNodeMetric):
        model.objects.using("vectors").filter(
            country_code=TEST_CC,
        ).delete()
    SpectralNodeMetric.objects.using("vectors").filter(
        snapshot_id__in=(SNAP_A, SNAP_B)
    ).delete()
    AmenityEmbedding.objects.using("vectors").filter(
        amenity_text__in=("cafe", "bar")
    ).delete()


@pytest.fixture
def seeded_spectral():
    """Write SpectralNodeMetric rows for SNAP_A from a small path graph."""
    _cleanup()
    G = _weighted_path_graph()
    features = _features(G)
    n = FactorNodeWriter().write_spectral_nodes(
        G, features, TEST_CC, SNAP_A,
        node_to_community=_two_communities(G),
    )
    assert n == N_NODES
    yield {"G": G, "features": features}
    _cleanup()


# ── FactorNodeWriter: spectral rows ──────────────────────────────────────


def test_write_spectral_nodes_match_eigenbasis(seeded_spectral):
    """Each row's eigen_loadings equal the node's row of Φ (zero-padded)."""
    features = seeded_spectral["features"]
    node_order = features["node_order"]
    eigvecs = features["eigenvectors"]

    rows = {
        r.osm_id: r
        for r in SpectralNodeMetric.objects.using("vectors").filter(
            snapshot_id=SNAP_A, country_code=TEST_CC,
        )
    }
    assert len(rows) == N_NODES

    for i, osm_id in enumerate(node_order):
        row = rows[int(osm_id)]
        loadings = np.asarray(row.eigen_loadings, dtype=float)
        assert loadings.shape == (EIGEN_LOADING_DIM,)
        # First K components equal the eigenvector row
        np.testing.assert_allclose(
            loadings[:K_EIGEN], eigvecs[i, :K_EIGEN], atol=1e-8,
        )
        # Padding is zero
        assert (loadings[K_EIGEN:] == 0.0).all()
        # Fiedler component is the first loading
        assert row.fiedler_component == pytest.approx(eigvecs[i, 0], abs=1e-10)


def test_write_spectral_nodes_structural_fields(seeded_spectral):
    """degree / clustering / component fields reflect the graph."""
    G = seeded_spectral["G"]
    rows = SpectralNodeMetric.objects.using("vectors").filter(
        snapshot_id=SNAP_A, country_code=TEST_CC,
    )
    degree_map = dict(G.degree())
    for row in rows:
        assert row.degree == degree_map[row.osm_id]
        # Path graph: no triangles → clustering is 0
        assert row.clustering_coeff == pytest.approx(0.0, abs=1e-9)
        # Connected graph → single component containing all nodes
        assert row.component_id == 0
        assert row.component_size == N_NODES


def test_write_spectral_nodes_idempotent(seeded_spectral):
    """Re-writing the same (snapshot, country) replaces rows."""
    G = seeded_spectral["G"]
    features = seeded_spectral["features"]
    n = FactorNodeWriter().write_spectral_nodes(G, features, TEST_CC, SNAP_A)
    assert n == N_NODES
    count = SpectralNodeMetric.objects.using("vectors").filter(
        snapshot_id=SNAP_A, country_code=TEST_CC,
    ).count()
    assert count == N_NODES


def test_write_spectral_nodes_dirichlet_contrib():
    """Per-node Dirichlet terms sum to the graph-level sᵀLs."""
    _cleanup()
    try:
        from semantic_search.services.graph_signal_service import (
            GraphSignalService,
        )
        G = _weighted_path_graph()
        features = _features(G)
        class_map = {n: d["wkg_class"] for n, d in G.nodes(data=True)
                     if d.get("wkg_class")}
        gss = GraphSignalService()
        signal, _ = gss.build_class_signal(G, class_map)
        expected_smoothness = gss.signal_smoothness(G, signal)

        FactorNodeWriter().write_spectral_nodes(
            G, features, TEST_CC, SNAP_A, signal=signal,
        )
        total = sum(
            r["dirichlet_contrib"] or 0.0
            for r in SpectralNodeMetric.objects.using("vectors")
            .filter(snapshot_id=SNAP_A, country_code=TEST_CC)
            .values("dirichlet_contrib")
        )
        # Σᵢ sᵢ·(Ls)ᵢ = sᵀLs  [GRAPH_REP:Ch3]
        assert total == pytest.approx(expected_smoothness, rel=1e-6)
    finally:
        _cleanup()


# ── FactorNodeWriter: drift rows (sign alignment) ────────────────────────


def _seed_pair(sign_flip=False, G_b=None, communities_b=None):
    """Seed SpectralNodeMetric for SNAP_A and SNAP_B; return (G, fa, fb)."""
    G = _weighted_path_graph()
    fa = _features(G)
    writer = FactorNodeWriter()
    writer.write_spectral_nodes(
        G, fa, TEST_CC, SNAP_A, node_to_community=_two_communities(G),
    )

    G_b = G_b or G
    fb = _features(G_b)
    if sign_flip:
        # Simulate eigsh sign indeterminacy: flip every eigenvector column
        fb = dict(fb)
        fb["eigenvectors"] = -fb["eigenvectors"]
        fb["fiedler_vector"] = [-x for x in fb["fiedler_vector"]]
    writer.write_spectral_nodes(
        G_b, fb, TEST_CC, SNAP_B,
        node_to_community=(
            communities_b if communities_b is not None
            else _two_communities(G_b)
        ),
    )
    return G, fa, fb


def test_drift_nodes_sign_aligned_zero_for_identical_graphs():
    """Sign-flipped eigenbasis of the SAME graph must give ~zero drift."""
    _cleanup()
    try:
        _seed_pair(sign_flip=True)
        n = FactorNodeWriter().write_drift_nodes(TEST_CC, SNAP_A, SNAP_B)
        assert n == N_NODES

        rows = DriftNodeMetric.objects.using("vectors").filter(
            country_code=TEST_CC,
            snapshot_from_id=SNAP_A,
            snapshot_to_id=SNAP_B,
        )
        for row in rows:
            assert row.fiedler_delta == pytest.approx(0.0, abs=1e-6)
            assert row.loading_drift == pytest.approx(0.0, abs=1e-6)
            assert row.community_changed is False
            assert row.degree_delta == 0
    finally:
        _cleanup()


def test_drift_nodes_detects_changes():
    """Changed degree/community propagate to the drift rows."""
    _cleanup()
    try:
        G_b = _weighted_path_graph()
        G_b.add_edge(0, 14, weight=3.5)  # closes the path into a cycle
        communities_b = _two_communities(G_b)
        communities_b[0] = 1  # node 0 switches community
        _seed_pair(G_b=G_b, communities_b=communities_b)

        n = FactorNodeWriter().write_drift_nodes(TEST_CC, SNAP_A, SNAP_B)
        assert n == N_NODES

        rows = {
            r.osm_id: r
            for r in DriftNodeMetric.objects.using("vectors").filter(
                country_code=TEST_CC,
                snapshot_from_id=SNAP_A,
                snapshot_to_id=SNAP_B,
            )
        }
        # Node 0 and 14 each gained one edge
        assert rows[0].degree_delta == 1
        assert rows[14].degree_delta == 1
        assert rows[7].degree_delta == 0
        # Node 0 changed community; node 1 did not
        assert rows[0].community_changed is True
        assert rows[1].community_changed is False
        # Structural change → nonzero loading drift somewhere
        assert any(r.loading_drift and r.loading_drift > 1e-3
                   for r in rows.values())
    finally:
        _cleanup()


# ── FactorResolutionService ──────────────────────────────────────────────


def _patch_eigenvalues(features):
    return mock.patch.object(
        FactorResolutionService, "_get_eigenvalues",
        staticmethod(
            lambda snapshot_id, country_code, subgraph_slug=None:
                features["eigenvalues"]
        ),
    )


def test_check_availability(seeded_spectral):
    frs = FactorResolutionService()
    present = sorted(seeded_spectral["G"].nodes())[:3]
    result = frs.check_availability(present + [999999999], SNAP_A, TEST_CC)
    assert all(result[o] for o in present)
    assert result[999999999] is False


def test_resolve_metrics(seeded_spectral):
    frs = FactorResolutionService()
    osm_ids = sorted(seeded_spectral["G"].nodes())[:3]
    metrics = frs.resolve_metrics(osm_ids, SNAP_A, TEST_CC)
    assert set(metrics.keys()) == set(osm_ids)
    for m in metrics.values():
        assert m["fiedler_component"] is not None
        assert m["louvain_community"] in (0, 1)
        assert m["component_size"] == N_NODES


def test_community_summary(seeded_spectral):
    frs = FactorResolutionService()
    summary = frs.community_summary(SNAP_A, TEST_CC)
    assert summary["community_count"] == 2
    total = sum(c["node_count"] for c in summary["communities"])
    assert total == N_NODES


def test_diffusion_rank_matches_eigen_expansion(seeded_spectral):
    """The pgvector query equals the direct eigen-expansion of the heat kernel.

    score(node) = Σ_k e^{-t·λ_k} · φ_k(anchor) · φ_k(node)  over the stored
    (truncated) basis — both sides use the same K eigenpairs, so the match
    is exact up to float tolerance.
    """
    features = seeded_spectral["features"]
    node_order = features["node_order"]
    eigvecs = features["eigenvectors"]
    lam = np.asarray(features["eigenvalues"])
    anchor = int(node_order[3])
    t = 1.0

    # Expected: u = Φ · diag(e^{-tλ}) · Φᵀ · δ_anchor  (same K basis)
    anchor_idx = node_order.index(anchor)
    phi_anchor = eigvecs[anchor_idx, :]
    w = np.exp(-t * lam) * phi_anchor           # (K,)
    expected = eigvecs @ w                       # (N,)
    expected_order = sorted(
        range(len(node_order)),
        key=lambda i: expected[i],
        reverse=True,
    )
    # Same semantics as diffusion_rank: drop the anchor and scores ≤ 1e-6
    # (a truncated eigen-expansion can produce tiny negative scores).
    expected_ids = [
        int(node_order[i]) for i in expected_order
        if int(node_order[i]) != anchor and expected[i] > 1e-6
    ][:5]

    frs = FactorResolutionService()
    trace = []
    with _patch_eigenvalues(features):
        ranked = frs.diffusion_rank(
            anchor, t, SNAP_A, TEST_CC, limit=5, trace=trace,
        )

    assert ranked is not None
    got_ids = [r["osm_id"] for r in ranked]
    assert got_ids == expected_ids
    for r in ranked:
        i = node_order.index(r["osm_id"])
        assert r["score"] == pytest.approx(float(expected[i]), rel=1e-4)
    # Trace records the factor join
    assert any(s.get("step") == "factor_join" for s in trace)


def test_diffusion_rank_candidate_filter(seeded_spectral):
    """candidate_osm_ids restricts the ranking to the given subset."""
    features = seeded_spectral["features"]
    node_order = features["node_order"]
    anchor = int(node_order[3])
    candidates = [int(node_order[5]), int(node_order[6]), 999999999]

    frs = FactorResolutionService()
    with _patch_eigenvalues(features):
        ranked = frs.diffusion_rank(
            anchor, 1.0, SNAP_A, TEST_CC,
            candidate_osm_ids=candidates, limit=10,
        )
    assert ranked is not None
    assert {r["osm_id"] for r in ranked} <= set(candidates)
    assert 999999999 not in {r["osm_id"] for r in ranked}


def test_diffusion_rank_missing_anchor_returns_none(seeded_spectral):
    frs = FactorResolutionService()
    trace = []
    with _patch_eigenvalues(seeded_spectral["features"]):
        result = frs.diffusion_rank(
            999999999, 1.0, SNAP_A, TEST_CC, trace=trace,
        )
    assert result is None
    assert any("no factor row" in s.get("warning", "") for s in trace)


# ── AmenityEmbedding + compute_amenity_embeddings ────────────────────────


def test_amenity_embedding_lookup():
    _cleanup()
    try:
        vec = (np.arange(300, dtype=float) / 300.0)
        AmenityEmbedding.objects.using("vectors").update_or_create(
            amenity_text="cafe", defaults={"embedding": vec.tolist()},
        )
        frs = FactorResolutionService()
        got = frs.amenity_embedding("  Cafe ")  # case/whitespace-insensitive
        np.testing.assert_allclose(got, vec, atol=1e-8)
        assert frs.amenity_embedding("not_in_vocab") is None
    finally:
        _cleanup()


def test_compute_amenity_embeddings_command(tmp_path):
    _cleanup()
    vocab_file = tmp_path / "vocab.json"
    vocab_file.write_text('["cafe", "Bar ", "cafe", ""]')
    fake_vec = np.full(300, 0.1)

    try:
        with mock.patch(
            "semantic_search.services.fasttext_service."
            "FastTextEmbeddingService.calculate_text_embedding",
            classmethod(lambda cls, text: fake_vec),
        ):
            call_command(
                "compute_amenity_embeddings", vocab_path=str(vocab_file),
            )
        rows = AmenityEmbedding.objects.using("vectors").filter(
            amenity_text__in=("cafe", "bar"),
        )
        # deduped + lowercased + blank dropped → 2 rows
        assert rows.count() == 2
        for row in rows:
            np.testing.assert_allclose(
                np.asarray(row.embedding, dtype=float), fake_vec, atol=1e-8,
            )

        # Idempotent: second run upserts, no duplicates
        with mock.patch(
            "semantic_search.services.fasttext_service."
            "FastTextEmbeddingService.calculate_text_embedding",
            classmethod(lambda cls, text: fake_vec),
        ):
            call_command(
                "compute_amenity_embeddings", vocab_path=str(vocab_file),
            )
        assert AmenityEmbedding.objects.using("vectors").filter(
            amenity_text__in=("cafe", "bar"),
        ).count() == 2
    finally:
        _cleanup()


# ── Executor table path (flag on) ────────────────────────────────────────


def test_executor_event_diffusion_table_path(seeded_spectral):
    """EVENT-DIFFUSION resolves via factor tables (table path is authoritative)."""
    from semantic_search.services.entity_geocoder import EntityGeocoder
    from semantic_search.services.query_executor_service import (
        QueryExecutorService,
    )

    features = seeded_spectral["features"]
    anchor = int(features["node_order"][3])
    parsed = {
        "template": "EVENT-DIFFUSION (#14)",
        "concepts": [
            {"type": "LOCATION", "text": "Test Source",
             "confidence": 1.0, "resolved_value": None},
        ],
    }

    with _patch_eigenvalues(features), \
            mock.patch.object(
                EntityGeocoder, "geocode",
                staticmethod(lambda name, country, snap: {
                    "osm_id": anchor, "name": "Test Source",
                    "lat": 0.0, "lon": 0.0, "tags": {},
                }),
            ):
        result = QueryExecutorService.execute(
            parsed, country_code=TEST_CC, snapshot_date=SNAP_A,
        )

    assert "error" not in result
    assert result["results"]["source_osm_id"] == anchor
    affected = result["results"]["affected"]
    assert set(affected.keys()) == {1.0, 5.0, 10.0}
    assert all(len(v) > 0 for v in affected.values())
    # Table path: factor_join in trace, no graph trace steps
    steps = [s.get("step") for s in result["trace"]]
    assert "factor_join" in steps


def test_executor_event_diffusion_no_factor_rows(seeded_spectral):
    """With no factor rows for the anchor, the executor returns an error
    (the legacy graph fallback has been removed)."""
    from semantic_search.services.entity_geocoder import EntityGeocoder
    from semantic_search.services.query_executor_service import (
        QueryExecutorService,
    )

    features = seeded_spectral["features"]
    anchor = int(features["node_order"][3])
    parsed = {
        "template": "EVENT-DIFFUSION (#14)",
        "concepts": [
            {"type": "LOCATION", "text": "Test Source",
             "confidence": 1.0, "resolved_value": None},
        ],
    }

    # Use a non-existent snapshot so factor rows aren't found
    with mock.patch.object(
                EntityGeocoder, "geocode",
                staticmethod(lambda name, country, snap: {
                    "osm_id": anchor, "name": "Test Source",
                    "lat": 0.0, "lon": 0.0, "tags": {},
                }),
            ):
        result = QueryExecutorService.execute(
            parsed, country_code=TEST_CC, snapshot_date="1999_01_01",
        )

    assert "error" in result["results"]
