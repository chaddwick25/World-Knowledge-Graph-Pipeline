"""Helper functions for resolving country metadata and geometry.

Monolith split (Phase 5 of PIPELINE_CONTROL_PLANE_AND_CLEANUP_PLAN): the
resolver functions moved to ``country_relations.py`` / ``country_iso.py`` /
``country_bbox.py`` (the static ISO/bbox/relations tables were already
extracted — this module reads ``OSMWikiDataHierarchy`` + the
``country_relations.json`` file).  This module re-exports them for the
single import site (``osm_wikidata_resolver.py``).
"""

from core.services.planet_init.country_bbox import (
    _store_bbox_on_profile,
    populate_bbox_for_profile,
    resolve_bbox_for_profile,
    resolve_country_bbox,
)
from core.services.planet_init.country_iso import (
    resolve_iso_code,
    resolve_iso_from_country_name,
)
from core.services.planet_init.country_relations import (
    get_country_by_iso,
    get_country_by_name,
    get_country_relations_dict,
)

__all__ = [
    "resolve_iso_code",
    "resolve_iso_from_country_name",
    "resolve_country_bbox",
    "populate_bbox_for_profile",
    "_store_bbox_on_profile",
    "resolve_bbox_for_profile",
    "get_country_relations_dict",
    "get_country_by_iso",
    "get_country_by_name",
]
