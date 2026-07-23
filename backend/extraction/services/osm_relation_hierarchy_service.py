import logging
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Any

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


WIKIDATA_SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"
WIKIDATA_USER_AGENT = "EDAVectorSearchToolkit/1.0 (OSMRelationHierarchy; contact via GitHub)"
SPARQL_TIMEOUT_S = 60


@dataclass
class HierarchyConfig:
    """Config for OSM relation hierarchy extraction."""
    max_depth: int = 4
    include_admin_levels: Optional[List[int]] = None
    # Deprecated: we no longer persist hierarchy caches to disk.
    cache_dir: str = "data/osm_wikidata_hierarchy_cache"


class OsmRelationHierarchyService:
    """Builds an OSM relation-based admin hierarchy for a given country.

    This is intentionally config-driven: no hardcoded country lists or
    admin_levels; behavior is controlled by HierarchyConfig.
    """

    def __init__(self, config: Optional[HierarchyConfig] = None):
        # We keep the config object for compatibility but no longer
        # create or use an on-disk hierarchy cache directory.
        self.config = config or HierarchyConfig()

    def build_hierarchy_for_country(
        self,
        iso: str,
        root_relation_id: int,
        wikidata_uri: Optional[str] = None,
        country_name: Optional[str] = None,
        continent: Optional[str] = None,
        country_slug: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Entry point: build or load the hierarchy tree for a country.

        This method is config-driven and uses Wikidata (P150/P402) to
        discover direct administrative subdivisions when a Wikidata URI is
        available. Results are cached per ISO code.

        Args:
            iso: ISO code (e.g., 'IE')
            root_relation_id: OSM relation ID for the country
            wikidata_uri: Optional Wikidata URI for the country
            country_name: Optional country name
            continent: Optional continent name (for subgraphs path resolution)
            country_slug: Optional country slug (for subgraphs path resolution)
        """
        iso = iso.upper()

        start_ts = time.time()
        children_count = 0

        # Root node (country level)
        root_node: Dict[str, Any] = {
            "osm_relation_id": root_relation_id,
            "name": country_name,
            "admin_level": 2,
            "children": [],
        }

        # If we have a Wikidata URI, try to fetch direct subdivisions with P402
        if wikidata_uri and "wikidata.org/entity/" in wikidata_uri:
            try:
                qid = wikidata_uri.rstrip("/").rsplit("/", 1)[-1]
                children = self._fetch_direct_subdivisions(qid)
                children_count = len(children)
                for child in children:
                    root_node["children"].append(
                        {
                            "osm_relation_id": child["relation_id"],
                            "name": child["label"],
                            "admin_level": None,
                            "children": [],
                        }
                    )
            except Exception as e:
                logger.warning(f"Failed to fetch subdivisions for {iso} from Wikidata: {e}")

        hierarchy = {
            "root_relation_id": root_relation_id,
            "admin_tree": [root_node],
            "default_levels": [2],
        }

        duration = time.time() - start_ts
        logger.info(
            "Built Wikidata hierarchy in-memory for %s: root_rel=%s, children=%s, "
            "duration=%.2fs",
            iso,
            root_relation_id,
            children_count,
            duration,
        )

        return hierarchy

    def _fetch_direct_subdivisions(self, country_qid: str) -> List[Dict[str, Any]]:
        """Fetch direct administrative subdivisions with OSM relation IDs.

        Uses P150 (contains administrative territorial entity) with an
        end-time qualifier filter (P582) to get currently valid children,
        and P402 (OSM relation ID) to link to OSM.
        """
        query = f"""
SELECT ?child ?childLabel ?osmRelId WHERE {{
  wd:{country_qid} p:P150 ?statement .
  ?statement ps:P150 ?child .
  FILTER NOT EXISTS {{ ?statement pq:P582 ?end }}
  ?child wdt:P402 ?osmRelId .
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}
ORDER BY ?childLabel
"""

        try:
            response = requests.get(
                WIKIDATA_SPARQL_ENDPOINT,
                params={"query": query, "format": "json"},
                headers={"User-Agent": WIKIDATA_USER_AGENT},
                timeout=SPARQL_TIMEOUT_S,
            )
            response.raise_for_status()
            data = response.json().get("results", {}).get("bindings", [])
        except Exception as exc:
            logger.warning(f"_fetch_direct_subdivisions({country_qid}): {exc}")
            return []

        children: List[Dict[str, Any]] = []
        for row in data:
            rel_raw = row.get("osmRelId", {}).get("value")
            label = row.get("childLabel", {}).get("value")
            if not rel_raw:
                continue
            try:
                rel_id = int(rel_raw)
            except (TypeError, ValueError):
                continue
            children.append({
                "relation_id": rel_id,
                "label": label,
            })

        logger.info(
            f"Fetched {len(children)} direct subdivisions with P402 for country {country_qid}"
        )
        return children


osm_relation_hierarchy_service = OsmRelationHierarchyService()
