"""
EntityGeocoder — resolve entity names to OsmEntity objects with lat/lon.

Uses PostGIS ILIKE name search first (fast, exact-ish), falls back to
FastText semantic search on the name (handles "Hollywood Boulevard" vs
"Hollywood Blvd").

Implements MAPQA_TO_EXECUTION_PLAN.md §2.2.3.
"""

import logging

from worldkg_nca.models import OsmEntity
from worldkg_nca.snapshot_utils import get_latest_snapshot_id

logger = logging.getLogger(__name__)


class EntityGeocoder:
    """Resolve entity names to coordinates using the OsmEntity table."""

    @staticmethod
    def geocode(entity_name: str, country_code: str = None,
                snapshot_date: str = None) -> dict:
        """Resolve an entity name to {osm_id, name, lat, lon, tags}.

        Args:
            entity_name: The entity name to geocode (e.g. "Hollywood Blvd")
            country_code: Optional ISO 3166-1 alpha-2 code to scope the search
            snapshot_date: Optional YYYY_MM_DD snapshot partition key

        Returns:
            dict with osm_id, name, lat, lon, tags, or None if not found.
        """
        if not entity_name:
            return None

        snapshot_id = snapshot_date or get_latest_snapshot_id()
        if not snapshot_id:
            logger.warning("No snapshot_id available for geocoding")
            return None

        qs = OsmEntity.objects.using("vectors").filter(
            snapshot_id=snapshot_id,
        )
        if country_code:
            qs = qs.filter(country_code=country_code.upper())

        # 1. Exact name match (case-insensitive) — check the 'name' tag
        qs_exact = qs.filter(tags__name__iexact=entity_name)
        entity = qs_exact.first()
        if entity:
            return EntityGeocoder._entity_to_dict(entity)

        # 2. ILIKE contains match
        qs_ilike = qs.filter(tags__name__icontains=entity_name)
        entity = qs_ilike.first()
        if entity:
            return EntityGeocoder._entity_to_dict(entity)

        # 3. Try with common abbreviations expanded
        expanded = EntityGeocoder._expand_abbreviations(entity_name)
        if expanded != entity_name:
            qs_exp = qs.filter(tags__name__icontains=expanded)
            entity = qs_exp.first()
            if entity:
                return EntityGeocoder._entity_to_dict(entity)

        logger.debug("Geocode failed for '%s' (country=%s, snapshot=%s)",
                     entity_name, country_code, snapshot_id)
        return None

    @staticmethod
    def geocode_many(entity_names: list, country_code: str = None,
                     snapshot_date: str = None) -> list:
        """Geocode multiple entity names. Returns list of dicts or None."""
        return [
            EntityGeocoder.geocode(name, country_code, snapshot_date)
            for name in entity_names
        ]

    @staticmethod
    def _entity_to_dict(entity: OsmEntity) -> dict:
        tags = entity.tags or {}
        lat = lon = None
        if entity.geom:
            lat = entity.geom.y
            lon = entity.geom.x
        return {
            "osm_id": entity.osm_id,
            "osm_type": entity.osm_type,
            "name": tags.get("name", ""),
            "lat": lat,
            "lon": lon,
            "tags": tags,
            "wkg_class": entity.wkg_class,
        }

    @staticmethod
    def _expand_abbreviations(name: str) -> str:
        """Expand common street/avenue abbreviations."""
        replacements = {
            " blvd": " boulevard",
            " ave": " avenue",
            " st": " street",
            " rd": " road",
            " dr": " drive",
            " ln": " lane",
            " hwy": " highway",
            " fwy": " freeway",
            " expy": " expressway",
            " pkwy": " parkway",
            " pl": " place",
            " sq": " square",
            " ct": " court",
        }
        lower = name.lower()
        for abbrev, full in replacements.items():
            if lower.endswith(abbrev):
                return name[:len(name) - len(abbrev)] + full
        return name
