import json
import logging
import os
import requests
from pathlib import Path
from typing import Dict, Optional, List

logger = logging.getLogger(__name__)

class GeofabrikIndexService:
    """
    Fetches and parses the Geofabrik index-v1.json file to map ISO codes to slugs.
    """
    INDEX_URL = "https://download.geofabrik.de/index-v1.json"

    def __init__(self, cache_path: str = None):
        if cache_path is None:
            base = os.getenv("BASE_DATA_DIR")
            cache_path = str(Path(base) / "geofabrik_index.json") if base else "data/geofabrik_index.json"
        self.cache_path = Path(cache_path)
        self.data = None

    def fetch_index(self, force_refresh: bool = False) -> Dict:
        """Fetch index from Geofabrik or local cache."""
        if not force_refresh and self.cache_path.exists():
            try:
                with open(self.cache_path, 'r') as f:
                    self.data = json.load(f)
                return self.data
            except Exception as e:
                logger.error(f"Failed to load cached Geofabrik index: {e}")

        logger.info(f"Fetching Geofabrik index from {self.INDEX_URL}...")
        try:
            response = requests.get(self.INDEX_URL)
            response.raise_for_status()
            self.data = response.json()
            
            # Cache it
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.cache_path, 'w') as f:
                json.dump(self.data, f)
                
            return self.data
        except Exception as e:
            logger.error(f"Failed to fetch Geofabrik index: {e}")
            return {}

    def get_iso_to_slug_map(self) -> Dict[str, Dict]:
        """
        Parses the feature collection into a mapping of ISO -> {slug, name, parent, pbf_url}.
        """
        if not self.data:
            self.fetch_index()
            
        iso_map = {}
        features = self.data.get('features', [])
        
        for feature in features:
            props = feature.get('properties', {})
            iso_codes = props.get('iso3166-1:alpha2', [])
            slug = props.get('id')
            name = props.get('name')
            parent = props.get('parent')
            urls = props.get('urls', {})
            pbf_url = urls.get('pbf')
            
            if iso_codes and slug:
                for iso in iso_codes:
                    iso_map[iso.upper()] = {
                        'slug': slug,
                        'name': name,
                        'parent': parent,
                        'pbf_url': pbf_url
                    }
                    
        return iso_map

geofabrik_index_service = GeofabrikIndexService()
