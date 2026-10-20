"""EmbeddingDriftService — Wasserstein Distance metrics and freshness scores on embeddings.

Computes the Sliced Wasserstein Distance (SWD) between snapshot embeddings
(100D spatial GV-NLE and 300D semantic GV-Tags) to evaluate map freshness and drift.
"""

import logging
import numpy as np
from scipy.stats import wasserstein_distance
from django.db import connections
from django.contrib.gis.geos import Polygon

from worldkg_nca.models import OsmEntity
from core.models import SubgraphProfile

logger = logging.getLogger(__name__)


class EmbeddingDriftService:
    """Service for computing Wasserstein Distance metrics between snapshot embeddings.

    Uses Sliced Wasserstein Distance (SWD) to scale computation to high-dimensional spaces.
    """

    def compute_sliced_wasserstein_distance(
        self,
        X: np.ndarray,
        Y: np.ndarray,
        num_projections: int = 100,
        seed: int = 42
    ) -> float:
        """Compute the Sliced Wasserstein Distance (SWD) between two sets of high-dimensional vectors.

        Args:
            X: np.ndarray of shape (N, d)
            Y: np.ndarray of shape (M, d)
            num_projections: Number of random 1D projections
            seed: Random seed for projection consistency

        Returns:
            The average 1D Wasserstein distance across projections.
        """
        if X is None or Y is None or X.size == 0 or Y.size == 0:
            return 0.0

        # Ensure 2D arrays
        if X.ndim == 1:
            X = X.reshape(1, -1)
        if Y.ndim == 1:
            Y = Y.reshape(1, -1)

        dim = X.shape[1]

        # Consistent random generator
        rng = np.random.default_rng(seed)

        # Generate random projection vectors on the unit sphere
        projections = rng.normal(size=(num_projections, dim))
        norms = np.linalg.norm(projections, axis=1, keepdims=True)
        # Avoid division by zero
        norms[norms == 0] = 1e-10
        projections /= norms

        distances = []
        for j in range(num_projections):
            proj = projections[j]
            X_proj = X @ proj
            Y_proj = Y @ proj

            # 1D Wasserstein distance (Earth Mover's Distance)
            d = wasserstein_distance(X_proj, Y_proj)
            distances.append(d)

        return float(np.mean(distances))

    def calculate_freshness_score(
        self,
        semantic_w: float,
        spatial_w: float,
        alpha: float = 0.5,
        lambda_sem: float = 5.0,
        lambda_spat: float = 5.0
    ) -> float:
        """Compute a global freshness score in [0, 1] based on semantic and spatial Wasserstein distances.

        Freshness is high when drift is low:
        F = alpha * exp(-lambda_sem * semantic_w) + (1 - alpha) * exp(-lambda_spat * spatial_w)
        """
        f_sem = np.exp(-lambda_sem * semantic_w) if semantic_w is not None else 1.0
        f_spat = np.exp(-lambda_spat * spatial_w) if spatial_w is not None else 1.0

        score = alpha * f_sem + (1.0 - alpha) * f_spat
        return float(score)

    def fetch_embeddings(
        self,
        country_code: str,
        snapshot_id: str,
        embedding_field: str,
        bbox: tuple = None,
        mock_if_empty: bool = False,
        mock_dim: int = 300,
        mock_size: int = 1000,
        seed: int = 42
    ) -> np.ndarray:
        """Fetch embedding vectors from OsmEntity for a given snapshot, country, and optional bbox.

        Args:
            country_code: ISO 3166-1 alpha-2 country code
            snapshot_id: Temporal snapshot partition key
            embedding_field: Name of vector field to retrieve ('gv_tags_embedding' or 'gv_nle_embedding')
            bbox: Optional bounding box (min_lon, min_lat, max_lon, max_lat)
            mock_if_empty: Generate synthetic embeddings if query returns no results (useful for testing/demo)
            mock_dim: Dimension of synthetic embeddings
            mock_size: Number of synthetic vectors to generate
            seed: Seed for random synthetic vectors

        Returns:
            np.ndarray of shape (N, dim)
        """
        queryset = OsmEntity.objects.using('vectors').filter(
            country_code=country_code,
            snapshot_id=snapshot_id
        )

        # Apply field existence filter
        filter_dict = {f"{embedding_field}__isnull": False}
        queryset = queryset.filter(**filter_dict)

        # Apply spatial filter if bbox is provided
        if bbox:
            min_lon, min_lat, max_lon, max_lat = bbox
            bbox_polygon = Polygon.from_bbox((min_lon, min_lat, max_lon, max_lat))
            queryset = queryset.filter(geom__isnull=False, geom__within=bbox_polygon)

        # Optimize by loading only the embedding field values
        embeddings_list = list(queryset.values_list(embedding_field, flat=True))

        if len(embeddings_list) == 0:
            if mock_if_empty:
                logger.warning(
                    f"No embeddings found for {country_code} in snapshot {snapshot_id} "
                    f"({embedding_field}). Generating synthetic embeddings for testing."
                )
                rng = np.random.default_rng(seed)
                return rng.normal(loc=0.0, scale=0.5, size=(mock_size, mock_dim))
            return np.empty((0, mock_dim))

        return np.vstack(embeddings_list)

    def compute_country_drift(
        self,
        country_code: str,
        snapshot_from: str,
        snapshot_to: str,
        num_projections: int = 100,
        mock_missing: bool = False,
        seed: int = 42
    ) -> dict:
        """Compute global and subdivision-level embedding drift between two snapshots.

        Args:
            country_code: ISO country code (e.g., 'BZ')
            snapshot_from: Earlier snapshot partition key (e.g., '2025_12_31_baseline')
            snapshot_to: Later snapshot partition key (e.g., '2025_12_31')
            num_projections: Number of SWD projections
            mock_missing: Generate synthetic embeddings if partition data is missing
            seed: Random seed for reproducible computations

        Returns:
            dict containing global and subdivision-level Wasserstein distances and freshness scores.
        """
        # 1. Fetch country-wide embeddings
        sem_from = self.fetch_embeddings(
            country_code, snapshot_from, 'gv_tags_embedding',
            mock_if_empty=mock_missing, mock_dim=300, seed=seed
        )
        sem_to = self.fetch_embeddings(
            country_code, snapshot_to, 'gv_tags_embedding',
            mock_if_empty=mock_missing, mock_dim=300, seed=seed + 1
        )

        spat_from = self.fetch_embeddings(
            country_code, snapshot_from, 'gv_nle_embedding',
            mock_if_empty=mock_missing, mock_dim=100, seed=seed + 2
        )
        spat_to = self.fetch_embeddings(
            country_code, snapshot_to, 'gv_nle_embedding',
            mock_if_empty=mock_missing, mock_dim=100, seed=seed + 3
        )

        # Calculate global distances
        semantic_w = self.compute_sliced_wasserstein_distance(sem_from, sem_to, num_projections, seed)
        spatial_w = self.compute_sliced_wasserstein_distance(spat_from, spat_to, num_projections, seed + 4)

        global_freshness = self.calculate_freshness_score(semantic_w, spatial_w)

        results = {
            "country_code": country_code,
            "snapshot_from": snapshot_from,
            "snapshot_to": snapshot_to,
            "global": {
                "semantic_w": semantic_w,
                "spatial_w": spatial_w,
                "freshness_score": global_freshness,
                "semantic_count_from": len(sem_from),
                "semantic_count_to": len(sem_to),
                "spatial_count_from": len(spat_from),
                "spatial_count_to": len(spat_to)
            },
            "subdivisions": {}
        }

        # 2. Fetch subdivisions (subgraphs)
        subgraphs = SubgraphProfile.objects.filter(
            country_profile__iso2=country_code
        )

        for sub in subgraphs:
            if not all([sub.bbox_min_lon, sub.bbox_min_lat, sub.bbox_max_lon, sub.bbox_max_lat]):
                continue

            bbox = (sub.bbox_min_lon, sub.bbox_min_lat, sub.bbox_max_lon, sub.bbox_max_lat)

            sub_sem_from = self.fetch_embeddings(
                country_code, snapshot_from, 'gv_tags_embedding', bbox=bbox,
                mock_if_empty=mock_missing, mock_dim=300, seed=seed
            )
            sub_sem_to = self.fetch_embeddings(
                country_code, snapshot_to, 'gv_tags_embedding', bbox=bbox,
                mock_if_empty=mock_missing, mock_dim=300, seed=seed + 1
            )

            sub_spat_from = self.fetch_embeddings(
                country_code, snapshot_from, 'gv_nle_embedding', bbox=bbox,
                mock_if_empty=mock_missing, mock_dim=100, seed=seed + 2
            )
            sub_spat_to = self.fetch_embeddings(
                country_code, snapshot_to, 'gv_nle_embedding', bbox=bbox,
                mock_if_empty=mock_missing, mock_dim=100, seed=seed + 3
            )

            sub_sem_w = self.compute_sliced_wasserstein_distance(sub_sem_from, sub_sem_to, num_projections, seed)
            sub_spat_w = self.compute_sliced_wasserstein_distance(sub_spat_from, sub_spat_to, num_projections, seed + 4)
            sub_freshness = self.calculate_freshness_score(sub_sem_w, sub_spat_w)

            results["subdivisions"][sub.name] = {
                "slug": sub.slug,
                "semantic_w": sub_sem_w,
                "spatial_w": sub_spat_w,
                "freshness_score": sub_freshness,
                "semantic_count_from": len(sub_sem_from),
                "semantic_count_to": len(sub_sem_to),
                "spatial_count_from": len(sub_spat_from),
                "spatial_count_to": len(sub_spat_to)
            }

        return results


# Singleton instance
_embedding_drift_service = None


def get_embedding_drift_service() -> EmbeddingDriftService:
    """Get singleton instance of embedding drift service."""
    global _embedding_drift_service
    if _embedding_drift_service is None:
        _embedding_drift_service = EmbeddingDriftService()
    return _embedding_drift_service
