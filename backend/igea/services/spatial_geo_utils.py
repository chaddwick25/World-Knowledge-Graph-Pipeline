"""Geohash / geo scoring helpers for USLP spatial link prediction.

Extracted from ``spatial_link_prediction.py`` (monolith split, Phase 5 of
PIPELINE_CONTROL_PLANE_AND_CLEANUP_PLAN).
"""

import logging
import numpy as np
import geohash2
from typing import Dict, List, Tuple

logger = logging.getLogger(__name__)

_GEOHASH_ALPHABET = "0123456789bcdefghjkmnpqrstuvwxyz"

# Fallback d_max values derived from geohash cell widths at the equator.
# Used only when the pool has not been loaded (e.g. unit tests) or a tail
# cluster is missing from _D_MAX_PER_CLUSTER (single-cluster precision
# level).  NOTE: these are NOT upper bounds on cluster-center distance —
# a pool spanning two cells already has centers up to ~2 cell-widths
# apart.  They are arbitrary scale constants that keep the score sane for
# degenerate pools (a single-cluster pool yields d ≈ 0 → sim ≈ 1.0
# regardless of the normalizer).
# Source: geohash cell dimensions — P1 ≈ 5000km, P3 ≈ 156km, P4 ≈ 39km.
_FALLBACK_D_MAX: Dict[int, float] = {1: 5000.0, 3: 156.0, 4: 39.0}


def _compute_d_max_per_precision(pool: List[Dict]) -> Dict[int, Dict[str, float]]:
    """Per-cluster max cluster-center distance, per precision level.

    Mirrors the paper's reference code (dataprep_utils.py:71-83 +
    fetch_entities_info.py:154-202) without materializing an N×N entity
    matrix.  For each precision level:
      1. Encode every candidate to a precision-P geohash.
      2. Take the unique cluster centers (decode each geohash to lat/lon).
      3. For each cluster center, compute the distance to its farthest
         peer (the per-column max of the K×K distance matrix; the matrix
         is symmetric, so row max == column max).

    K = number of unique clusters: at most 32 for P1, but potentially
    10^3–10^4 for P4 on a large pool (P4 cells are ~39km wide).  The
    pairwise pass is therefore vectorized numpy in row-blocks — O(K²)
    time, O(block × K) peak memory.  Precisions with a single cluster are
    omitted; lookups fall back to _FALLBACK_D_MAX (d ≈ 0 → sim ≈ 1.0).
    """
    if not pool:
        return {}
    result: Dict[int, Dict[str, float]] = {}
    coords = [(e['lat'], e['lon']) for e in pool]
    for precision in (1, 3, 4):
        # Unique cluster centers at this precision
        centers: Dict[str, Tuple[float, float]] = {}
        for lat, lon in coords:
            gh = geohash2.encode(lat, lon, precision=precision)
            if gh not in centers:
                lat_s, lon_s = geohash2.decode(gh)
                centers[gh] = (float(lat_s), float(lon_s))
        if len(centers) <= 1:
            continue
        ghs = list(centers.keys())
        pts = np.radians(np.array([centers[g] for g in ghs]))  # (K, 2)
        # Vectorized pairwise Haversine in row-blocks (bounds peak memory
        # to BLOCK × K instead of K² — matters when K ~ 10^4 at P4).
        per_cluster_max = np.zeros(len(ghs))
        BLOCK = 1024
        for start in range(0, len(ghs), BLOCK):
            b = pts[start:start + BLOCK]
            dlat = b[:, 0][:, None] - pts[:, 0][None, :]
            dlon = b[:, 1][:, None] - pts[:, 1][None, :]
            a = (np.sin(dlat / 2) ** 2
                 + np.cos(b[:, 0])[:, None]
                 * np.cos(pts[:, 0])[None, :]
                 * np.sin(dlon / 2) ** 2)
            dist = 6371.0 * 2 * np.arcsin(np.sqrt(np.clip(a, 0, 1)))
            per_cluster_max[start:start + BLOCK] = dist.max(axis=1)
        result[precision] = {
            gh: max(float(m), 1e-6)  # avoid div-by-zero
            for gh, m in zip(ghs, per_cluster_max)
        }
    return result


# ---------------------------------------------------------------------------
# Geohash helpers (no external dependency)
# ---------------------------------------------------------------------------

def _encode_geohash(lat: float, lon: float, precision: int) -> str:
    lat_range = [-90.0, 90.0]
    lon_range = [-180.0, 180.0]
    result, bits, bit_idx, char_val = [], 0, 0, 0
    is_lon = True
    while len(result) < precision:
        if is_lon:
            mid = (lon_range[0] + lon_range[1]) / 2
            if lon >= mid:
                char_val = (char_val << 1) | 1
                lon_range[0] = mid
            else:
                char_val <<= 1
                lon_range[1] = mid
        else:
            mid = (lat_range[0] + lat_range[1]) / 2
            if lat >= mid:
                char_val = (char_val << 1) | 1
                lat_range[0] = mid
            else:
                char_val <<= 1
                lat_range[1] = mid
        is_lon = not is_lon
        bits += 1
        if bits == 5:
            result.append(_GEOHASH_ALPHABET[char_val])
            bits = 0
            char_val = 0
    return "".join(result)


def _geohash_center(gh: str) -> Tuple[float, float]:
    lat_range = [-90.0, 90.0]
    lon_range = [-180.0, 180.0]
    is_lon = True
    for ch in gh:
        char_val = _GEOHASH_ALPHABET.index(ch)
        for bit in [16, 8, 4, 2, 1]:
            if is_lon:
                mid = (lon_range[0] + lon_range[1]) / 2
                if char_val & bit:
                    lon_range[0] = mid
                else:
                    lon_range[1] = mid
            else:
                mid = (lat_range[0] + lat_range[1]) / 2
                if char_val & bit:
                    lat_range[0] = mid
                else:
                    lat_range[1] = mid
            is_lon = not is_lon
    return (lat_range[0] + lat_range[1]) / 2, (lon_range[0] + lon_range[1]) / 2


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))
