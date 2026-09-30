"""Math-correctness tests for the GeoVectors IDW damped-weight formulas.

Pins the two formulas identified in ``docs/issues/ENCODER_MATH_REVIEW.md``
against their reference implementations:

Training (paper §3.2, ``WeightedDeepWalkGraph._damp_and_row_norm``):
    w'(d) = max(1/ln(max(d, 1.1)), e)
    → ``KNNGraphService.calculate_edge_weight`` and
      ``WeightedDeepWalkService.apply_damped_weights``

Inference (paper §3.4, ``NLEModel.encode_coords``):
    w_enc(o, oj) = ln(1 + 1/d_km)   — pinned here for contrast; NOT the
    training formula, and the two are not interchangeable.

Also verifies the double-damping guard: re-applying the training formula to
already-damped weights (range [e, ~10.5]) collapses every weight to e, which
``apply_damped_weights`` now rejects with a ValueError.

These tests are pure math — no database or GPU required.
"""

import math

import numpy as np
import pytest

from semantic_search.services.deepwalk_service import WeightedDeepWalkService
from semantic_search.services.knn_graph_service import KNNGraphService


E = math.e
MAX_WEIGHT = 1.0 / math.log(1.1)       # ≈ 10.492 — weight of any pair ≤ 1.1 km apart
FLOOR_THRESHOLD_KM = math.exp(1.0 / E)  # ≈ 1.4447 — beyond this, ln(d) ≥ 1 → floor at e


def ref_training_weight(dist_km):
    """Reference implementation: max(1/ln(max(d, 1.1)), e)."""
    return max(1.0 / math.log(max(dist_km, 1.1)), E)


class TestCalculateEdgeWeight:
    """``KNNGraphService.calculate_edge_weight`` implements the §3.2 training formula."""

    def test_floors_at_e_for_distant_neighbors(self):
        # Any pair ≥ e^(1/e) ≈ 1.44 km apart has ln(d) ≥ 1 → exactly the e floor.
        for d in [1.5, 2.0, 5.0, 50.0, 1000.0]:
            assert KNNGraphService.calculate_edge_weight(d) == pytest.approx(E, rel=1e-12), d

    def test_maximum_weight_at_1_1_km_clamp(self):
        # d ≤ 1.1 → 1/ln(1.1) ≈ 10.492 for every sub-1.1 km pair.
        assert KNNGraphService.calculate_edge_weight(1.1) == pytest.approx(MAX_WEIGHT, rel=1e-12)
        assert KNNGraphService.calculate_edge_weight(0.5) == pytest.approx(MAX_WEIGHT, rel=1e-12)
        assert KNNGraphService.calculate_edge_weight(0.0) == pytest.approx(MAX_WEIGHT, rel=1e-12)

    def test_matches_reference_formula(self):
        # Sweep both sides of the 1.1 km clamp and the e^(1/e) floor threshold.
        for d in [0.0, 0.3, 1.0, 1.1, 1.2, 1.44, 1.45, 3.0, 27.0]:
            got = KNNGraphService.calculate_edge_weight(d)
            assert got == pytest.approx(ref_training_weight(d), rel=1e-12), d

    def test_monotonically_decreasing(self):
        ds = np.linspace(0.0, 50.0, 200)
        ws = [KNNGraphService.calculate_edge_weight(float(d)) for d in ds]
        # Non-increasing: flat at the clamp, then decreasing, flat again at the floor.
        assert all(b <= a for a, b in zip(ws, ws[1:]))

    def test_weight_range_is_e_to_1_over_ln_1_1(self):
        for d in np.linspace(0.0, 100.0, 500):
            w = KNNGraphService.calculate_edge_weight(float(d))
            assert E - 1e-12 <= w <= MAX_WEIGHT + 1e-12, d


class TestApplyDampedWeights:
    """``apply_damped_weights`` on raw-distance graphs + the double-damping guard."""

    def _service(self):
        return WeightedDeepWalkService(apply_damping=False)

    def test_raises_on_pre_damped_graph(self):
        # Minimum weight sits exactly at the e floor — the signature of
        # KNNGraphService output (every pair > 1.44 km apart is floored to e).
        graph = {1: [(2, E), (3, 4.0)], 2: [(1, E)]}
        with pytest.raises(ValueError, match="pre-damped"):
            self._service().apply_damped_weights(graph)

    def test_full_scan_catches_floor_buried_mid_graph(self):
        # All weights ≥ e (pre-damped signature) with the single floored edge
        # buried at node 50 — beyond a 20-node prefix sample. Pins that the
        # guard scans the whole graph, not just the first nodes.
        graph = {i: [(i + 1, 4.0)] for i in range(1, 100)}
        graph[50] = [(51, E)]  # the only floored edge, mid-graph
        with pytest.raises(ValueError, match="pre-damped"):
            self._service().apply_damped_weights(graph)

    def test_accepts_raw_distance_graph(self):
        graph = {1: [(2, 0.5), (3, 3.0)], 2: [(1, 0.5)], 3: [(1, 3.0)]}
        out = self._service().apply_damped_weights(graph)
        assert out[1][0][1] == pytest.approx(MAX_WEIGHT, rel=1e-12)  # 0.5 km → clamp
        assert out[1][1][1] == pytest.approx(E, rel=1e-12)           # 3.0 km → floor

    def test_matches_reference_formula_edge_by_edge(self):
        raw = {1: [(2, 0.0), (3, 0.9), (4, 1.1), (5, 1.3), (6, 2.0), (7, 25.0)]}
        out = self._service().apply_damped_weights(raw)
        for (_, w), (_, d) in zip(out[1], raw[1]):
            assert w == pytest.approx(ref_training_weight(d), rel=1e-12), d

    def test_zero_weight_is_clamped_not_floored(self):
        # d = 0 km (identical coordinates) → max weight, matching the reference
        # clamp semantics. (The previous +1e-10 epsilon form floored it to e —
        # inverting the intent for the closest neighbors.)
        out = self._service().apply_damped_weights({7: [(8, 0.0)]})
        assert out[7][0][1] == pytest.approx(MAX_WEIGHT, rel=1e-12)

    def test_empty_graph_and_isolated_nodes(self):
        assert self._service().apply_damped_weights({}) == {}
        out = self._service().apply_damped_weights({1: [], 2: [(1, 5.0)]})
        assert out[1] == []
        assert out[2][0][1] == pytest.approx(E, rel=1e-12)

    def test_double_damping_collapses_to_e(self):
        # Demonstrate the failure the guard prevents: re-applying the training
        # formula to already-damped weights maps every weight to e.
        # Include one pair > 1.44 km apart so the once-damped graph carries a
        # floored edge (min == e) — the signature the guard detects.
        raw = {1: [(2, 0.5), (3, 5.0)]}
        once = self._service().apply_damped_weights(raw)
        assert all(E - 1e-12 <= w <= MAX_WEIGHT + 1e-12 for _, w in once[1])
        assert min(w for _, w in once[1]) == pytest.approx(E, rel=1e-12)
        # The guard refuses the second application...
        with pytest.raises(ValueError, match="pre-damped"):
            self._service().apply_damped_weights(once)
        # ...and the math confirms why: max(1/ln(max(w,1.1)), e) = e for all w ≥ e.
        for _, w in once[1]:
            assert ref_training_weight(w) == pytest.approx(E, rel=1e-12)

    def test_guard_detection_boundary_dense_cluster(self):
        # Documented limitation: a pre-damped graph whose weights are ALL in
        # (e, ~10.5] — every pair between 1.1 and 1.44 km apart, no floored
        # edge — is indistinguishable from a raw-distance graph by the min
        # signature, so the guard cannot fire. The collapse math still holds.
        pre_damped_dense = {1: [(2, 6.0), (3, 8.0)]}
        out = self._service().apply_damped_weights(pre_damped_dense)  # no raise
        for _, w in out[1]:
            assert ref_training_weight(w) == pytest.approx(E, rel=1e-12)


class TestInferenceFormulaContrast:
    """The §3.4 inference formula ``ln(1 + 1/d_km)`` — pinned for contrast.

    ``NLEModel.encode_coords`` weights the 50 nearest neighbor embeddings by
    ``np.log(1 + (1 / (d_m / 1000)))`` (see geovectors_encoder/core/models/
    nle.py).  Unlike the training formula it has no e floor: the weight is a
    relative contribution to a normalized sum, so the floor is unnecessary.
    """

    def test_ln_1_plus_inverse_is_relative_weight(self):
        assert math.log(1 + 1 / 0.5) == pytest.approx(math.log(3.0))
        assert math.log(1 + 1 / 1.0) == pytest.approx(math.log(2.0))
        # No floor: a 1 km neighbor contributes ln(2) ≈ 0.69, below e — this is
        # fine for a weighted mean, but would break a transition matrix.
        assert math.log(1 + 1 / 1.0) < E

    def test_inference_formula_is_not_the_training_formula(self):
        # The same distance produces different weights in the two formulas
        # (except at isolated coincidence points) — they are not interchangeable.
        for d in [0.5, 1.0, 2.0, 10.0]:
            inf = math.log(1 + 1 / d)
            trn = ref_training_weight(d)
            assert inf != pytest.approx(trn), d
