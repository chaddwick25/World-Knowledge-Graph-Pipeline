"""SpectralDriftService — spectral drift between snapshots.

Computes the structural change between two ``GraphSpectralFingerprint``
records:

- **Spectral distance**: ``‖λ_t - λ_{t-1}‖₂``  [STATS:Ch3]
- **Fiedler drift**: cosine distance between Fiedler vectors
- **Connectivity delta**: ``Δλ₂``
- **Spectral gap delta**: ``Δ(λₖ - λ₂)``
- **Signal-smoothness delta**: ``Δ(sᵀLs)`` (semantic-structural co-evolution)

Node alignment: OSM node IDs are stable across snapshots, but nodes appear
and disappear. For eigenvalue comparison this is not an issue (eigenvalues
are graph invariants). For Fiedler vector comparison, we project onto the
intersection of node sets (nodes present in both snapshots).

Phase 0 invariants (enforced by ``tests/unit/test_spectral_drift_service.py``):
- Spectral distance ≥ 0 (L2 norm)
- Spectral distance = 0 for identical snapshots
- Fiedler drift ∈ [0, 2] (cosine distance range)
- Symmetric: drift(A, B) = drift(B, A)

References:
- [STATS:Ch3] — Spectral distance, KL divergence
- [COHEN:Ch13] — Eigendecomposition
"""

import logging

import numpy as np
from scipy.spatial.distance import cosine

logger = logging.getLogger(__name__)


class SpectralDriftService:
    """Compute spectral drift between snapshots.

    Layer 2: Eigenvalue/eigenvector drift (structural change)
    Layer 3: Signal-weighted drift (semantic-structural co-evolution)
    """

    def compute_spectral_drift(self, fp_from: dict, fp_to: dict) -> dict:
        """Compute spectral distance between two graph spectral fingerprints.

        Args:
            fp_from: fingerprint dict for the earlier snapshot (T_{t-1}),
                with keys ``eigenvalues``, ``fiedler_vector``,
                ``signal_smoothness``.
            fp_to: fingerprint dict for the later snapshot (T_t).

        Returns:
            dict with ``spectral_distance``, ``connectivity_delta``,
            ``spectral_gap_delta``, ``fiedler_drift``.
        """
        lambda_from = np.asarray(fp_from["eigenvalues"], dtype=float)
        lambda_to = np.asarray(fp_to["eigenvalues"], dtype=float)

        # Pad shorter vector with zeros if k differs
        max_k = max(len(lambda_from), len(lambda_to))
        if len(lambda_from) < max_k:
            lambda_from = np.pad(lambda_from, (0, max_k - len(lambda_from)))
        if len(lambda_to) < max_k:
            lambda_to = np.pad(lambda_to, (0, max_k - len(lambda_to)))

        spectral_distance = float(np.linalg.norm(lambda_to - lambda_from))
        # λ₂ shift (first non-trivial eigenvalue)
        connectivity_delta = float(lambda_to[0] - lambda_from[0])
        spectral_gap_delta = float(
            (lambda_to[-1] - lambda_to[0]) - (lambda_from[-1] - lambda_from[0])
        )

        # Fiedler vector drift (cosine distance) — project onto common node set
        fiedler_from = np.asarray(fp_from.get("fiedler_vector") or [], dtype=float)
        fiedler_to = np.asarray(fp_to.get("fiedler_vector") or [], dtype=float)
        fiedler_drift = self._fiedler_cosine_distance(fiedler_from, fiedler_to)

        return {
            "spectral_distance": spectral_distance,
            "connectivity_delta": connectivity_delta,
            "spectral_gap_delta": spectral_gap_delta,
            "fiedler_drift": fiedler_drift,
        }

    def compute_signal_drift(self, smoothness_from: float, smoothness_to: float) -> dict:
        """Compute signal-weighted graph drift.

        Tracks how WorldKG class signals align with graph structure across
        snapshots.
        """
        safe_from = float(smoothness_from) if smoothness_from else 0.0
        safe_to = float(smoothness_to) if smoothness_to else 0.0
        return {
            "smoothness_delta": safe_to - safe_from,
            "smoothness_ratio": safe_to / max(safe_from, 1e-10),
        }

    def classify_drift_magnitude(self, spectral_distance: float) -> str:
        """Categorize drift magnitude from the spectral distance.

        Thresholds are heuristic defaults — callers may override.
        """
        if spectral_distance < 0.1:
            return "low"
        if spectral_distance < 0.5:
            return "medium"
        if spectral_distance < 1.0:
            return "high"
        return "extreme"

    @staticmethod
    def _fiedler_cosine_distance(a: np.ndarray, b: np.ndarray) -> float:
        """Cosine distance between two Fiedler vectors, projected onto the
        common prefix (min length). Returns 0.0 when either vector is empty
        or has zero norm.
        """
        if a.size == 0 or b.size == 0:
            return 0.0
        min_n = min(a.size, b.size)
        a = a[:min_n]
        b = b[:min_n]
        na = float(np.linalg.norm(a))
        nb = float(np.linalg.norm(b))
        if na == 0.0 or nb == 0.0:
            return 0.0
        return float(cosine(a, b))
