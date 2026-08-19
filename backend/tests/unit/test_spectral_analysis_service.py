"""Invariant tests for SpectralAnalysisService.

Pins the Phase 0 invariants from GRAPH_SPECTRAL_TEMPORAL_PLAN.md §"Phase 0:
Spectral Analysis" and §"Phase 0: Heat Kernel Diffusion":

- Eigenvalues are non-negative and sorted ascending
- λ₀ ≈ 0 (trivial eigenvalue, dropped from the returned features)
- Eigenvectors are orthonormal: ΦᵀΦ ≈ I
- All eigenvalues ∈ [0, 2] for normalized Laplacian
- For a known graph (path graph P₄), eigenvalues match analytical solution
- Heat kernel: mass conserved, non-negative, identity at t=0

These tests do NOT require a database — they build synthetic
``networkx`` graphs in-memory.
"""

import os

import numpy as np
import pytest
import networkx as nx

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "backend.settings")
django.setup()

from semantic_search.services.spectral_analysis_service import (
    SpectralAnalysisService,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _path_graph(n: int) -> nx.Graph:
    """Path graph P_n — analytical eigenvalues are known."""
    return nx.path_graph(n)


def _complete_graph(n: int) -> nx.Graph:
    """Complete graph K_n — normalized Laplacian eigenvalues are {0, n/(n-1)}."""
    return nx.complete_graph(n)


# --------------------------------------------------------------------------- #
# Spectral analysis invariants
# --------------------------------------------------------------------------- #

def test_eigenvalues_non_negative_and_sorted_ascending():
    """All eigenvalues ≥ 0 and sorted ascending (Phase 0 invariant)."""
    G = _path_graph(10)
    features = SpectralAnalysisService().compute_spectral_features(G, k=5)
    ev = np.array(features["eigenvalues"])
    assert (ev >= -1e-9).all(), f"Negative eigenvalue: {ev}"
    assert (ev[1:] >= ev[:-1] - 1e-9).all(), f"Not sorted: {ev}"


def test_eigenvalues_in_unit_range_for_normalized_laplacian():
    """Normalized Laplacian eigenvalues ∈ [0, 2] (Phase 0 invariant)."""
    G = _complete_graph(8)
    features = SpectralAnalysisService().compute_spectral_features(G, k=5)
    ev = np.array(features["eigenvalues"])
    assert (ev <= 2.0 + 1e-6).all(), f"Eigenvalue > 2: {ev}"
    assert (ev >= -1e-9).all(), f"Negative eigenvalue: {ev}"


def test_eigenvectors_orthonormal():
    """Eigenvectors are orthonormal: ΦᵀΦ ≈ I (Phase 0 invariant)."""
    G = _path_graph(20)
    features = SpectralAnalysisService().compute_spectral_features(G, k=5)
    V = np.asarray(features["eigenvectors"])
    gram = V.T @ V
    assert V.shape[1] == 5
    np.testing.assert_allclose(gram, np.eye(5), atol=1e-6)


def test_path_graph_eigenvalues_match_analytical():
    """Path graph P_n normalized-Laplacian eigenvalues match the closed form.

    λ_k = 1 - cos(π k / (n-1))  for k = 0, 1, ..., n-1
    """
    n = 8
    G = _path_graph(n)
    features = SpectralAnalysisService().compute_spectral_features(G, k=5)
    ev = np.array(features["eigenvalues"])
    # Analytical (drop λ₀=0 — already dropped by the service)
    expected = np.array([
        1.0 - np.cos(np.pi * k / (n - 1)) for k in range(1, 6)
    ])
    np.testing.assert_allclose(ev, expected, atol=1e-6)


def test_complete_graph_eigenvalues_match_analytical():
    """Complete graph K_n normalized Laplacian: {0, n/(n-1) (multiplicity n-1)}."""
    n = 6
    G = _complete_graph(n)
    features = SpectralAnalysisService().compute_spectral_features(G, k=4)
    ev = np.array(features["eigenvalues"])
    expected = np.array([n / (n - 1)] * 4)
    np.testing.assert_allclose(ev, expected, atol=1e-6)


def test_algebraic_connectivity_zero_for_disconnected_graph():
    """λ₂ = 0 for a disconnected graph (Phase 0 invariant)."""
    G = nx.Graph()
    G.add_edges_from([(0, 1), (1, 2), (2, 0)])  # component 1
    G.add_edges_from([(3, 4), (4, 5), (5, 3)])  # component 2
    features = SpectralAnalysisService().compute_spectral_features(G, k=3)
    # First non-trivial eigenvalue should be ~0 (disconnected)
    assert features["algebraic_connectivity"] < 1e-6


def test_algebraic_connectivity_positive_for_connected_graph():
    """λ₂ > 0 for a connected graph (Phase 0 invariant)."""
    G = _path_graph(10)
    features = SpectralAnalysisService().compute_spectral_features(G, k=5)
    assert features["algebraic_connectivity"] > 0.0


def test_empty_graph_returns_empty_features():
    """Empty graph → empty features (no crash)."""
    G = nx.Graph()
    features = SpectralAnalysisService().compute_spectral_features(G, k=4)
    assert features["node_count"] == 0
    assert features["eigenvalues"] == []
    assert features["algebraic_connectivity"] == 0.0


def test_directed_graph_is_converted_to_undirected():
    """A DiGraph is converted to undirected for spectral analysis."""
    G = nx.DiGraph()
    G.add_edges_from([(0, 1), (1, 2), (2, 3), (3, 0)])
    features = SpectralAnalysisService().compute_spectral_features(G, k=2)
    assert features["node_count"] == 4
    assert features["algebraic_connectivity"] > 0.0


# --------------------------------------------------------------------------- #
# Heat kernel invariants
# --------------------------------------------------------------------------- #

def test_heat_kernel_mass_conserved():
    """Σᵢ u(t)ᵢ = 1 for all t (Phase 0 invariant)."""
    G = _path_graph(20)
    svc = SpectralAnalysisService()
    result = svc.compute_heat_kernel(G, source_node=5, t_values=[0.5, 2.0, 10.0])
    for t, u in result.items():
        assert abs(sum(u) - 1.0) < 1e-6, f"t={t}: mass={sum(u)}"


def test_heat_kernel_non_negative():
    """u(t) ≥ 0 for all t (Phase 0 invariant)."""
    G = _path_graph(20)
    result = SpectralAnalysisService().compute_heat_kernel(
        G, source_node=0, t_values=[0.1, 1.0, 5.0]
    )
    for t, u in result.items():
        assert all(x >= -1e-9 for x in u), f"t={t}: negative value"


def test_heat_kernel_identity_at_t_zero():
    """u(0) = δ_source (Phase 0 invariant)."""
    G = _path_graph(10)
    result = SpectralAnalysisService().compute_heat_kernel(
        G, source_node=3, t_values=[1e-12]
    )
    u = result[1e-12]
    # Source node should have the bulk of the mass
    assert u[3] > 0.99


def test_heat_kernel_steady_state_approaches_uniform():
    """u(t→∞) → uniform on connected component (Phase 0 invariant)."""
    G = _path_graph(10)
    result = SpectralAnalysisService().compute_heat_kernel(
        G, source_node=0, t_values=[1000.0]
    )
    u = np.array(result[1000.0])
    np.testing.assert_allclose(u, np.full_like(u, 1.0 / 10), atol=1e-3)


def test_heat_kernel_unknown_source_raises():
    """Unknown source node → KeyError (clear error)."""
    G = _path_graph(5)
    with pytest.raises(KeyError):
        SpectralAnalysisService().compute_heat_kernel(G, source_node=999, t_values=[1.0])
