"""
Pipeline configuration helpers.

Historically this file held the ``CountryConfig`` dataclass — the central
configuration object for a single pipeline run. That class has been
superseded by ``CountryEnvelope`` / ``PlanetEnvelope`` in
``pipeline/envelopes.py``, which use frozen dataclasses and externalized
hyperparams (``hyperparams.yaml``).

What remains here are two utilities still used by the envelopes:

* ``_resolve_country_pbf_path`` — resolve a country PBF path from
  candidate slugs (used by ``CountryEnvelope.from_db``).
* ``SubgraphConfig`` — the subgraph dataclass, used by both the
  envelopes and the country-pipeline tasks (subgraph fan-out).
"""

import dataclasses
import logging
from typing import List, Optional

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────
# Path resolution helper
# ──────────────────────────────────────────────────────────────────────────
def _resolve_country_pbf_path(
    path_service, continent_norm: str, candidate_slugs: List[str],
) -> Optional[str]:
    """Resolve the country PBF path by trying candidate slugs.

    Tries each slug in *candidate_slugs* and returns the first path
    whose parent directory exists on the filesystem.  Falls back to
    the first candidate if none exist.
    """
    for candidate in candidate_slugs:
        try:
            if hasattr(path_service, "get_country_pbf_path"):
                pbf_path = path_service.get_country_pbf_path(continent_norm, candidate)
                if pbf_path.parent.exists():
                    return str(pbf_path)
        except Exception:
            continue
    # Fallback: return the first candidate path anyway
    if hasattr(path_service, "get_country_pbf_path"):
        return str(path_service.get_country_pbf_path(continent_norm, candidate_slugs[0]))
    return None

# ──────────────────────────────────────────────────────────────────────────
# SubgraphConfig
# ──────────────────────────────────────────────────────────────────────────
@dataclasses.dataclass(frozen=True)
class SubgraphConfig:
    """Configuration for a single subgraph (admin region / city).

    Subgraphs allow large countries (USA, Canada, Russia) to be processed
    on commercial hardware by splitting the country into smaller regions
    that each fit in GPU memory.
    """

    name: str
    slug: str
    osm_relation_id: Optional[int] = None
    wikidata_qid: Optional[str] = None
    admin_level: Optional[int] = None

    # Paths (resolved at config-build time)
    poly_path: Optional[str] = None
    pbf_path: Optional[str] = None
    pickle_path: Optional[str] = None

    # Bounding box (for filtering)
    bbox_min_lon: Optional[float] = None
    bbox_min_lat: Optional[float] = None
    bbox_max_lon: Optional[float] = None
    bbox_max_lat: Optional[float] = None

    @classmethod
    def from_db(cls, profile: "SubgraphProfile") -> "SubgraphConfig":
        """Build from a SubgraphProfile DB row."""
        return cls(
            name=profile.name,
            slug=profile.slug,
            osm_relation_id=profile.osm_relation_id,
            wikidata_qid=profile.wikidata_id,
            admin_level=profile.admin_level,
            poly_path=profile.subgraph_poly_path,
            pbf_path=profile.subgraph_pbf_path,
            pickle_path=profile.subgraph_pickle_path,
            bbox_min_lon=profile.bbox_min_lon,
            bbox_min_lat=profile.bbox_min_lat,
            bbox_max_lon=profile.bbox_max_lon,
            bbox_max_lat=profile.bbox_max_lat,
        )

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SubgraphConfig":
        return cls(**{k: v for k, v in d.items() if k in {f.name for f in dataclasses.fields(cls)}})
