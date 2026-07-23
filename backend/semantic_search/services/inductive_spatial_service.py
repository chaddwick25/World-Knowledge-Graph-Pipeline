"""
Inductive Spatial Embedding Service

Produces provisional GV-NLE embeddings for entities not seen during DeepWalk training,
using the USLP inductive principle from:
    Mann, Dsouza, Yu, Demidova — "Spatial Link Prediction with Spatial and Semantic
    Embeddings", ISWC 2023.

Core idea: new entities inherit a spatially coherent embedding by taking the
proximity-weighted mean of the k=50 nearest *already-embedded* entities' GV-NLE
vectors, using the same damped-weight formula as the original GeoVectors paper.

GV-NLE inductive formula (derived from GeoVectors Section 3.2):
    GV-NLE_inductive(o) = Σ w(o,oᵢ)·GV-NLE(oᵢ) / Σ w(o,oᵢ)
    where w(o,oᵢ) = max(1/ln(dist_km(o,oᵢ)), e)

This lets every new temporal-snapshot entity get a spatial embedding immediately,
without full graph retraining.  Full DeepWalk retraining remains the authoritative
path and should be scheduled periodically (monthly).
"""

import logging
import numpy as np
from typing import Optional, List, Tuple, Dict
from haversine import haversine, Unit

logger = logging.getLogger(__name__)

_GEOHASH_ALPHABET = "0123456789bcdefghjkmnpqrstuvwxyz"
_GEOHASH_BITS = [
    16, 8, 4, 2, 1,
    16, 8, 4, 2, 1,
    16, 8, 4, 2, 1,
]


# ---------------------------------------------------------------------------
# Minimal geohash encoder (no external dependency)
# ---------------------------------------------------------------------------

def _encode_geohash(lat: float, lon: float, precision: int = 6) -> str:
    """Encode lat/lon to geohash string of given precision."""
    lat_range = [-90.0, 90.0]
    lon_range = [-180.0, 180.0]
    result = []
    bits = 0
    bit_idx = 0
    char_val = 0
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
    """Return (lat, lon) center of a geohash cell."""
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
    lat = (lat_range[0] + lat_range[1]) / 2
    lon = (lon_range[0] + lon_range[1]) / 2
    return lat, lon


# ---------------------------------------------------------------------------
# Edge weight (GeoVectors paper formula)
# ---------------------------------------------------------------------------

def _damped_weight(dist_km: float) -> float:
    """w' = max(1/ln(max(d, 1.1)), e)  —  GeoVectors training formula."""
    d = max(dist_km, 1.1)
    return max(1.0 / np.log(d), np.e)


# ---------------------------------------------------------------------------
# Main service
# ---------------------------------------------------------------------------

class InductiveSpatialService:
    """
    Computes provisional GV-NLE embeddings for unseen entities (inductive path).

    Usage:
        service = InductiveSpatialService(k=50)
        service.load_pool_from_db()                        # or load_pool(entities)
        emb = service.embed_entity(lat=51.5, lon=-0.1)    # London
    """

    def __init__(self, k: int = 50):
        """
        Args:
            k: Number of nearest pool entities to aggregate (paper default: 50).
        """
        self.k = k
        self._pool_coords: Optional[np.ndarray] = None   # shape (N, 2) [lat, lon]
        self._pool_embeddings: Optional[np.ndarray] = None  # shape (N, 300)
        self._pool_osm_ids: Optional[List[int]] = None
        self._pool_size: int = 0
        self._tree = None

    # ------------------------------------------------------------------
    # Pool loading
    # ------------------------------------------------------------------

    def load_pool(self, entities: List[Dict]) -> int:
        """
        Load embedding pool from a list of dicts.

        Args:
            entities: list of {osm_id, lat, lon, gv_nle_embedding (list[float])}

        Returns:
            Number of entities loaded into the pool.
        """
        valid = [e for e in entities
                 if e.get('gv_nle_embedding') is not None
                 and e.get('lat') is not None
                 and e.get('lon') is not None]

        if not valid:
            logger.warning("InductiveSpatialService: no valid pool entities provided")
            self._pool_size = 0
            return 0

        self._pool_osm_ids = [e['osm_id'] for e in valid]
        self._pool_coords = np.array([[e['lat'], e['lon']] for e in valid], dtype=np.float64)
        self._pool_embeddings = np.array([e['gv_nle_embedding'] for e in valid], dtype=np.float32)
        self._pool_size = len(valid)
        
        # Build ultra-fast BallTree for k-NN queries (requires radians)
        from sklearn.neighbors import BallTree
        logger.info(f"InductiveSpatialService: Building BallTree index for {self._pool_size} entities...")
        self._tree = BallTree(np.radians(self._pool_coords), metric='haversine')
        
        logger.info(f"InductiveSpatialService: pool loaded with {self._pool_size} entities")
        return self._pool_size

    def load_pool_from_db(self, region_filter: Optional[Dict] = None, limit: int = 500_000) -> int:
        """
        Load embedding pool directly from the pgvector DB.

        Args:
            region_filter: Optional ORM filter kwargs applied to OsmEntity queryset.
            limit:         Maximum entities to load (memory safety cap).

        Returns:
            Number of entities loaded.
        """
        from worldkg_nca.models import OsmEntity

        qs = OsmEntity.objects.using('vectors').filter(
            gv_nle_embedding__isnull=False,
            geom__isnull=False,
        )
        if region_filter:
            qs = qs.filter(**region_filter)

        qs = qs[:limit]

        entities = []
        for e in qs.iterator(chunk_size=10_000):
            entities.append({
                'osm_id': e.osm_id,
                'lat': e.geom.y,
                'lon': e.geom.x,
                'gv_nle_embedding': list(e.gv_nle_embedding),
            })

        return self.load_pool(entities)

    # ------------------------------------------------------------------
    # Embedding inference
    # ------------------------------------------------------------------

    def embed_entity(self, lat: float, lon: float) -> Optional[np.ndarray]:
        """
        Compute inductive GV-NLE embedding for a new entity at (lat, lon).

        Returns proximity-weighted mean of the k nearest pool embeddings.
        Returns None if the pool is empty.

        Args:
            lat: WGS84 latitude
            lon: WGS84 longitude

        Returns:
            300-dim numpy float32 array, or None.
        """
        if self._pool_size == 0:
            logger.warning("InductiveSpatialService.embed_entity: pool is empty")
            return None

        coord = (lat, lon)
        k = min(self.k, self._pool_size)

        # Vectorized query: returns (distances in radians, indices)
        coord_rad = np.radians([[lat, lon]])
        dists_rad, indices = self._tree.query(coord_rad, k=k)
        
        # Convert haversine radians back to kilometers (Earth radius ~ 6371.0)
        nearest_dists = dists_rad[0] * 6371.0
        nearest_idx = indices[0]

        # Damped weights
        weights = np.array([_damped_weight(d) for d in nearest_dists], dtype=np.float32)
        weights /= weights.sum()

        # Weighted mean
        embedding = (self._pool_embeddings[nearest_idx] * weights[:, None]).sum(axis=0)

        # L2-normalise (consistent with GV-NLE training)
        norm = np.linalg.norm(embedding)
        if norm > 0:
            embedding /= norm

        return embedding

    def embed_batch(self, coords: List[Tuple[float, float]]) -> List[Optional[np.ndarray]]:
        """
        Compute inductive GV-NLE embeddings for a list of (lat, lon) pairs.

        Args:
            coords: list of (lat, lon) tuples

        Returns:
            List of 300-dim arrays (or None for failures).
        """
        return [self.embed_entity(lat, lon) for lat, lon in coords]

    # ------------------------------------------------------------------
    # Geohash-based fast candidate scoring (USLP geographic space)
    # ------------------------------------------------------------------

    def geohash_score(self, lat1: float, lon1: float,
                      lat2: float, lon2: float,
                      precision: int = 6) -> float:
        """
        Compute geographic similarity score between two entities via geohash.

        The USLP paper selects geohash precision per relation type:
        - Short precision (4–5): farther-distance relations (isInCountry)
        - Long precision (6–7): nearby relations (addrSuburb, addrHamlet)

        Score = 1 / (1 + Haversine(geohash_center1, geohash_center2))

        Args:
            lat1, lon1: coordinates of head entity
            lat2, lon2: coordinates of candidate tail entity
            precision:  geohash precision (4–7, default 6)

        Returns:
            float in (0, 1] — higher means closer
        """
        gh1 = _encode_geohash(lat1, lon1, precision)
        gh2 = _encode_geohash(lat2, lon2, precision)
        c1 = _geohash_center(gh1)
        c2 = _geohash_center(gh2)
        dist_km = haversine(c1, c2, unit=Unit.KILOMETERS)
        return 1.0 / (1.0 + dist_km)

    # ------------------------------------------------------------------
    # Pool statistics
    # ------------------------------------------------------------------

    def pool_stats(self) -> Dict:
        """Return summary statistics about the loaded embedding pool."""
        if self._pool_size == 0:
            return {'pool_size': 0, 'embedding_dim': 0}
        return {
            'pool_size': self._pool_size,
            'embedding_dim': self._pool_embeddings.shape[1],
            'lat_range': (float(self._pool_coords[:, 0].min()),
                          float(self._pool_coords[:, 0].max())),
            'lon_range': (float(self._pool_coords[:, 1].min()),
                          float(self._pool_coords[:, 1].max())),
        }
