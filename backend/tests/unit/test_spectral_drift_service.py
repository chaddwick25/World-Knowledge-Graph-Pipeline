"""Invariant tests for SpectralDriftService.

Pins the Phase 0 invariants from GRAPH_SPECTRAL_TEMPORAL_PLAN.md §"Phase 0:
Spectral Drift (Distance Between Spectra)":

- Spectral distance ≥ 0 (L2 norm)
- Spectral distance = 0 for identical snapshots
- Fiedler drift ∈ [0, 2] (cosine distance range)
- Symmetric: drift(A, B) = drift(B, A)
- Drift increases monotonically with structural change

These tests do NOT require a database.
"""

import os

import numpy as np
import pytest

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "backend.settings")
django.setup()

from semantic_search.services.spectral_drift_service import SpectralDriftService


def _fp(eigenvalues, fiedler=None, smoothness=0.0):
    return {
        "eigenvalues": list(eigenvalues),
        "fiedler_vector": list(fiedler) if fiedler is not None else [],
        "signal_smoothness": smoothness,
    }


def test_spectral_distance_non_negative():
    """‖λ_a - λ_b‖₂ ≥ 0 (Phase 0 invariant)."""
    svc = SpectralDriftService()
    a = _fp([0.1, 0.2, 0.3])
    b = _fp([0.15, 0.25, 0.35])
    drift = svc.compute_spectral_drift(a, b)
    assert drift["spectral_distance"] >= 0.0


def test_spectral_distance_zero_for_identical_snapshots():
    """‖λ - λ‖₂ = 0 (Phase 0 invariant)."""
    svc = SpectralDriftService()
    a = _fp([0.1, 0.2, 0.3, 0.4], fiedler=[1.0, -1.0, 0.5, -0.5])
    drift = svc.compute_spectral_drift(a, a)
    assert drift["spectral_distance"] < 1e-9
    assert abs(drift["connectivity_delta"]) < 1e-9
    assert abs(drift["spectral_gap_delta"]) < 1e-9
    assert drift["fiedler_drift"] < 1e-9


def test_spectral_distance_symmetric():
    """drift(A, B) = drift(B, A) (Phase 0 invariant)."""
    svc = SpectralDriftService()
    a = _fp([0.1, 0.2, 0.3], fiedler=[1.0, -1.0, 0.5])
    b = _fp([0.2, 0.3, 0.4], fiedler=[-1.0, 1.0, -0.5])
    d_ab = svc.compute_spectral_drift(a, b)
    d_ba = svc.compute_spectral_drift(b, a)
    np.testing.assert_allclose(
        d_ab["spectral_distance"], d_ba["spectral_distance"], atol=1e-9
    )
    # Fiedler cosine distance is symmetric
    np.testing.assert_allclose(
        d_ab["fiedler_drift"], d_ba["fiedler_drift"], atol=1e-9
    )


def test_fiedler_drift_in_unit_range():
    """Fiedler drift ∈ [0, 2] (cosine distance range, Phase 0 invariant)."""
    svc = SpectralDriftService()
    a = _fp([0.1], fiedler=[1.0, 2.0, 3.0])
    b = _fp([0.1], fiedler=[-1.0, -2.0, -3.0])
    drift = svc.compute_spectral_drift(a, b)
    assert 0.0 <= drift["fiedler_drift"] <= 2.0 + 1e-6


def test_drift_increases_with_structural_change():
    """Larger eigenvalue shifts → larger spectral distance (Phase 0 invariant)."""
    svc = SpectralDriftService()
    base = _fp([0.1, 0.2, 0.3])
    small_change = _fp([0.11, 0.21, 0.31])
    large_change = _fp([0.5, 0.6, 0.7])
    d_small = svc.compute_spectral_drift(base, small_change)["spectral_distance"]
    d_large = svc.compute_spectral_drift(base, large_change)["spectral_distance"]
    assert d_large > d_small


def test_pads_unequal_eigenvalue_counts():
    """Eigenvalue count mismatch is handled by zero-padding."""
    svc = SpectralDriftService()
    a = _fp([0.1, 0.2, 0.3])
    b = _fp([0.1, 0.2, 0.3, 0.4, 0.5])
    drift = svc.compute_spectral_drift(a, b)
    # The padded entries contribute their full value to the distance
    expected = float(np.linalg.norm(np.array([0.0, 0.0, 0.0, 0.4, 0.5])))
    np.testing.assert_allclose(drift["spectral_distance"], expected, atol=1e-9)


def test_empty_fiedler_vectors_yield_zero_drift():
    """Missing Fiedler vectors → fiedler_drift = 0 (graceful)."""
    svc = SpectralDriftService()
    a = _fp([0.1])
    b = _fp([0.2])
    drift = svc.compute_spectral_drift(a, b)
    assert drift["fiedler_drift"] == 0.0


def test_signal_drift_deltas():
    """compute_signal_drift returns delta + ratio."""
    svc = SpectralDriftService()
    out = svc.compute_signal_drift(1.0, 1.5)
    assert out["smoothness_delta"] == 0.5
    assert out["smoothness_ratio"] == 1.5


def test_classify_drift_magnitude_thresholds():
    """classify_drift_magnitude returns the right category."""
    svc = SpectralDriftService()
    assert svc.classify_drift_magnitude(0.05) == "low"
    assert svc.classify_drift_magnitude(0.3) == "medium"
    assert svc.classify_drift_magnitude(0.7) == "high"
    assert svc.classify_drift_magnitude(1.5) == "extreme"
