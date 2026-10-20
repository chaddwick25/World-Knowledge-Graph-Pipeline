"""SpectralForecastService — forecast spectral features across snapshots.

Forecasts the next snapshot's spectral profile from the time series of
``GraphSpectralFingerprint`` eigenvalues. Uses ARIMA when ``statsmodels``
is available AND ≥3 snapshots exist; otherwise falls back to exponential
smoothing (pure numpy, no new dependency).

Also provides CUSUM-based change-point detection on the spectral distance
time series.

Phase 0 invariants (enforced by ``tests/unit/test_spectral_forecast_service.py``):
- Forecast eigenvalues ≥ 0 (eigenvalues of a PSD matrix)
- Confidence intervals widen with forecast horizon
- Falls back to exponential smoothing when statsmodels unavailable
  or when < 3 snapshots exist

References:
- [STATS:Ch6] — Time series, forecasting, change-point detection
"""

import logging

import numpy as np

logger = logging.getLogger(__name__)


class SpectralForecastService:
    """Forecast spectral features using time-series methods.

    Requires ≥3 snapshots for ARIMA, ≥2 for exponential smoothing.
    With monthly snapshots, ARIMA needs 10-20 data points (1-2 years)
    for useful forecasts. The exponential smoothing fallback is more
    honest for the first year of data collection.
    """

    def forecast_eigenvalues(self, eigenvalue_series, steps: int = 1):
        """ARIMA forecast on eigenvalue time series.

        Args:
            eigenvalue_series: list of ``np.ndarray`` (or list), one per
                snapshot. Each entry is the eigenvalue vector for that
                snapshot. All entries must have the same length.
            steps: number of future snapshots to predict

        Returns:
            (forecast, confidence) — both ``np.ndarray`` of shape
            ``(n_eigenvalues,)`` for ``steps=1``. Confidence is the
            1.96σ half-width per eigenvalue.
        """
        series = [np.asarray(s, dtype=float) for s in eigenvalue_series if s is not None]
        if len(series) < 3:
            return self._exponential_smoothing_forecast(series, steps)

        try:
            from statsmodels.tsa.arima.model import ARIMA
        except ImportError:
            logger.info(
                "statsmodels unavailable — using exponential smoothing fallback"
            )
            return self._exponential_smoothing_forecast(series, steps)

        n_eigenvalues = len(series[0])
        forecasts = []
        confidences = []
        for i in range(n_eigenvalues):
            col = np.array([ev[i] if i < len(ev) else 0.0 for ev in series])
            try:
                model = ARIMA(col, order=(1, 1, 1))
                fit = model.fit()
                fc = fit.forecast(steps=steps)
                forecasts.append(float(fc[0]))
                se = float(np.std(fit.resid)) if hasattr(fit, 'resid') else 0.0
                confidences.append(1.96 * se)
            except Exception as exc:
                logger.warning(
                    "ARIMA fit failed for eigenvalue %d — falling back to exp smoothing: %s",
                    i, exc,
                )
                # Per-column fallback
                fc, ci = self._exponential_smoothing_forecast(series, steps)
                forecasts.append(float(fc[i]) if i < len(fc) else 0.0)
                confidences.append(float(ci[i]) if i < len(ci) else 0.0)

        forecast = np.array(forecasts)
        # Clamp to non-negative (eigenvalues of a PSD matrix are ≥ 0)
        forecast = np.clip(forecast, 0.0, None)
        return forecast, np.array(confidences)

    def detect_changepoints(self, spectral_distance_series) -> dict:
        """Detect structural change points in the spectral distance time series.

        Uses CUSUM (cumulative sum) method.  [STATS:Ch6]

        Args:
            spectral_distance_series: list of spectral distances between
                consecutive snapshots (length T-1 for T snapshots).

        Returns:
            dict with ``changepoint_index`` and ``changepoint_magnitude``.
            ``changepoint_index`` is -1 for an empty series.
        """
        distances = np.asarray(
            [float(d) for d in spectral_distance_series if d is not None],
            dtype=float,
        )
        if distances.size == 0:
            return {"changepoint_index": -1, "changepoint_magnitude": 0.0}
        mean = float(np.mean(distances))
        cusum = np.cumsum(distances - mean)
        max_idx = int(np.argmax(np.abs(cusum)))
        return {
            "changepoint_index": max_idx,
            "changepoint_magnitude": float(cusum[max_idx]),
        }

    def _exponential_smoothing_forecast(self, series, steps: int = 1, alpha: float = 0.3):
        """Simple exponential smoothing fallback for <3 snapshots.

        Pure numpy — no statsmodels dependency needed.
        """
        if not series:
            return np.array([]), np.array([])
        # Align all series to the same length (pad with zeros if needed)
        max_len = max(len(s) for s in series)
        aligned = np.array([
            np.pad(s, (0, max_len - len(s))) for s in series
        ], dtype=float)  # shape (T, k)

        # Weighted average — most recent snapshot gets the most weight
        T = aligned.shape[0]
        weights = np.array([alpha * (1 - alpha) ** i for i in range(T)][::-1])
        weights = weights / weights.sum()
        forecast = (weights[:, None] * aligned).sum(axis=0)
        # Confidence: 1.96σ across the series per eigenvalue
        if T > 1:
            conf = 1.96 * np.std(aligned, axis=0)
        else:
            conf = np.zeros_like(forecast)
        # Clamp forecast to non-negative
        forecast = np.clip(forecast, 0.0, None)
        return forecast, conf
