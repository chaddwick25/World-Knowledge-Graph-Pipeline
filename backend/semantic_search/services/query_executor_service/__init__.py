"""Query executor package — re-exports QueryExecutorService + constants.

Monolith split (Phase 5 of PIPELINE_CONTROL_PLANE_AND_CLEANUP_PLAN): the
module became a package; the class is assembled from per-concern mixins
in ``service.py`` (core), ``template_executors.py``, ``spatial_search.py``,
``graph_executors.py``, ``geo_uslp.py``, ``synthesis.py`` (constants in
``_constants.py``).  Import sites are unchanged.
"""

from semantic_search.services.query_executor_service._constants import (
    AMENITY_TAG_ALIASES,
    AMENITY_TO_WKGS,
    DEFAULT_NEAR_RADIUS_M,
    DIRECTION_NEAREST_RADIUS_M,
    GENERIC_AMENITY_KEYS,
    GENERIC_AMENITY_PHRASES,
    PROXIMITY_QUESTION_RE,
    USLP_FALLBACK_D_MAX_KM,
    USLP_GEOHASH_PRECISION,
    _FASTTEXT_AMENITY_DISTANCE_THRESHOLD,
)
from semantic_search.services.query_executor_service.service import (
    QueryExecutorService,
)

__all__ = ["QueryExecutorService"]
