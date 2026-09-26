"""Country relations lookup — DB-backed + country_relations.json fallback.

Extracted from ``country_metadata.py`` (monolith split, Phase 5 of
PIPELINE_CONTROL_PLANE_AND_CLEANUP_PLAN).
"""

import json
import logging
from pathlib import Path
from typing import Dict, Optional

from django.conf import settings
from django.db import models

from core.models import OSMWikiDataHierarchy

logger = logging.getLogger(__name__)

# Module-level cache for country relations to avoid repeated DB queries
_country_relations_cache: Dict[str, dict] = None


def get_country_relations_dict() -> Dict[str, dict]:

    """

    Load country relations from OSMWikiDataHierarchy model (cached).



    Returns a dict compatible with the old country_relations.json format:

    {

        'ISO_CODE': {

            'name': 'Country Name',

            'relation_id': 12345,

            'slug': 'country_slug',

            'parent_slug': 'continent_slug',

            'continent_name': 'continent_name',

            'continent_id': 'uuid',

            'pbf_url': 'https://...',

            'wkg_uri': 'http://www.wikidata.org/entity/Q781'

        },

        ...

    }

    """

    global _country_relations_cache

    if _country_relations_cache is not None:

        return _country_relations_cache

    try:

        import json

        from pathlib import Path

        from django.conf import settings



        # 1. Load data from the legacy JSON file if it exists. We only use this

        #    as an optional metadata overlay (ISO codes, Geovectors TSV paths),

        #    not as the primary source of truth.

        json_relations_by_uri: Dict[str, dict] = {}

        json_path = Path(settings.BASE_DATA_DIR) / "country_relations.json"

        if json_path.exists():

            try:

                with open(json_path, "r") as f:

                    json_data = json.load(f)

                    for iso_code, v in json_data.items():

                        wkg_uri = v.get("wkg_uri")

                        if not wkg_uri:

                            continue

                        v["iso_code"] = iso_code.upper()

                        json_relations_by_uri[wkg_uri] = v

            except Exception as e:

                logger.warning(f"Failed to load country_relations.json: {e}")



        # 2. Query all countries (admin_level=2) from OSMWikiDataHierarchy

        hierarchies = list(OSMWikiDataHierarchy.objects.filter(admin_level=2))



        # 3. Convert to dict format compatible with existing code, keyed

        #    primarily by ISO 3166-1 alpha-2 codes. When no ISO is known for a

        #    row, we fall back to using the Wikidata ID (QID) as the key.

        relations: Dict[str, dict] = {}

        for hierarchy in hierarchies:

            json_entry = json_relations_by_uri.get(hierarchy.wikidata_uri or "")



            # Prefer ISO code from JSON overlay when available

            iso_code = None

            if json_entry is not None:

                iso_code = json_entry.get("iso_code")



            # Fallback: use Wikidata ID / last URI segment as synthetic key

            if not iso_code:

                iso_code = hierarchy.wikidata_id or (

                    (hierarchy.wikidata_uri or "").rsplit("/", 1)[-1]

                    if hierarchy.wikidata_uri

                    else None

                )



            if not iso_code:

                continue



            key = iso_code.upper()



            data = {

                "name": hierarchy.name,

                "relation_id": hierarchy.osm_relation_id,

                "slug": hierarchy.slug,

                "parent_slug": hierarchy.parent_slug,

                "continent_name": hierarchy.continent_name,

                "continent_id": str(hierarchy.continent_id)

                if hierarchy.continent_id

                else None,

                "pbf_url": hierarchy.pbf_url,

                "wkg_uri": hierarchy.wikidata_uri,

                "iso_code": key,

            }



            # Merge TSV paths and legacy metadata from JSON when available

            if json_entry is not None:

                if json_entry.get("geovectors_location_tsv"):

                    data["geovectors_location_tsv"] = json_entry[

                        "geovectors_location_tsv"

                    ]

                if json_entry.get("geovectors_tags_tsv"):

                    data["geovectors_tags_tsv"] = json_entry["geovectors_tags_tsv"]

                # Prefer JSON pbf_url if DB does not have one

                if not data["pbf_url"] and json_entry.get("pbf_url"):

                    data["pbf_url"] = json_entry["pbf_url"]



            relations[key] = data



            qid = None

            if hierarchy.wikidata_id:

                qid = hierarchy.wikidata_id.upper()

            elif hierarchy.wikidata_uri:

                qid = hierarchy.wikidata_uri.rsplit("/", 1)[-1].upper()

            if qid and qid != key and qid not in relations:

                relations[qid] = dict(data)



        logger.info(

            "Loaded %d country relations from OSMWikiDataHierarchy (merged with JSON metadata)",

            len(hierarchies),

        )

        _country_relations_cache = relations

        return relations

    except Exception as exc:

        logger.error("Failed to load country relations from OSMWikiDataHierarchy: %s", exc)

        return {}







def get_country_by_iso(iso_code: str) -> Optional[dict]:

    """

    Get country data for a specific ISO code.

    

    Args:

        iso_code: ISO 3166-1 alpha-2 or alpha-3 code, or Wikidata ID

    

    Returns:

        Country data dict or None if not found

    """

    try:

        hierarchy = OSMWikiDataHierarchy.objects.filter(

            admin_level=2

        ).filter(

            models.Q(wikidata_id__icontains=iso_code) | 

            models.Q(slug__icontains=iso_code.lower())

        ).first()

        

        if not hierarchy:

            return None

        

        return {

            'name': hierarchy.name,

            'relation_id': hierarchy.osm_relation_id,

            'slug': hierarchy.slug,

            'parent_slug': hierarchy.parent_slug,

            'continent_name': hierarchy.continent_name,

            'continent_id': str(hierarchy.continent_id) if hierarchy.continent_id else None,

            'pbf_url': hierarchy.pbf_url,

            'wkg_uri': hierarchy.wikidata_uri,

            'wikidata_id': hierarchy.wikidata_id

        }

    except Exception as exc:

        logger.error(f"Failed to get country by ISO {iso_code}: {exc}")

        return None







def get_country_by_name(country_name: str) -> Optional[dict]:

    """

    Get country data by country name (case-insensitive).

    

    Args:

        country_name: Country name (e.g., 'Monaco', 'El Salvador')

    

    Returns:

        Country data dict or None if not found

    """

    try:

        hierarchy = OSMWikiDataHierarchy.objects.filter(

            admin_level=2,

            name__icontains=country_name

        ).first()

        

        if not hierarchy:

            return None

        

        iso_code = hierarchy.wikidata_id or (hierarchy.wikidata_uri.split('/')[-1] if hierarchy.wikidata_uri else None)

        

        return {

            'name': hierarchy.name,

            'relation_id': hierarchy.osm_relation_id,

            'slug': hierarchy.slug,

            'parent_slug': hierarchy.parent_slug,

            'continent_name': hierarchy.continent_name,

            'continent_id': str(hierarchy.continent_id) if hierarchy.continent_id else None,

            'pbf_url': hierarchy.pbf_url,

            'wkg_uri': hierarchy.wikidata_uri,

            'wikidata_id': iso_code

        }

    except Exception as exc:

        logger.error(f"Failed to get country by name {country_name}: {exc}")

        return None






