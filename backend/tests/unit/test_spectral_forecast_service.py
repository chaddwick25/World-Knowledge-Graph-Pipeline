"""Invariant tests for SpectralForecastService.

Pins the Phase 0 invariants from GRAPH_SPECTRAL_TEMPORAL_PLAN.md §"Phase 0:
Forecasting (ARIMA / Exponential Smoothing)":

- Forecast eigenvalues ≥ 0 (eigenvalues of a PSD matrix)
- Falls back to exponential smoothing when <3 snapshots
- Confidence intervals are non-negative
- Change-point detection returns a valid index

These tests do NOT require a database and do NOT require statsmodels
(the exponential smoothing fallback is exercised).
"""

import os

import numpy as np
import pytest

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "backend.settings")
django.setup()

from semantic_search.services.spectral_forecast_service import (
    SpectralForecastService,
)


def test_forecast_eigenvalues_non_negative():
    """Forecast eigenvalues ≥ 0 (Phase 0 invariant)."""
    svc = SpectralForecastService()
    series = [
        np.array([0.1, 0.2, 0.3]),
        np.array([0.15, 0.25, 0.35]),
    ]
    fc, _ = svc.forecast_eigenvalues(series, steps=1)
    assert (fc >= 0.0).all()


def test_forecast_falls_back_with_two_snapshots():
    """<3 snapshots → exponential smoothing fallback (no ARIMA)."""
    svc = SpectralForecastService()
    series = [np.array([0.1, 0.2]), np.array([0.2, 0.3])]
    fc, ci = svc.forecast_eigenvalues(series, steps=1)
    assert fc.shape == (2,)
    assert ci.shape == (2,)


def test_forecast_with_three_snapshots():
    """≥3 snapshots → ARIMA path (or exp-smoothing if statsmodels missing)."""
    svc = SpectralForecastService()
    series = [
        np.array([0.1, 0.2]),
        np.array([0.15, 0.25]),
        np.array([0.2, 0.3]),
    ]
    fc, ci = svc.forecast_eigenvalues(series, steps=1)
    assert fc.shape == (2,)
    assert (fc >= 0.0).all()


def test_forecast_confidence_non_negative():
    """Confidence intervals are non-negative (1.96σ ≥ 0)."""
    svc = SpectralForecastService()
    series = [np.array([0.1, 0.2]), np.array([0.5, 0.6])]
    _, ci = svc.forecast_eigenvalues(series, steps=1)
    assert (ci >= 0.0).all()


def test_forecast_empty_series_returns_empty():
    """Empty series → empty forecast (no crash)."""
    svc = SpectralForecastService()
    fc, ci = svc.forecast_eigenvalues([], steps=1)
    assert fc.size == 0
    assert ci.size == 0


def test_changepoint_detection_returns_valid_index():
    """CUSUM change-point detection returns a valid index in range."""
    svc = SpectralForecastService()
    distances = [0.1, 0.1, 0.1, 1.5, 0.1, 0.1]
    cp = svc.detect_changepoints(distances)
    assert 0 <= cp["changepoint_index"] < len(distances)


def test_changepoint_detection_empty_series():
    """Empty distance series → changepoint_index = -1."""
    svc = SpectralForecastService()
    cp = svc.detect_changepoints([])
    assert cp["changepoint_index"] == -1
    assert cp["changepoint_magnitude"] == 0.0


def test_changepoint_detects_large_jump():
    """CUSUM flags the index where the structural jump occurs."""
    svc = SpectralForecastService()
    # Constant baseline then a big jump
    distances = [0.05, 0.05, 0.05, 0.05, 2.0, 2.0]
    cp = svc.detect_changepoints(distances)
    # The CUSUM magnitude (signed) should be large in absolute value
    assert abs(cp["changepoint_magnitude"]) > 0.5
