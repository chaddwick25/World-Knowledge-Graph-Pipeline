"""Unit tests for EmbeddingDriftService."""

import os
import numpy as np
import pytest
import django

# Setup Django settings for standalone test execution if needed
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "backend.settings")
django.setup()

from semantic_search.services.embedding_drift_service import EmbeddingDriftService


def test_sliced_wasserstein_distance_properties():
    """Verify basic properties of Sliced Wasserstein Distance."""
    service = EmbeddingDriftService()

    # 1. Identical distributions (should have distance very close to zero)
    X = np.random.normal(loc=0.0, scale=1.0, size=(100, 10))
    d_ident = service.compute_sliced_wasserstein_distance(X, X, num_projections=50)
    assert d_ident >= 0.0
    assert d_ident < 1e-4

    # 2. Non-identical distributions (should have positive distance)
    Y = np.random.normal(loc=2.0, scale=1.0, size=(100, 10))
    d_diff = service.compute_sliced_wasserstein_distance(X, Y, num_projections=50)
    assert d_diff > 0.5

    # 3. Symmetry (within random projection seed matching)
    d_xy = service.compute_sliced_wasserstein_distance(X, Y, num_projections=50, seed=42)
    d_yx = service.compute_sliced_wasserstein_distance(Y, X, num_projections=50, seed=42)
    np.testing.assert_allclose(d_xy, d_yx, atol=1e-5)


def test_freshness_score_calculation():
    """Verify that freshness score is high for small distances and decreases as distance grows."""
    service = EmbeddingDriftService()

    # Identical should yield 1.0 (100%) freshness
    assert service.calculate_freshness_score(0.0, 0.0) == 1.0

    # Progressive decrease
    score_low = service.calculate_freshness_score(0.05, 0.03)
    score_high = service.calculate_freshness_score(0.20, 0.15)

    assert 0.0 < score_high < score_low < 1.0


def test_empty_input_handling():
    """Verify empty/None inputs are handled gracefully without exceptions."""
    service = EmbeddingDriftService()
    empty = np.empty((0, 10))

    assert service.compute_sliced_wasserstein_distance(empty, empty) == 0.0
    assert service.compute_sliced_wasserstein_distance(None, None) == 0.0
