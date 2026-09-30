"""Invariant tests for GraphSignalService.

Pins the Phase 0 invariants from GRAPH_SPECTRAL_TEMPORAL_PLAN.md §"Phase 0:
Graph Signal Smoothness (Dirichlet Energy)":

- sᵀLs ≥ 0 (L is PSD)
- sᵀLs = 0 for a constant signal on a connected graph
- (αs)ᵀL(αs) = α²(sᵀLs)  (quadratic scaling)
- Smooth signal (spatially clustered classes) → low sᵀLs
- Noisy signal (random class assignment) → high sᵀLs

These tests do NOT require a database.
"""

import os

import numpy as np
import pytest
import networkx as nx

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "backend.settings")
django.setup()

from semantic_search.services.graph_signal_service import GraphSignalService


def _path_graph(n: int) -> nx.Graph:
    return nx.path_graph(n)


# --------------------------------------------------------------------------- #
# Dirichlet energy invariants
# --------------------------------------------------------------------------- #

def test_dirichlet_energy_non_negative():
    """sᵀLs ≥ 0 for all signals (Phase 0 invariant — L is PSD)."""
    G = _path_graph(15)
    svc = GraphSignalService()
    rng = np.random.default_rng(42)
    for _ in range(10):
        signal = rng.standard_normal(15)
        assert svc.signal_smoothness(G, signal) >= -1e-9


def test_dirichlet_energy_zero_for_constant_signal():
    """sᵀLs = 0 for a constant signal on a connected graph (Phase 0 invariant)."""
    G = _path_graph(15)
    svc = GraphSignalService()
    signal = np.full(15, 3.14)
    assert abs(svc.signal_smoothness(G, signal)) < 1e-9


def test_dirichlet_energy_quadratic_scaling():
    """(αs)ᵀL(αs) = α²(sᵀLs) (Phase 0 invariant)."""
    G = _path_graph(15)
    svc = GraphSignalService()
    signal = np.arange(15, dtype=float)
    base = svc.signal_smoothness(G, signal)
    scaled = svc.signal_smoothness(G, 2.5 * signal)
    np.testing.assert_allclose(scaled, 2.5 ** 2 * base, rtol=1e-6)


def test_smooth_signal_lower_than_noisy_signal():
    """Spatially clustered classes → lower Dirichlet energy than random."""
    G = _path_graph(30)
    svc = GraphSignalService()
    # Smooth: first half class 0, second half class 1
    smooth_signal = np.array([0.0] * 15 + [1.0] * 15)
    # Noisy: random assignment
    rng = np.random.default_rng(7)
    noisy_signal = rng.integers(0, 2, size=30).astype(float)
    smooth_energy = svc.signal_smoothness(G, smooth_signal)
    noisy_energy = svc.signal_smoothness(G, noisy_signal)
    assert smooth_energy < noisy_energy


# --------------------------------------------------------------------------- #
# Class signal construction
# --------------------------------------------------------------------------- #

def test_build_class_signal_assigns_class_indices():
    """build_class_signal returns a per-node signal + class→index map."""
    G = _path_graph(5)
    entity_class_map = {0: "wkgs:Cafe", 1: "wkgs:Cafe", 2: "wkgs:School"}
    svc = GraphSignalService()
    signal, class_index = svc.build_class_signal(G, entity_class_map)
    assert set(class_index.keys()) == {"wkgs:Cafe", "wkgs:School"}
    assert signal.shape == (5,)
    # Nodes not in the map get class index 0
    assert signal[3] == 0.0
    # Nodes in the map get the correct class index
    assert signal[0] == class_index["wkgs:Cafe"]
    assert signal[2] == class_index["wkgs:School"]


def test_build_onehot_signal_shape():
    """build_onehot_signal returns an (N, C) matrix."""
    G = _path_graph(4)
    entity_class_map = {0: "a", 1: "b", 2: "c"}
    svc = GraphSignalService()
    onehot, class_index = svc.build_onehot_signal(G, entity_class_map)
    assert onehot.shape == (4, 3)
    # Each row sums to 1 (one-hot)
    np.testing.assert_allclose(onehot.sum(axis=1), np.ones(4))


# --------------------------------------------------------------------------- #
# Signal diffusion
# --------------------------------------------------------------------------- #

def test_diffuse_signal_returns_signal_shape():
    """(L + μI)⁻¹s returns a vector of the same length as the signal."""
    G = _path_graph(10)
    svc = GraphSignalService()
    signal = np.zeros(10)
    signal[0] = 1.0
    diffused = svc.diffuse_signal(G, signal, mu=0.1)
    assert diffused.shape == (10,)


def test_diffuse_signal_smooths_the_input():
    """Diffusion reduces the variance of the signal (smoothing)."""
    G = _path_graph(20)
    svc = GraphSignalService()
    # Spike at one node
    signal = np.zeros(20)
    signal[10] = 1.0
    diffused = svc.diffuse_signal(G, signal, mu=0.5)
    # Variance should decrease after smoothing
    assert np.var(diffused) < np.var(signal)
