import json
import logging
from collections import deque
from pathlib import Path
from typing import Dict, Optional
import requests

logger = logging.getLogger(__name__)
# TODO: Audit this entire file for security and correctness
# TODO: Load these from a config file
WKGS_BASE_URI = "http://schema.worldkg.org/"
ZENODO_ONTOLOGY_URL = "https://zenodo.org/record/4953986/files/worldkg_ontology.ttl"
WORLDKG_REPO_URL = "https://github.com/alishiba14/WorldKG-Knowledge-Graph"
class WorldKGOntologyLoader:
    """
    Load the official WorldKG ontology from the Zenodo RDF dump into the
    Redis-backed WorldKGOntologyService.

    Sources:
    - Zenodo dataset (full TTL):  https://zenodo.org/record/4953986
    - GitHub ontology JSON:       WorldKG-Knowledge-Graph/WorldKG_ontology.json

    The TTL file contains:
    - rdfs:subClassOf relationships (class hierarchy)
    - owl:equivalentClass links to Wikidata and DBpedia (NCA result)
    - dcterms:source links to OSM Wiki pages

    Usage:
        loader = WorldKGOntologyLoader()

        # From a downloaded TTL file
        ontology_dict = loader.load_from_ttl('/path/to/worldkg_ontology.ttl')

        # From the GitHub JSON format
        ontology_dict = loader.load_from_json('/path/to/WorldKG_ontology.json')

        # Pipe into the ontology service
        from worldkg_nca.services.ontology_service import get_worldkg_ontology_service
        service = get_worldkg_ontology_service()
        service.load_ontology_from_dict(ontology_dict)
    """

    def load_from_ttl(self, ttl_path: str) -> Dict:
        """
        Parse WorldKG ontology from a TTL file (Zenodo download) and convert
        to the dict format accepted by WorldKGOntologyService.load_ontology_from_dict().

        Args:
            ttl_path: Path to the WorldKG ontology .ttl file

        Returns:
            Dict with wkgs:-prefixed class names as keys
        """
        try:
            from rdflib import Graph, OWL, RDF, Namespace
            from rdflib.namespace import RDFS, DCTERMS
        except ImportError:
            raise ImportError("rdflib is required: pip install rdflib")

        WKGS = Namespace(WKGS_BASE_URI)

        g = Graph()
        logger.info(f"Parsing WorldKG ontology TTL: {ttl_path}")
        g.parse(ttl_path, format='turtle')
        logger.info(f"Loaded {len(g)} ontology triples")

        ontology_dict: Dict = {}

        # Extract rdfs:subClassOf relationships
        for cls, superclass in g.subject_objects(RDFS.subClassOf):
            cls_str = str(cls)
            super_str = str(superclass)

            if not cls_str.startswith(WKGS_BASE_URI):
                continue

            class_name = 'wkgs:' + cls_str[len(WKGS_BASE_URI):]

            if class_name not in ontology_dict:
                ontology_dict[class_name] = {
                    'superclasses': [],
                    'depth': 0,
                    'canonical_osm_key': None,
                    'canonical_osm_value': None,
                    'wikidata_equivalent': None,
                    'dbpedia_equivalent': None,
                    'canonical_tags': {},
                }

            if super_str.startswith(WKGS_BASE_URI):
                parent_name = 'wkgs:' + super_str[len(WKGS_BASE_URI):]
                if parent_name not in ontology_dict[class_name]['superclasses']:
                    ontology_dict[class_name]['superclasses'].append(parent_name)

        # Ensure root class exists
        if 'wkgs:WKGObject' not in ontology_dict:
            ontology_dict['wkgs:WKGObject'] = {
                'superclasses': [],
                'depth': 0,
                'canonical_osm_key': None,
                'canonical_osm_value': None,
                'wikidata_equivalent': None,
                'dbpedia_equivalent': None,
                'canonical_tags': {},
            }

        # Extract owl:equivalentClass links (NCA alignment result)
        for cls, equiv in g.subject_objects(OWL.equivalentClass):
            cls_str = str(cls)
            equiv_str = str(equiv)

            if not cls_str.startswith(WKGS_BASE_URI):
                continue

            class_name = 'wkgs:' + cls_str[len(WKGS_BASE_URI):]
            if class_name not in ontology_dict:
                continue

            if 'wikidata.org' in equiv_str:
                ontology_dict[class_name]['wikidata_equivalent'] = equiv_str
            elif 'dbpedia.org' in equiv_str:
                ontology_dict[class_name]['dbpedia_equivalent'] = equiv_str

        # Derive canonical_osm_key and canonical_osm_value from class names
        # WorldKG classes follow UpperCamelCase from OSM key/value naming.
        # We infer back using the OSM feature map naming convention.
        self._infer_osm_keys_from_hierarchy(ontology_dict)

        # Compute depths using BFS from wkgs:WKGObject
        self._compute_depths(ontology_dict)

        logger.info(
            f"Parsed {len(ontology_dict)} WorldKG classes "
            f"({sum(1 for v in ontology_dict.values() if v.get('wikidata_equivalent'))} "
            f"with Wikidata alignment)"
        )
        return ontology_dict

    def load_from_json(self, json_path: str) -> Dict:
        """
        Load ontology from the WorldKG GitHub repository JSON format.

        The WorldKG repo includes WorldKG_ontology.json with class lists and mappings.
        This method converts that format to the WorldKGOntologyService dict format.

        Args:
            json_path: Path to WorldKG_ontology.json from the WorldKG GitHub repo

        Returns:
            Dict with wkgs:-prefixed class names as keys
        """
        with open(json_path) as f:
            raw = json.load(f)

        ontology_dict: Dict = {
            'wkgs:WKGObject': {
                'superclasses': [],
                'depth': 0,
                'canonical_osm_key': None,
                'canonical_osm_value': None,
                'wikidata_equivalent': None,
                'dbpedia_equivalent': None,
                'canonical_tags': {},
            }
        }

        for entry in raw:
            # Expected fields from WorldKG repo JSON:
            # {class_name, parent_class, osm_key, osm_value, wikidata_class, dbpedia_class}
            class_name = entry.get('class_name', '')
            if not class_name.startswith('wkgs:'):
                class_name = 'wkgs:' + class_name

            parent = entry.get('parent_class', 'wkgs:WKGObject')
            if parent and not parent.startswith('wkgs:'):
                parent = 'wkgs:' + parent

            osm_key = entry.get('osm_key') or None
            osm_value = entry.get('osm_value') or None

            canonical_tags = {}
            if osm_key and osm_value:
                canonical_tags = {osm_key: osm_value}

            ontology_dict[class_name] = {
                'superclasses': [parent] if parent else [],
                'depth': 0,
                'canonical_osm_key': osm_key,
                'canonical_osm_value': osm_value,
                'wikidata_equivalent': entry.get('wikidata_class') or None,
                'dbpedia_equivalent': entry.get('dbpedia_class') or None,
                'canonical_tags': canonical_tags,
            }

        self._compute_depths(ontology_dict)
        logger.info(f"Loaded {len(ontology_dict)} classes from {json_path}")
        return ontology_dict

    def save_to_json(self, ontology_dict: Dict, output_path: str) -> None:
        """
        Save the loaded ontology dict to a JSON file for caching.

        The saved file can be reloaded via WorldKGOntologyService.load_ontology_from_dict()
        or passed to the management command with --load-ontology.
        """
        with open(output_path, 'w') as f:
            json.dump(ontology_dict, f, indent=2)
        logger.info(f"Saved {len(ontology_dict)} classes to {output_path}")

    def _compute_depths(self, ontology_dict: Dict) -> None:
        """BFS from wkgs:WKGObject to compute depth for all classes."""
        depths: Dict[str, int] = {'wkgs:WKGObject': 0}
        queue: deque = deque(['wkgs:WKGObject'])

        while queue:
            current = queue.popleft()
            for class_name, metadata in ontology_dict.items():
                if current in metadata.get('superclasses', []):
                    if class_name not in depths:
                        depths[class_name] = depths[current] + 1
                        queue.append(class_name)

        for class_name, depth in depths.items():
            if class_name in ontology_dict:
                ontology_dict[class_name]['depth'] = depth

    def _infer_osm_keys_from_hierarchy(self, ontology_dict: Dict) -> None:
        """
        Infer canonical_osm_key for depth-1 classes by examining subclass names.

        WorldKG depth-1 classes are named by UpperCamelCase of OSM key:
          wkgs:Amenity  <- key = 'amenity'
          wkgs:Natural  <- key = 'natural'
          wkgs:Building <- key = 'building'

        This provides the key context needed for predict_class_from_tags().
        Exact key→class mapping requires the full WorldKG ontology or manual spec.
        """
        # Known depth-1 key class mappings from the WorldKG paper
        # TODO: Load these from a config file
        KEY_CLASS_MAP = {
            'wkgs:Amenity':    'amenity',
            'wkgs:Natural':    'natural',
            'wkgs:Building':   'building',
            'wkgs:Highway':    'highway',
            'wkgs:Railway':    'railway',
            'wkgs:Leisure':    'leisure',
            'wkgs:Shop':       'shop',
            'wkgs:Tourism':    'tourism',
            'wkgs:Historic':   'historic',
            'wkgs:Waterway':   'waterway',
            'wkgs:Landuse':    'landuse',
            'wkgs:Place':      'place',
            'wkgs:Aeroway':    'aeroway',
            'wkgs:Emergency':  'emergency',
            'wkgs:Healthcare': 'healthcare',
            'wkgs:Man_made':   'man_made',
            'wkgs:Power':      'power',
            'wkgs:Public_transport': 'public_transport',
        }

        for class_name, osm_key in KEY_CLASS_MAP.items():
            if class_name in ontology_dict:
                ontology_dict[class_name]['canonical_osm_key'] = osm_key
