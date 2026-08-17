import json
import logging
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from django.conf import settings
from django.utils import timezone

from core.models import OSMWikiDataHierarchy
from core.services.planet_init.osm_relation_hierarchy_service import (
    osm_relation_hierarchy_service,
)
from core.services.snapshot.regional_path_service import normalize_country_slug
from core.services.planet_init.osm_wikidata_resolver import get_country_relations_dict
from core.services.snapshot.country_override_service import get_country_slug

logger = logging.getLogger(__name__)


@dataclass
class HierarchyStatus:
    iso: str
    cache_path: Optional[Path]
    subgraph_path: Optional[Path]
    synced_at: Optional[Any]
    from_cache: bool
    children: Optional[int]


class HierarchyCacheService:
    """Central service for managing Wikidata/OSM hierarchy caches.

    Responsibilities:
    - Resolve country metadata from DB given an ISO.
    - Build or load hierarchy JSON via OsmRelationHierarchyService.
    - Ensure hierarchy cache JSON exists in the backend cache directory.
    - Optionally copy hierarchy JSON into the subgraph hierarchy path.
    - Track cache paths and sync time on OSMWikiDataHierarchy.
    """

    def __init__(self, cache_dir: Optional[Path] = None) -> None:
        # Deprecated: we keep the interface but no longer persist caches to disk.
        base_dir = Path(settings.BASE_DIR).parent
        default_cache_dir = base_dir / "backend" / "data" / "osm_wikidata_hierarchy_cache"
        self.cache_dir = cache_dir or default_cache_dir

    def _resolve_country_entry(self, iso: str) -> Optional[Dict[str, Any]]:
        iso = (iso or "").upper()
        relations = get_country_relations_dict()
        if not relations:
            logger.warning("HierarchyCacheService: no country relations available")
            return None

        entry = (
            relations.get(iso)
            or relations.get(iso.upper())
            or relations.get(iso.lower())
        )
        if not entry:
            for k, v in relations.items():
                if k.upper() == iso or v.get("iso_code", "").upper() == iso:
                    entry = v
                    break
        if not entry:
            logger.warning("HierarchyCacheService: ISO %s not found in relations", iso)
        return entry

    def _get_cache_path(self, iso: str) -> Path:
        # Deprecated: retained for backwards compatibility, but not used for I/O.
        iso = (iso or "").upper()
        return self.cache_dir / f"{iso}_hierarchy.json"

    def get_or_build_hierarchy(
        self,
        iso: str,
        *,
        refresh: bool = False,
        propagate_to_subgraphs: bool = True,
    ) -> Dict[str, Any]:
        """Ensure hierarchy JSON exists for the given ISO and return metrics.

        If `refresh` is False and a usable cache is found, it will be reused.
        Otherwise, OsmRelationHierarchyService is invoked to rebuild the
        hierarchy and cache JSON.
        """

        iso = (iso or "").upper()
        entry = self._resolve_country_entry(iso)
        if not entry:
            return {
                "success": False,
                "iso": iso,
                "error": "missing_entry",
            }

        relation_id = entry.get("relation_id")
        wikidata_uri = entry.get("wkg_uri")
        country_name = entry.get("name")
        continent = entry.get("continent_name") or entry.get("parent_slug")

        if not relation_id or not wikidata_uri or not country_name:
            logger.warning(
                "HierarchyCacheService: ISO %s missing relation_id/wkg_uri/name, skipping.",
                iso,
            )
            return {
                "success": False,
                "iso": iso,
                "error": "missing_fields",
            }

        # Derive country_slug using JSON-based ISO overrides where needed
        default_slug = entry.get("slug", normalize_country_slug(country_name))
        country_slug = get_country_slug(iso, default_slug)

        # Build hierarchy via OsmRelationHierarchyService (in-memory only)
        start_ts = time.time()
        try:
            hierarchy = osm_relation_hierarchy_service.build_hierarchy_for_country(
                iso=iso,
                root_relation_id=relation_id,
                wikidata_uri=wikidata_uri,
                country_name=country_name,
                continent=continent,
                country_slug=country_slug,
            )
        except Exception as exc:
            logger.error("HierarchyCacheService: build failed for %s: %s", iso, exc, exc_info=True)
            return {
                "success": False,
                "iso": iso,
                "error": str(exc),
            }

        admin_tree = hierarchy.get("admin_tree", [])
        root = admin_tree[0] if admin_tree else {}
        children_count = len(root.get("children", []))

        duration = time.time() - start_ts
        logger.info(
            "HierarchyCacheService: built hierarchy for %s: children=%s, duration=%.2fs",
            iso,
            children_count,
            duration,
        )

        # Update DB tracking
        self._update_db_tracking(
            iso,
            cache_path=None,
            subgraph_path=None,
        )

        return {
            "success": True,
            "iso": iso,
            "country_name": country_name,
            "country_slug": country_slug,
            "children": children_count,
            "from_cache": False,
            "cache_path": None,
            "subgraph_path": None,
            "duration": duration,
        }

    def _update_db_tracking(
        self,
        iso: str,
        *,
        cache_path: Optional[str],
        subgraph_path: Optional[str],
    ) -> None:
        try:
            iso = (iso or "").upper()
            # We expect one country-level OSMWikiDataHierarchy per slug/admin_level=2
            hierarchy = OSMWikiDataHierarchy.objects.filter(admin_level=2).filter(
                slug__iexact=iso
            ).first()
            if not hierarchy:
                # Fall back to wikidata_id/uri-based heuristic
                hierarchy = OSMWikiDataHierarchy.objects.filter(admin_level=2).filter(
                    wikidata_id__icontains=iso
                ).first()
            if not hierarchy:
                return

            hierarchy.hierarchy_synced_at = timezone.now()
            if cache_path:
                hierarchy.hierarchy_cache_path = cache_path
            if subgraph_path:
                hierarchy.subgraph_hierarchy_path = subgraph_path
            hierarchy.save(update_fields=[
                "hierarchy_synced_at",
                "hierarchy_cache_path",
                "subgraph_hierarchy_path",
            ])
        except Exception as exc:
            logger.warning("HierarchyCacheService: failed to update DB tracking for %s: %s", iso, exc)


hierarchy_cache_service = HierarchyCacheService()
