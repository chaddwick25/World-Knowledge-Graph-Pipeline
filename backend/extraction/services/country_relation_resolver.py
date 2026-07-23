import json
import logging
from pathlib import Path
from typing import Dict, List

from .geofabrik_index_service import geofabrik_index_service
from .sparql_country_relation_service import sparql_country_relation_service
from .regional_path_service import normalize_country_name

logger = logging.getLogger(__name__)

# ── Continent slug redirects ─────────────────────────────────────────
# Geofabrik parent slugs use hyphens (e.g. 'australia-oceania') but
# RegionHierarchy continent names use underscores (e.g. 'oceania').
# This map redirects known Geofabrik continent paths to their canonical
# RegionHierarchy names.
CONTINENT_REDIRECTS = {
    'australia_oceania': 'oceania',
    'central_america': 'central-america',
    'north_america': 'north-america',
    'south_america': 'south-america',
}

class CountryRelationResolver:
    """
    Merges WorldKG SPARQL data with Geofabrik Index data to create a 
    definitive regional mapping file.
    """
    #  TODO: remove hard coded path
    def __init__(self, output_path: str = "data/country_relations.json"):
        self.output_path = Path(output_path)

    def sync(self, force_refresh: bool = False) -> Dict:
        """
        Fetches data from both sources, merges them by ISO code, and saves to JSON.
        Now also links to continents registered in the RegionHierarchy.
        """
        # 1. Get Geofabrik ISO -> Slug mapping
        geofabrik_map = geofabrik_index_service.get_iso_to_slug_map()
        
        # 2. Get SPARQL ISO -> Relation ID mapping
        sparql_map = sparql_country_relation_service.fetch_country_relations()
        # TODO:remove if its legacy code I dont think we use fallback mapping
        # 3. Get all countries from RegionHierarchy for fallback mapping
        from api.models import RegionHierarchy
        all_regions = RegionHierarchy.objects.filter(parent__isnull=False).select_related('parent')
        db_region_map = {}
        for r in all_regions:
            normalized_name = normalize_country_name(r.name)
            db_region_map[normalized_name] = r
            
        # Build continent map with normalized underscore keys to handle
        # slug variants like 'central-america' vs 'central_america'.
        # If duplicates exist (e.g. hyphen and underscore variants), the
        # entry with children wins — the empty one is the stale duplicate.
        raw_continents = {}
        for r in RegionHierarchy.objects.filter(parent__isnull=True):
            key = r.name.lower().replace('-', '_')
            existing = raw_continents.get(key)
            if existing is None or r.children.count() > 0:
                raw_continents[key] = str(r.id)
        continents = raw_continents
        
        # 4. Merge and filter
        merged_list = {}
        for iso, sparql_data in sparql_map.items():
            name = sparql_data['name']
            normalized_sparql_name = normalize_country_name(name)
            
            if iso in geofabrik_map:
                geo_data = geofabrik_map[iso]
                parent_slug = geo_data['parent']
                
                # Normalize parent slug to find continent
                continent_name = None
                continent_id = None
                
                if parent_slug:
                    base_continent = parent_slug.split('/')[0].lower().replace('-', '_')
                    # Apply redirects for known continent name mismatches
                    if base_continent in CONTINENT_REDIRECTS:
                        base_continent = CONTINENT_REDIRECTS[base_continent]
                    
                    if base_continent in continents:
                        continent_name = base_continent
                        continent_id = continents[base_continent]

                # Use hyphens for slugs to match on-disk GeoVectors TSV filenaming
                slug = geo_data['slug'].replace('_', '-')
                merged_list[iso] = {
                    'name': name,
                    'relation_id': sparql_data['relation_id'],
                    'slug': slug,
                    'parent_slug': parent_slug,
                    'continent_name': continent_name,
                    'continent_id': continent_id,
                    'pbf_url': geo_data['pbf_url'],
                    'wkg_uri': sparql_data['uri']
                }
            elif normalized_sparql_name in db_region_map:
                # Fallback: found in SPARQL and DB, but missing from Geofabrik
                db_region = db_region_map[normalized_sparql_name]
                continent_name = db_region.parent.name if db_region.parent else None
                continent_id = str(db_region.parent.id) if db_region.parent else None
                
                logger.info(f"ISO {iso} ({name}) matched via RegionHierarchy fallback (missing from Geofabrik).")
                
                merged_list[iso] = {
                    'name': name,
                    'relation_id': sparql_data['relation_id'],
                    'slug': db_region.name, # Use DB name as slug
                    'parent_slug': continent_name,
                    'continent_name': continent_name,
                    'continent_id': continent_id,
                    'pbf_url': None, # No direct Geofabrik PBF
                    'wkg_uri': sparql_data['uri']
                }
            else:
                logger.warning(f"ISO {iso} ({name}) found in SPARQL but missing from both Geofabrik and RegionHierarchy.")

        # 5. Save results
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.output_path, 'w') as f:
            json.dump(merged_list, f, indent=4)
            
        logger.info(f"Sync complete. {len(merged_list)} regions written to {self.output_path}")
        return merged_list

country_relation_resolver = CountryRelationResolver()

