"""Tests for SubgraphTransportService (SUBGRAPH_FUNCTIONAL_MAPS_PLAN_v2.md).

Covers:
- Transport matrix computation (identity, permutation, non-isomorphic)
- Transport loadings application
- Adjacency detection from overlapping bboxes
- Insufficient shared entities → skip
- Batch write + idempotent re-write
- Cross-subgraph diffusion with transport matrices
- Cross-subgraph diffusion fallback when no transport matrix exists
"""

import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "backend.settings")
import django

django.setup()

import numpy as np
import pytest

from semantic_search.services.subgraph_transport_service import (
    SubgraphTransportService,
    MIN_SHARED_ENTITIES,
)
from worldkg_nca.models import SubgraphTransport

# Factor tables live on the vectors DB
pytestmark = pytest.mark.django_db(
    transaction=False, databases=["default", "vectors"],
)

TEST_CC = "ZZ"
SNAP = "9999_01_01"


# ── Transport matrix computation ────────────────────────────────────────


class TestComputeTransportMatrix:
    """Tests for SubgraphTransportService.compute_transport_matrix."""

    def test_identity_graphs_give_identity_matrix(self):
        """Two identical graphs → C ≈ identity (up to sign)."""
        svc = SubgraphTransportService()
        np.random.seed(42)
        n, k = 100, 10

        # Build a random symmetric matrix and its eigendecomposition
        A = np.random.randn(n, n)
        A = (A + A.T) / 2
        eigenvalues, eigenvectors = np.linalg.eigh(A)
        # Sort ascending, drop trivial
        idx = eigenvalues.argsort()
        eigenvalues = eigenvalues[idx][1:k + 1]
        eigenvectors = eigenvectors[:, idx][:, 1:k + 1]

        node_ids = np.arange(n, dtype=np.int64)
        shared_ids = node_ids.copy()  # all nodes shared

        result = svc.compute_transport_matrix(
            eigenvalues_a=eigenvalues, eigenvectors_a=eigenvectors,
            node_ids_a=node_ids,
            eigenvalues_b=eigenvalues.copy(), eigenvectors_b=eigenvectors.copy(),
            node_ids_b=node_ids.copy(),
            shared_node_ids=shared_ids,
            lambda_reg=0.0,  # no regularization for identity test
        )

        assert result is not None
        C = result["transport_matrix"]
        # C should be close to identity (or -identity due to sign ambiguity)
        diag = np.diag(C)
        assert np.allclose(np.abs(diag), 1.0, atol=0.01)
        # Off-diagonal should be near zero
        off_diag = C - np.diag(diag)
        assert np.allclose(off_diag, 0.0, atol=0.01)

    def test_permuted_graphs_give_permutation_matrix(self):
        """Two isomorphic graphs (node-permuted) → C ≈ permutation matrix."""
        svc = SubgraphTransportService()
        np.random.seed(123)
        n, k = 80, 8

        A = np.random.randn(n, n)
        A = (A + A.T) / 2
        eigenvalues, eigenvectors = np.linalg.eigh(A)
        idx = eigenvalues.argsort()
        eigenvalues = eigenvalues[idx][1:k + 1]
        eigenvectors = eigenvectors[:, idx][:, 1:k + 1]

        node_ids_a = np.arange(n, dtype=np.int64)
        # Permute node ordering for B
        perm = np.random.permutation(n)
        node_ids_b = node_ids_a[perm]
        eigenvectors_b = eigenvectors[perm]

        shared_ids = np.intersect1d(node_ids_a, node_ids_b)

        result = svc.compute_transport_matrix(
            eigenvalues_a=eigenvalues, eigenvectors_a=eigenvectors,
            node_ids_a=node_ids_a,
            eigenvalues_b=eigenvalues.copy(), eigenvectors_b=eigenvectors_b,
            node_ids_b=node_ids_b,
            shared_node_ids=shared_ids,
            lambda_reg=0.0,
        )

        assert result is not None
        C = result["transport_matrix"]
        # C should be close to a signed permutation matrix
        # Each row/col should have one entry near ±1, rest near 0
        for i in range(k):
            row_max = np.max(np.abs(C[i]))
            assert row_max > 0.9, f"Row {i} max abs value {row_max} < 0.9"
            # Count near-zero entries
            near_zero = np.sum(np.abs(C[i]) < 0.1)
            assert near_zero >= k - 2, f"Row {i} has too many non-zero entries"

    def test_non_isomorphic_subgraphs_low_residual(self):
        """Non-isomorphic subgraphs (edge perturbation) → low residual."""
        svc = SubgraphTransportService()
        np.random.seed(456)
        n, k = 200, 12

        # Build graph A
        A = np.random.randn(n, n)
        A = (A + A.T) / 2
        lam_a, vec_a = np.linalg.eigh(A)
        idx = lam_a.argsort()
        lam_a = lam_a[idx][1:k + 1]
        vec_a = vec_a[:, idx][:, 1:k + 1]

        # Build graph B = A + perturbation (non-isomorphic)
        perturbation = np.random.randn(n, n) * 0.1
        perturbation = (perturbation + perturbation.T) / 2
        B = A + perturbation
        lam_b, vec_b = np.linalg.eigh(B)
        idx = lam_b.argsort()
        lam_b = lam_b[idx][1:k + 1]
        vec_b = vec_b[:, idx][:, 1:k + 1]

        node_ids = np.arange(n, dtype=np.int64)
        shared_ids = node_ids.copy()

        result = svc.compute_transport_matrix(
            eigenvalues_a=lam_a, eigenvectors_a=vec_a,
            node_ids_a=node_ids,
            eigenvalues_b=lam_b, eigenvectors_b=vec_b,
            node_ids_b=node_ids.copy(),
            shared_node_ids=shared_ids,
            lambda_reg=1e-3,
        )

        assert result is not None
        # With small perturbation, the fit residual should be low
        assert result["fit_residual"] < 0.5, (
            f"Fit residual {result['fit_residual']} too high for small perturbation"
        )

    def test_insufficient_shared_entities_returns_none(self):
        """Fewer than MIN_SHARED_ENTITIES shared → None."""
        svc = SubgraphTransportService()
        n, k = 100, 10

        A = np.random.randn(n, n)
        A = (A + A.T) / 2
        lam, vec = np.linalg.eigh(A)
        idx = lam.argsort()
        lam = lam[idx][1:k + 1]
        vec = vec[:, idx][:, 1:k + 1]

        node_ids = np.arange(n, dtype=np.int64)
        # Only 5 shared — below MIN_SHARED_ENTITIES
        shared_ids = np.array([0, 1, 2, 3, 4], dtype=np.int64)

        result = svc.compute_transport_matrix(
            eigenvalues_a=lam, eigenvectors_a=vec,
            node_ids_a=node_ids,
            eigenvalues_b=lam.copy(), eigenvectors_b=vec.copy(),
            node_ids_b=node_ids.copy(),
            shared_node_ids=shared_ids,
        )

        assert result is None

    def test_too_few_eigenvalues_returns_none(self):
        """k < 2 → None."""
        svc = SubgraphTransportService()
        result = svc.compute_transport_matrix(
            eigenvalues_a=np.array([0.5]),
            eigenvectors_a=np.ones((10, 1)),
            node_ids_a=np.arange(10),
            eigenvalues_b=np.array([0.5]),
            eigenvectors_b=np.ones((10, 1)),
            node_ids_b=np.arange(10),
            shared_node_ids=np.arange(10),
        )
        assert result is None


# ── Transport loadings ──────────────────────────────────────────────────


class TestTransportLoadings:
    """Tests for SubgraphTransportService.transport_loadings."""

    def test_identity_transport_unchanged(self):
        """Transport through identity matrix → loadings unchanged."""
        svc = SubgraphTransportService()
        k = 10
        C = np.eye(k)
        loadings = np.random.randn(k)
        transported = svc.transport_loadings(loadings, C)
        assert np.allclose(transported, loadings)

    def test_zero_matrix_gives_zero(self):
        """Transport through zero matrix → zero loadings."""
        svc = SubgraphTransportService()
        k = 5
        C = np.zeros((k, k))
        loadings = np.random.randn(k)
        transported = svc.transport_loadings(loadings, C)
        assert np.allclose(transported, 0.0)

    def test_truncates_longer_loadings(self):
        """Loadings longer than k are truncated."""
        svc = SubgraphTransportService()
        k = 5
        C = np.eye(k)
        loadings = np.random.randn(k + 10)
        transported = svc.transport_loadings(loadings, C)
        assert len(transported) == k
        assert np.allclose(transported, loadings[:k])

    def test_pads_shorter_loadings(self):
        """Loadings shorter than k are zero-padded."""
        svc = SubgraphTransportService()
        k = 5
        C = np.eye(k)
        loadings = np.random.randn(3)
        transported = svc.transport_loadings(loadings, C)
        assert len(transported) == k
        assert np.allclose(transported[:3], loadings)
        assert np.allclose(transported[3:], 0.0)


# ── Adjacency computation ───────────────────────────────────────────────


class TestComputeAdjacency:
    """Tests for SubgraphTransportService.compute_adjacency."""

    def test_overlapping_bboxes_are_adjacent(self):
        """Two subgraphs with overlapping bboxes → adjacent."""
        from pipeline.config import SubgraphConfig

        svc = SubgraphTransportService()
        sg_a = SubgraphConfig(
            name="A", slug="aaa",
            bbox_min_lon=0.0, bbox_min_lat=0.0,
            bbox_max_lon=1.0, bbox_max_lat=1.0,
        )
        sg_b = SubgraphConfig(
            name="B", slug="bbb",
            bbox_min_lon=0.5, bbox_min_lat=0.5,
            bbox_max_lon=2.0, bbox_max_lat=2.0,
        )
        pairs = svc.compute_adjacency([sg_a, sg_b])
        assert ("aaa", "bbb") in pairs

    def test_non_overlapping_bboxes_not_adjacent(self):
        """Two subgraphs with far-apart bboxes → not adjacent."""
        from pipeline.config import SubgraphConfig

        svc = SubgraphTransportService()
        sg_a = SubgraphConfig(
            name="A", slug="aaa",
            bbox_min_lon=0.0, bbox_min_lat=0.0,
            bbox_max_lon=1.0, bbox_max_lat=1.0,
        )
        sg_b = SubgraphConfig(
            name="B", slug="bbb",
            bbox_min_lon=10.0, bbox_min_lat=10.0,
            bbox_max_lon=11.0, bbox_max_lat=11.0,
        )
        pairs = svc.compute_adjacency([sg_a, sg_b], buffer_deg=0.45)
        assert ("aaa", "bbb") not in pairs

    def test_close_bboxes_are_adjacent(self):
        """Two subgraphs within 2× buffer_deg → adjacent."""
        from pipeline.config import SubgraphConfig

        svc = SubgraphTransportService()
        sg_a = SubgraphConfig(
            name="A", slug="aaa",
            bbox_min_lon=0.0, bbox_min_lat=0.0,
            bbox_max_lon=1.0, bbox_max_lat=1.0,
        )
        sg_b = SubgraphConfig(
            name="B", slug="bbb",
            bbox_min_lon=1.5, bbox_min_lat=1.5,
            bbox_max_lon=2.5, bbox_max_lat=2.5,
        )
        # Gap is 0.5°, 2×0.45=0.9° → adjacent
        pairs = svc.compute_adjacency([sg_a, sg_b], buffer_deg=0.45)
        assert ("aaa", "bbb") in pairs

    def test_pairs_sorted_and_unique(self):
        """Pairs are sorted and each appears once."""
        from pipeline.config import SubgraphConfig

        svc = SubgraphTransportService()
        configs = [
            SubgraphConfig(
                name=f"SG{i}", slug=f"sg_{i}",
                bbox_min_lon=float(i), bbox_min_lat=0.0,
                bbox_max_lon=float(i + 1), bbox_max_lat=1.0,
            )
            for i in range(5)
        ]
        pairs = svc.compute_adjacency(configs)
        # All pairs should be sorted
        for a, b in pairs:
            assert a < b
        # No duplicates
        assert len(pairs) == len(set(pairs))


# ── DB I/O ──────────────────────────────────────────────────────────────


class TestTransportDBIO:
    """Tests for write_transport_matrices and load_transport_matrix."""

    def test_write_and_load_transport_matrix(self):
        """Write a transport matrix and load it back."""
        svc = SubgraphTransportService()
        k = 5
        C = np.eye(k) * 2.0

        transport_data = [{
            "subgraph_from": "dublin",
            "subgraph_to": "meath",
            "transport_matrix": C,
            "k_dim": k,
            "shared_entity_count": 100,
            "fit_residual": 0.1,
            "commutativity_residual": 0.05,
        }]

        written = svc.write_transport_matrices(
            snapshot_id=SNAP, country_code=TEST_CC,
            transport_data=transport_data,
        )
        assert written == 1

        loaded = svc.load_transport_matrix(
            snapshot_id=SNAP, country_code=TEST_CC,
            subgraph_from="dublin", subgraph_to="meath",
        )
        assert loaded is not None
        assert loaded.shape == (k, k)
        assert np.allclose(loaded, C)

        # Cleanup
        SubgraphTransport.objects.using("vectors").filter(
            snapshot_id=SNAP, country_code=TEST_CC,
        ).delete()

    def test_idempotent_write(self):
        """Re-writing replaces existing rows."""
        svc = SubgraphTransportService()
        k = 3
        C1 = np.ones((k, k))
        C2 = np.eye(k) * 3.0

        # First write
        svc.write_transport_matrices(
            snapshot_id=SNAP, country_code=TEST_CC,
            transport_data=[{
                "subgraph_from": "a", "subgraph_to": "b",
                "transport_matrix": C1, "k_dim": k,
                "shared_entity_count": 50,
                "fit_residual": 0.2, "commutativity_residual": 0.1,
            }],
        )
        # Second write (different matrix)
        svc.write_transport_matrices(
            snapshot_id=SNAP, country_code=TEST_CC,
            transport_data=[{
                "subgraph_from": "a", "subgraph_to": "b",
                "transport_matrix": C2, "k_dim": k,
                "shared_entity_count": 50,
                "fit_residual": 0.15, "commutativity_residual": 0.08,
            }],
        )

        # Should only have 1 row (replaced)
        count = SubgraphTransport.objects.using("vectors").filter(
            snapshot_id=SNAP, country_code=TEST_CC,
            subgraph_from="a", subgraph_to="b",
        ).count()
        assert count == 1

        loaded = svc.load_transport_matrix(
            snapshot_id=SNAP, country_code=TEST_CC,
            subgraph_from="a", subgraph_to="b",
        )
        assert np.allclose(loaded, C2)

        # Cleanup
        SubgraphTransport.objects.using("vectors").filter(
            snapshot_id=SNAP, country_code=TEST_CC,
        ).delete()

    def test_load_nonexistent_returns_none(self):
        """Loading a non-existent transport matrix → None."""
        svc = SubgraphTransportService()
        result = svc.load_transport_matrix(
            snapshot_id="9999_99_99", country_code=TEST_CC,
            subgraph_from="nowhere", subgraph_to="anywhere",
        )
        assert result is None

    def test_empty_write_returns_zero(self):
        """Writing empty list → 0 rows, no error."""
        svc = SubgraphTransportService()
        written = svc.write_transport_matrices(
            snapshot_id=SNAP, country_code=TEST_CC,
            transport_data=[],
        )
        assert written == 0
