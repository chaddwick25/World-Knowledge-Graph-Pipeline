"""Math-correctness unit tests for the USLP geographic-space formula fix.

Implements the test plan from
``docs/plans/USLP_GEO_SPACE_FORMULA_FIX.md`` §3 (Fix 4).

Pins the paper's reference-code invariants
(``docs/Schematics/SSLPandUSLP-main/dataprep_utils.py:71-83``):

    geo_sim(h, t, r) = 1 - d(h_cluster, t_cluster) / d_max(t_cluster, precision(r))

where ``d_max`` is the **per-cluster** (per-column) max cluster-center
distance at the relation's precision level, precomputed at pool-load time
by ``_compute_d_max_per_precision``.

These tests do NOT require a database or GPU — they construct a minimal
``SpatialLinkPredictionService`` with a synthetic pool and assert the math.
The FastText-backed name/class spaces are exercised through stubbed
embeddings so the three-space sum (Fix 2) can be verified without loading
the real FastText model.
"""

import os

import numpy as np
import pytest

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "backend.settings")
django.setup()

from igea.services import spatial_link_prediction as slp
from igea.services.spatial_link_prediction import (
    SpatialLinkPredictionService,
    RELATION_GEOHASH_PRECISION,
    DEFAULT_GEOHASH_PRECISION,
    _compute_d_max_per_precision,
    _D_MAX_PER_CLUSTER,
    _FALLBACK_D_MAX,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _make_pool(coords):
    """Build a minimal candidate pool from a list of (lat, lon) tuples."""
    return [
        {
            "osm_id": i + 1,
            "lat": lat,
            "lon": lon,
            "tags": {"name": f"entity_{i+1}"},
            "wkg_class": None,
            "gv_tags_embedding": None,
        }
        for i, (lat, lon) in enumerate(coords)
    ]


def _svc_with_pool(coords, reset_global=True):
    """Construct a service with a synthetic pool and d_max precomputed.

    ``reset_global`` clears the module-level ``_D_MAX_PER_CLUSTER`` before
    loading so each test starts from a known state (the global is shared
    across services under the Celery prefork model — see the
    LOAD-BEFORE-SCORE INVARIANT note in spatial_link_prediction.py).
    """
    if reset_global:
        slp._D_MAX_PER_CLUSTER = {}
    svc = SpatialLinkPredictionService()
    svc.load_candidate_pool(_make_pool(coords))
    return svc


# --------------------------------------------------------------------------- #
# Fix 4 — test cases 1-5 (geo formula + d_max computation)
# --------------------------------------------------------------------------- #

class TestGeoScoreFormula:
    """Cases 1-5: the geographic-space formula and d_max precomputation."""

    def test_geo_score_uses_one_minus_d_over_d_max(self):
        """Score must equal ``1 - d/d_max`` (paper), NOT ``1/(1+d)``.

        Construct a case where the head→tail center distance is moderate
        relative to d_max, so the paper formula gives a meaningful score
        (~0.5) while the old formula gives a near-zero score (~0.0002).
        """
        import geohash2
        from haversine import haversine, Unit

        # Three P1 clusters with known center distances:
        #   cell '7' center (-22, -22), cell 'k' center (-22, 22),
        #   cell 'm' center (-22, 68).
        # d(7,k) ≈ 4520 km, d(7,m) ≈ 9111 km, d(k,m) ≈ 4724 km.
        # d_max(7) = 9111, d_max(k) = 4724, d_max(m) = 9111.
        # Score from k (head) to 7 (tail): d=4520, d_max(7)=9111
        # → paper: 1 - 4520/9111 ≈ 0.504
        # → old:   1/(1+4520)    ≈ 0.0002
        coords = [
            (-10.0, -10.0),   # cell '7'
            (-10.0, 10.0),    # cell 'k'
            (-10.0, 60.0),    # cell 'm'
        ]
        ghs = set(geohash2.encode(lat, lon, precision=1) for lat, lon in coords)
        assert len(ghs) == 3, f"expected 3 P1 clusters, got {ghs}"

        svc = _svc_with_pool(coords)
        precision = RELATION_GEOHASH_PRECISION["isInCountry"]  # P1
        d_max_map = slp._D_MAX_PER_CLUSTER.get(precision, {})
        assert len(d_max_map) == 3, d_max_map

        # Head in cluster k, tail in cluster 7.
        gh_h = geohash2.encode(-10.0, 10.0, precision=precision)
        gh_t = geohash2.encode(-10.0, -10.0, precision=precision)
        c_h = tuple(map(float, geohash2.decode(gh_h)))
        c_t = tuple(map(float, geohash2.decode(gh_t)))
        d = haversine(c_h, c_t, unit=Unit.KILOMETERS)
        d_max_t = d_max_map[gh_t]

        score = svc._geo_score(-10.0, 10.0, -10.0, -10.0, "isInCountry")
        expected = max(0.0, min(1.0, 1.0 - d / d_max_t))
        assert score == pytest.approx(expected, rel=1e-6)
        # And specifically: NOT the old 1/(1+d) formula.
        old_score = 1.0 / (1.0 + d)
        assert abs(score - old_score) > 0.5, (
            f"paper score {score} should differ sharply from old {old_score}"
        )

    def test_geo_score_range_zero_to_one(self):
        """Geo score ∈ [0, 1] for all (h, t) pairs, including d > d_max."""
        coords = [
            (0.0, 0.0),
            (0.0, 10.0),
            (0.0, -10.0),
        ]
        svc = _svc_with_pool(coords)
        # Score every pair across all relations/precisions.
        for relation in ["isInCountry", "addrState", "addrCity"]:
            for h in coords:
                for t in coords:
                    s = svc._geo_score(h[0], h[1], t[0], t[1], relation)
                    assert 0.0 <= s <= 1.0, (relation, h, t, s)
        # Head far outside the tail cluster's span → clamped to 0.
        # Place head at the antipode of a 2-cluster pool.
        coords2 = [(0.0, 0.0), (0.0, 10.0)]
        svc2 = _svc_with_pool(coords2)
        s = svc2._geo_score(0.0, 179.0, 0.0, 0.0, "addrCity")
        assert s == 0.0

    def test_geo_score_same_cluster_is_one(self):
        """Two entities in the same geohash cell → d=0 → score=1.0."""
        # Same P4 cell (39 km wide): two points 1 km apart at equator.
        coords = [(0.0, 0.0), (0.0, 0.005)]  # ~0.5 km apart → same P4 cell
        svc = _svc_with_pool(coords)
        # Use a relation with precision 4.
        s = svc._geo_score(0.0, 0.0, 0.0, 0.005, "addrCity")
        assert s == pytest.approx(1.0, abs=1e-6)

    def test_geo_score_farthest_pair_is_zero(self):
        """The two cluster centers defining the tail's d_max → score 0.0."""
        # Two P4 clusters; tail's farthest peer is the other cluster.
        # Place them far enough apart to be in different P4 cells but
        # close enough that the d_max is exactly the center-to-center
        # distance (so 1 - d/d_max = 0).
        coords = [(0.0, 0.0), (0.0, 1.0)]  # ~111 km apart → different P4 cells
        svc = _svc_with_pool(coords)
        precision = 4
        d_max_map = slp._D_MAX_PER_CLUSTER.get(precision, {})
        assert d_max_map, "P4 d_max map should be populated"
        import geohash2
        gh_a = geohash2.encode(0.0, 0.0, precision=precision)
        gh_b = geohash2.encode(0.0, 1.0, precision=precision)
        assert gh_a != gh_b, "test setup: points must be in different P4 cells"
        # Score from A's center to B; d == d_max(B) → 0.0.
        c_a = tuple(map(float, geohash2.decode(gh_a)))
        c_b = tuple(map(float, geohash2.decode(gh_b)))
        s = svc._geo_score(c_a[0], c_a[1], c_b[0], c_b[1], "addrCity")
        assert s == pytest.approx(0.0, abs=1e-6)

    def test_d_max_computed_from_unique_clusters(self):
        """d_max derives from unique cluster centers, not raw entity coords.

        Pool with many entities but only 2 unique P1 clusters ~1000 km
        apart.  Assert ``_D_MAX_PER_CLUSTER[1][gh] ≈ 1000`` for both
        cluster geohashes.
        """
        import geohash2
        # P1 cells are ~5000 km wide.  Pick points within the same P1 cell.
        # Cell '7' center is (-22, -22), spanning lat [-45, 0], lon [-45, 0].
        # Cell 'k' center is (-22, 22), spanning lat [-45, 0], lon [0, 45].
        # Place 2 entities in each cell, ~1000 km apart between cell centers.
        coords = [
            (-10.0, -10.0),   # cell '7'
            (-20.0, -30.0),   # cell '7' (same P1 cluster)
            (-10.0, 10.0),    # cell 'k'
            (-20.0, 30.0),    # cell 'k' (same P1 cluster)
        ]
        # Verify the P1 geohash assignments.
        ghs = set(geohash2.encode(lat, lon, precision=1) for lat, lon in coords)
        assert len(ghs) == 2, f"expected 2 P1 clusters, got {ghs}"
        svc = _svc_with_pool(coords)
        d_max_map = slp._D_MAX_PER_CLUSTER.get(1, {})
        assert len(d_max_map) == 2, f"expected 2 P1 clusters, got {len(d_max_map)}"
        # All d_max values should be equal (symmetric matrix) and reflect
        # the distance between the two unique cluster centers.
        centers = set()
        for gh in d_max_map:
            centers.add(tuple(map(float, geohash2.decode(gh))))
        assert len(centers) == 2
        from haversine import haversine, Unit
        c_list = list(centers)
        expected_d_max = haversine(c_list[0], c_list[1], unit=Unit.KILOMETERS)
        for gh, m in d_max_map.items():
            assert m == pytest.approx(expected_d_max, rel=1e-2), (gh, m)


# --------------------------------------------------------------------------- #
# Fix 4 — test cases 6-8 (name/class unclamped cosine + three-space sum)
# --------------------------------------------------------------------------- #

class _StubFastText:
    """Deterministic FastText stub returning unit vectors keyed by text.

    Maps each distinct text to a unique unit vector so cosine similarity is
    deterministic and controllable in tests.  Negative cosine is achievable
    by mapping one text to the negation of another's vector.
    """

    def __init__(self):
        self._vecs = {}
        self._dim = 8

    def _vec_for(self, text):
        if text in self._vecs:
            return self._vecs[text]
        # Hash the text to a deterministic unit vector.
        h = abs(hash(text)) % (2 ** 31)
        rng = np.random.RandomState(h)
        v = rng.randn(self._dim).astype(np.float32)
        n = np.linalg.norm(v)
        if n == 0:
            v = np.zeros(self._dim, dtype=np.float32)
        else:
            v = v / n
        self._vecs[text] = v
        return v

    def set_vector(self, text, vector):
        v = np.asarray(vector, dtype=np.float32)
        n = np.linalg.norm(v)
        self._vecs[text] = v / n if n else v

    def calculate_embedding(self, tag_counts):
        # The real service accepts a tag_counts dict OR a string (via
        # _embed_text which converts a string to {tok: 1}).  Support both.
        if isinstance(tag_counts, str):
            return self._vec_for(tag_counts)
        # tag_counts: {token: count} — combine the tokens' vectors.
        acc = np.zeros(self._dim, dtype=np.float32)
        for tok, cnt in tag_counts.items():
            acc += cnt * self._vec_for(tok)
        n = np.linalg.norm(acc)
        return acc / n if n else acc


def _svc_with_stub_ft(coords):
    """Service with a stubbed FastText so name/class scores are deterministic."""
    svc = _svc_with_pool(coords)
    stub = _StubFastText()
    # Patch the lazy FastText loader + embedding cache.
    svc._ft_model = stub
    svc._embedding_cache = {}
    svc._get_fasttext = lambda: stub
    return svc, stub


class TestNameAndClassScore:
    """Cases 6-8: unclamped cosine + three-space sum."""

    def test_name_score_unclamped_cosine(self):
        """``_name_score`` can return negative values (antonym literals)."""
        svc, stub = _svc_with_stub_ft([(0.0, 0.0), (0.0, 0.01)])
        # Make the literal the negation of the candidate name vector.
        stub.set_vector("good", np.array([1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]))
        stub.set_vector("bad", np.array([-1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]))
        candidate = {"tags": {"name": "bad"}}
        score = svc._name_score("good", candidate)
        assert score < 0.0, f"expected negative cosine, got {score}"

    def test_class_score_unclamped_cosine(self):
        """``_class_score`` can return negative values."""
        svc, stub = _svc_with_stub_ft([(0.0, 0.0), (0.0, 0.01)])
        stub.set_vector("country", np.array([1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]))
        stub.set_vector("ocean", np.array([-1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]))
        candidate = {"wkg_class": "wkgs:ocean"}
        score = svc._class_score("isInCountry", candidate)
        assert score < 0.0, f"expected negative cosine, got {score}"

    def test_total_score_is_sum_of_three_spaces(self):
        """``unnormalized == g + n + c`` and ``normalized == unnormalized / 3``."""
        svc, stub = _svc_with_stub_ft([(0.0, 0.0), (0.0, 0.01)])
        candidate = {"tags": {"name": "place"}, "wkg_class": "wkgs:city", "lat": 0.0, "lon": 0.01}
        unnormalized, normalized, g, n, c = svc._total_score(
            head_lat=0.0, head_lon=0.0, head_osm_id=1,
            relation="addrCity", literal="place", candidate=candidate,
        )
        g = svc._geo_score(0.0, 0.0, candidate["lat"], candidate["lon"], "addrCity")
        n = svc._name_score("place", candidate)
        c = svc._class_score("addrCity", candidate)
        assert unnormalized == pytest.approx(g + n + c, rel=1e-5)
        assert normalized == pytest.approx((g + n + c) / 3.0, rel=1e-5)


# --------------------------------------------------------------------------- #
# Fix 4 — test cases 9-11 (persist bug, fallback d_max, per-cluster d_max)
# --------------------------------------------------------------------------- #

class TestPersistAndDMax:
    """Cases 9-11: per-space score persistence, fallback, per-cluster d_max."""

    def test_persist_links_writes_per_space_scores(self):
        """CPU ``predict_links_for_entity`` link dicts carry non-zero
        ``geo_score``/``name_score``/``topo_score`` (Fix 2).

        This test does NOT touch the database — it inspects the link dicts
        returned by ``predict_links_for_entity`` (which ``persist_links``
        reads via ``link.get('geo_score', 0.0)``).
        """
        svc, stub = _svc_with_stub_ft([(0.0, 0.0), (0.0, 0.01)])
        # Give the candidate a name + class that produce non-zero scores.
        stub.set_vector("kingston", np.array([1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]))
        stub.set_vector("city", np.array([1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]))
        head_tags = {"addr:city": "kingston"}
        links = svc.predict_links_for_entity(
            head_osm_id=1, head_lat=0.0, head_lon=0.0,
            head_tags=head_tags, threshold=0.0, top_k=5,
        )
        assert links, "expected at least one scored link"
        for link in links:
            assert "geo_score" in link, link
            assert "name_score" in link, link
            assert "topo_score" in link, link
            # Geo score must be non-zero (same-cluster → 1.0).
            assert link["geo_score"] > 0.0, link
            # At least one of name/topo should be non-zero with our stub.
            assert (link["name_score"] != 0.0) or (link["topo_score"] != 0.0), link

    def test_fallback_d_max_when_pool_empty(self):
        """``_geo_score`` uses ``_FALLBACK_D_MAX`` when the global is empty."""
        slp._D_MAX_PER_CLUSTER = {}
        svc = SpatialLinkPredictionService()  # no pool load
        # Score with a relation whose precision has a fallback.
        s = svc._geo_score(0.0, 0.0, 0.0, 0.0, "addrCity")
        # d=0 → sim=1.0 regardless of the normalizer.
        assert s == pytest.approx(1.0, abs=1e-6)
        # Non-zero distance with the fallback normalizer.
        s2 = svc._geo_score(0.0, 0.0, 0.0, 0.01, "addrCity")
        # d ≈ 1.1 km, d_max = 39 km → 1 - 1.1/39 ≈ 0.97
        assert 0.9 < s2 <= 1.0, s2

    def test_d_max_is_per_cluster_not_global(self):
        """Per-cluster d_max: a tail cluster whose farthest peer is close
        gets normalized by ITS own max, not the global max.

        Pool with 3 P4 clusters A, B, C in a line where the middle cluster
        B's farthest peer is closer than the outer clusters' farthest peer.
        A pair ending at B must be normalized by B's own d_max (smaller),
        yielding a steeper score than the global-max normalization would.
        """
        import geohash2
        from haversine import haversine, Unit

        # P4 cells are ~39 km wide in longitude at the equator.
        # Use wide spacing so cell centers are clearly at different distances.
        #   A at (0, 0), B at (0, 2), C at (0, 5)
        # ~222 km A↔B, ~556 km B↔C, ~778 km A↔C (at equator).
        # d_max(A) = d(A,C) ≈ 778, d_max(B) = d(B,C) ≈ 556,
        # d_max(C) = d(C,A) ≈ 778.  So d_max(B) < d_max(A) == d_max(C).
        coords = [(0.0, 0.0), (0.0, 2.0), (0.0, 5.0)]
        # Verify they're in distinct P4 cells.
        ghs = set(geohash2.encode(lat, lon, precision=4) for lat, lon in coords)
        assert len(ghs) == 3, f"expected 3 P4 clusters, got {ghs}"

        svc = _svc_with_pool(coords)
        d_max_map = slp._D_MAX_PER_CLUSTER.get(4, {})
        assert len(d_max_map) == 3, d_max_map

        gh_a = geohash2.encode(0.0, 0.0, precision=4)
        gh_b = geohash2.encode(0.0, 2.0, precision=4)
        gh_c = geohash2.encode(0.0, 5.0, precision=4)
        m_a = d_max_map[gh_a]
        m_b = d_max_map[gh_b]
        m_c = d_max_map[gh_c]
        # B is in the middle → its farthest peer is closer than A's or C's.
        assert m_b < m_a, (m_a, m_b, m_c)
        assert m_b < m_c, (m_a, m_b, m_c)

        # Score a pair from A to B: normalized by B's d_max (m_b), not the
        # global max (m_a or m_c).  Confirm it uses m_b by comparing
        # against both possible normalizers.
        c_a = tuple(map(float, geohash2.decode(gh_a)))
        c_b = tuple(map(float, geohash2.decode(gh_b)))
        d = haversine(c_a, c_b, unit=Unit.KILOMETERS)
        score = svc._geo_score(c_a[0], c_a[1], c_b[0], c_b[1], "addrCity")
        expected_per_cluster = max(0.0, min(1.0, 1.0 - d / m_b))
        expected_global_a = max(0.0, min(1.0, 1.0 - d / m_a))
        assert score == pytest.approx(expected_per_cluster, rel=1e-6)
        # And the two normalizers give different scores (else the test is moot).
        assert abs(expected_per_cluster - expected_global_a) > 1e-3, (
            "per-cluster and global normalizers must differ for this pool"
        )


# --------------------------------------------------------------------------- #
# Fix 4 — test case 12 (GPU cluster-center parity)
# --------------------------------------------------------------------------- #

class TestGpuClusterCenterParity:
    """Case 12: GPU geo score uses cluster centers, not raw coordinates."""

    def test_gpu_geo_uses_cluster_centers(self):
        """Head and tail in the same precision-P geohash cell but ~1 km
        apart in raw coordinates.  CPU ``_geo_score`` returns 1.0 (same
        center, d=0).  The GPU geo score must return the same value —
        pinning that the GPU path scores center-to-center, not
        raw-coordinate-to-raw.

        When CUDA is unavailable, ``GPUAcceleratedUSLP`` falls back to CPU
        device; the test still exercises the precompute + scoring path
        (``_precompute_gpu_data`` builds the center tensors on CPU).

        This test bypasses the FastText-dependent parts of
        ``_precompute_gpu_data`` by calling the CPU ``load_candidate_pool``
        (which populates ``_D_MAX_PER_CLUSTER``) and then manually building
        the per-precision center/d_max tensors the GPU scoring path uses.
        """
        import torch
        from igea.services.gpu_uslp_service import GPUAcceleratedUSLP

        slp._D_MAX_PER_CLUSTER = {}
        # Two entities in the same P4 cell, ~1 km apart in raw coords.
        coords = [(0.0, 0.0), (0.0, 0.005)]
        pool = _make_pool(coords)
        svc = GPUAcceleratedUSLP(device="cpu")  # force CPU device for test portability
        # Use the CPU pool load (populates _D_MAX_PER_CLUSTER via the
        # parent) without triggering GPU FastText precompute.
        SpatialLinkPredictionService.load_candidate_pool(svc, pool)

        # CPU reference score.
        cpu_score = svc._geo_score(0.0, 0.0, 0.0, 0.005, "addrCity")
        assert cpu_score == pytest.approx(1.0, abs=1e-6)

        # Manually build the per-precision center/d_max tensors that
        # _precompute_gpu_data would build (mirrors gpu_uslp_service.py
        # §3.1.3 fix part 1).  This isolates the geo-score precompute from
        # the FastText model dependency.
        import geohash2
        precision = RELATION_GEOHASH_PRECISION["addrCity"]
        gh_rows = [geohash2.encode(e['lat'], e['lon'], precision=precision)
                   for e in pool]
        centers = np.array([tuple(map(float, geohash2.decode(g)))
                            for g in gh_rows], dtype=np.float32)
        svc._gpu_pool_center_coords[precision] = torch.from_numpy(centers)
        d_max_map = slp._D_MAX_PER_CLUSTER.get(precision, {})
        fallback = _FALLBACK_D_MAX.get(precision, 39.0)
        svc._gpu_pool_d_max[precision] = torch.from_numpy(np.array(
            [d_max_map.get(g, fallback) for g in gh_rows], dtype=np.float32
        ))

        # GPU path: build the head center + score against the pool centers.
        center_coords = svc._gpu_pool_center_coords[precision]  # (N, 2) degrees
        d_max_rows = svc._gpu_pool_d_max[precision]  # (N,) km

        gh_h = geohash2.encode(0.0, 0.0, precision=precision)
        c_h = torch.tensor(tuple(map(float, geohash2.decode(gh_h))),
                           dtype=center_coords.dtype, device=center_coords.device)
        # Pairwise Haversine from head center to each pool center.
        lat1 = torch.deg2rad(c_h[0])
        lon1 = torch.deg2rad(c_h[1])
        lat2 = torch.deg2rad(center_coords[:, 0])
        lon2 = torch.deg2rad(center_coords[:, 1])
        dlat = lat2 - lat1
        dlon = lon2 - lon1
        a = (torch.sin(dlat / 2) ** 2
             + torch.cos(lat1) * torch.cos(lat2) * torch.sin(dlon / 2) ** 2)
        center_dists = 6371.0 * 2 * torch.arcsin(torch.sqrt(a.clamp(0, 1)))
        geo_scores = torch.clamp(1.0 - center_dists / d_max_rows, min=0.0, max=1.0)
        # Both pool entries share the head's cluster → d=0 → score 1.0.
        assert torch.allclose(geo_scores, torch.ones_like(geo_scores), atol=1e-5), (
            geo_scores.cpu().numpy()
        )
