"""
Helper functions for resolving country metadata and geometry.

This module is now a re-export hub. The actual implementations live in:
- extraction.services.geometry_utils — polygon/bbox/WKT helpers
- extraction.services.country_metadata — ISO code resolution, country bbox/relations/metadata

All existing imports `from extraction.services.osm_wikidata_resolver import X`
continue to work via the re-exports below.
"""

from extraction.services.geometry_utils import (
    parse_poly_bbox,
    parse_poly_to_wkt,
    bbox_to_wkt,
    expand_bbox_to_min_span,
)
from extraction.services.country_metadata import (
    resolve_iso_code,
    resolve_iso_from_country_name,
    resolve_country_bbox,
    populate_bbox_for_profile,
    _store_bbox_on_profile,
    resolve_bbox_for_profile,
    get_country_relations_dict,
    get_country_by_iso,
    get_country_by_name,
)

__all__ = [
    'parse_poly_bbox',
    'parse_poly_to_wkt',
    'bbox_to_wkt',
    'expand_bbox_to_min_span',
    'resolve_iso_code',
    'resolve_iso_from_country_name',
    'resolve_country_bbox',
    'populate_bbox_for_profile',
    '_store_bbox_on_profile',
    'resolve_bbox_for_profile',
    'get_country_relations_dict',
    'get_country_by_iso',
    'get_country_by_name',
]
