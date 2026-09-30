import redis
import json
import logging
from typing import Dict, List, Optional, Set
from django.conf import settings
from collections import defaultdict

logger = logging.getLogger(__name__)


class WorldKGOntologyService:
    """
    Redis-backed cache for WorldKG ontology structure.
    Provides fast lookups for class hierarchies, subsumption queries, and tree distances.
    
    [CODE AUDIT TRACEABILITY]
    Maps to logic from: https://github.com/alishiba14/WorldKG-Knowledge-Graph
    Implementation: Replaces their dynamic CreateTriples.py java/python tag lookup arrays
    by preloading the WorldKG JSON into Redis natively. `predict_class_from_tags()` 
    directly mathematically substitutes their raw TTL generation code.

    Namespaces (per Dsouza et al. CIKM 2021):
    - wkgs: = WorldKG schema — classes and properties (e.g., wkgs:Restaurant, wkgs:Amenity)
    - wkg:  = WorldKG entity instances only (e.g., wkg:1014675277)

    Ontology structure (2-level hierarchy from OSM map features):
    - Depth 0: wkgs:WKGObject (root)
    - Depth 1: Key-level classes (e.g., wkgs:Amenity from key 'amenity')
    - Depth 2: Value-subclasses (e.g., wkgs:Restaurant from amenity=restaurant)
    - Boolean/numeric values (e.g., building=yes) stay at depth-1 class (wkgs:Building)

    Redis keys:
    - worldkg:class:{class_name}:superclasses       -> List[str]
    - worldkg:class:{class_name}:subclasses         -> List[str]
    - worldkg:class:{class_name}:depth              -> int
    - worldkg:class:{class_name}:canonical_tags     -> JSON  (backward compat)
    - worldkg:class:{class_name}:canonical_osm_key  -> str
    - worldkg:class:{class_name}:canonical_osm_value -> str
    - worldkg:class:{class_name}:wikidata_equivalent -> str (full URI)
    - worldkg:class:{class_name}:dbpedia_equivalent  -> str (full URI)
    - worldkg:ontology:all_classes                  -> Set[str]
    """
    
    def __init__(self):
        self.redis_client = redis.Redis(
            host=settings.REDIS_HOST,
            port=settings.REDIS_PORT,
            db=settings.WORLDKG_REDIS_DB,
            decode_responses=True
        )
        self._cache_prefix = "worldkg"
        self._index_built = False
        self._key_value_index: Dict[str, Dict[str, str]] = {}
        self._key_class_index: Dict[str, str] = {}
        self._class_depths: Dict[str, int] = {}
        self._class_superclasses: Dict[str, List[str]] = {}
        self._class_wikidata: Dict[str, str] = {}
        self._legacy_tags_index: Dict[str, Dict[str, str]] = {}
        self._class_to_key_value: Dict[str, tuple] = {}
        self._key_class_index_reverse: Dict[str, str] = {}
    
    def _class_key(self, class_name: str, suffix: str) -> str:
        """Generate Redis key for class metadata."""
        return f"{self._cache_prefix}:class:{class_name}:{suffix}"
    
    def load_ontology_from_dict(self, ontology_dict: Dict[str, Dict]) -> None:
        """
        Load WorldKG ontology from a dictionary structure.

        Expected format (wkgs: namespace for all class names):
        {
            "wkgs:Cafe": {
                "superclasses": ["wkgs:Amenity"],
                "depth": 2,
                "canonical_osm_key": "amenity",
                "canonical_osm_value": "cafe",
                "wikidata_equivalent": "http://www.wikidata.org/entity/Q30022",
                "dbpedia_equivalent": "http://dbpedia.org/ontology/Cafe",
                "canonical_tags": {"amenity": "cafe"}  # kept for backward compat
            },
            ...
        }
        """
        pipe = self.redis_client.pipeline()
        all_classes = set()
        subclass_map = defaultdict(set)
        
        for class_name, metadata in ontology_dict.items():
            all_classes.add(class_name)
            
            # Store superclasses
            superclasses = metadata.get('superclasses', [])
            pipe.delete(self._class_key(class_name, 'superclasses'))
            if superclasses:
                pipe.rpush(self._class_key(class_name, 'superclasses'), *superclasses)
                for parent in superclasses:
                    subclass_map[parent].add(class_name)
            
            # Store depth
            depth = metadata.get('depth', 0)
            pipe.set(self._class_key(class_name, 'depth'), depth)
            
            # Store canonical tags (backward compat) and structured OSM key/value
            canonical_tags = metadata.get('canonical_tags', {})
            pipe.set(
                self._class_key(class_name, 'canonical_tags'),
                json.dumps(canonical_tags)
            )

            osm_key = metadata.get('canonical_osm_key')
            osm_value = metadata.get('canonical_osm_value')
            if osm_key:
                pipe.set(self._class_key(class_name, 'canonical_osm_key'), osm_key)
            else:
                pipe.delete(self._class_key(class_name, 'canonical_osm_key'))
            if osm_value:
                pipe.set(self._class_key(class_name, 'canonical_osm_value'), osm_value)
            else:
                pipe.delete(self._class_key(class_name, 'canonical_osm_value'))

            wikidata_eq = metadata.get('wikidata_equivalent')
            dbpedia_eq = metadata.get('dbpedia_equivalent')
            if wikidata_eq:
                pipe.set(self._class_key(class_name, 'wikidata_equivalent'), wikidata_eq)
            else:
                pipe.delete(self._class_key(class_name, 'wikidata_equivalent'))
            if dbpedia_eq:
                pipe.set(self._class_key(class_name, 'dbpedia_equivalent'), dbpedia_eq)
            else:
                pipe.delete(self._class_key(class_name, 'dbpedia_equivalent'))
        
        # Store reverse mapping (subclasses)
        for parent, children in subclass_map.items():
            pipe.delete(self._class_key(parent, 'subclasses'))
            pipe.rpush(self._class_key(parent, 'subclasses'), *children)
        
        # Store all classes set
        pipe.delete(f"{self._cache_prefix}:ontology:all_classes")
        pipe.sadd(f"{self._cache_prefix}:ontology:all_classes", *all_classes)
        
        pipe.execute()
        logger.info(f"Loaded {len(all_classes)} WorldKG classes into Redis")
        self._build_memory_index()
    
    def get_superclasses(self, class_name: str) -> List[str]:
        """Get all superclasses (parents) of a class."""
        return self.redis_client.lrange(
            self._class_key(class_name, 'superclasses'), 0, -1
        )
    
    def get_subclasses(self, class_name: str, recursive: bool = False) -> List[str]:
        """
        Get subclasses (children) of a class.
        
        Args:
            class_name: WorldKG class name
            recursive: If True, return all descendants (transitive closure)
        """
        direct_subclasses = self.redis_client.lrange(
            self._class_key(class_name, 'subclasses'), 0, -1
        )
        
        if not recursive:
            return direct_subclasses
        
        # BFS to get all descendants
        all_descendants = set()
        queue = list(direct_subclasses)
        
        while queue:
            current = queue.pop(0)
            if current in all_descendants:
                continue
            all_descendants.add(current)
            children = self.redis_client.lrange(
                self._class_key(current, 'subclasses'), 0, -1
            )
            queue.extend(children)
        
        return list(all_descendants)
    
    def get_depth(self, class_name: str) -> int:
        """Get ontology depth of a class (0=root, higher=more specific)."""
        depth_str = self.redis_client.get(self._class_key(class_name, 'depth'))
        return int(depth_str) if depth_str else 0
    
    def get_canonical_tags(self, class_name: str) -> Dict[str, str]:
        """Get canonical OSM tags for a WorldKG class."""
        tags_json = self.redis_client.get(self._class_key(class_name, 'canonical_tags'))
        return json.loads(tags_json) if tags_json else {}

    def get_canonical_osm_key(self, class_name: str) -> Optional[str]:
        """Get the OSM key that defines this class (e.g., 'amenity' for wkgs:Restaurant)."""
        return self.redis_client.get(self._class_key(class_name, 'canonical_osm_key'))

    def get_canonical_osm_value(self, class_name: str) -> Optional[str]:
        """Get the OSM value that defines this class (e.g., 'restaurant' for wkgs:Restaurant)."""
        return self.redis_client.get(self._class_key(class_name, 'canonical_osm_value'))

    def get_wikidata_equivalent(self, class_name: str) -> Optional[str]:
        """Get Wikidata equivalent class URI via NCA alignment (owl:equivalentClass)."""
        return self.redis_client.get(self._class_key(class_name, 'wikidata_equivalent'))

    def get_dbpedia_equivalent(self, class_name: str) -> Optional[str]:
        """Get DBpedia equivalent class URI via NCA alignment (owl:equivalentClass)."""
        return self.redis_client.get(self._class_key(class_name, 'dbpedia_equivalent'))
    
    def get_all_classes(self) -> Set[str]:
        """Get set of all WorldKG classes in the ontology."""
        return self.redis_client.smembers(f"{self._cache_prefix}:ontology:all_classes")
    
    def is_subclass_of(self, child: str, parent: str) -> bool:
        """
        Check if child is a subclass of parent (transitive).
        
        Returns True if child == parent or child is descendant of parent.
        """
        if child == parent:
            return True
        
        superclasses = self.get_superclasses(child)
        if parent in superclasses:
            return True
        
        # Recursive check
        for superclass in superclasses:
            if self.is_subclass_of(superclass, parent):
                return True
        
        return False
    
    def get_common_ancestor(self, class1: str, class2: str) -> Optional[str]:
        """
        Find the most specific common ancestor of two classes.
        
        Returns the deepest class that is a superclass of both inputs.
        """
        ancestors1 = self._get_all_ancestors(class1)
        ancestors2 = self._get_all_ancestors(class2)
        
        common = ancestors1 & ancestors2
        if not common:
            return None
        
        # Return the deepest common ancestor
        return max(common, key=lambda c: self.get_depth(c))
    
    def _get_all_ancestors(self, class_name: str) -> Set[str]:
        """Get all ancestors (superclasses) of a class, including itself."""
        ancestors = {class_name}
        queue = [class_name]
        
        while queue:
            current = queue.pop(0)
            parents = self.get_superclasses(current)
            for parent in parents:
                if parent not in ancestors:
                    ancestors.add(parent)
                    queue.append(parent)
        
        return ancestors
    
    def tree_distance(self, class1: str, class2: str) -> int:
        """
        Compute tree distance between two classes.
        
        Distance = depth(class1) + depth(class2) - 2 * depth(LCA)
        where LCA = lowest common ancestor
        
        Used for ontology-aware similarity in DoppelCity.
        """
        if class1 == class2:
            return 0
        
        lca = self.get_common_ancestor(class1, class2)
        if lca is None:
            # No common ancestor - return large distance
            return 999
        
        depth1 = self.get_depth(class1)
        depth2 = self.get_depth(class2)
        depth_lca = self.get_depth(lca)
        
        return depth1 + depth2 - 2 * depth_lca
    
    def pairwise_tree_distances(self, classes: Set[str]) -> Dict[tuple, int]:
        """
        Compute pairwise tree distances for a set of classes.
        
        Returns dict mapping (class1, class2) -> distance.
        Used as ground metric for Earth Mover's Distance in DoppelCity.
        """
        classes_list = list(classes)
        distances = {}
        
        for i, c1 in enumerate(classes_list):
            for c2 in classes_list[i:]:
                dist = self.tree_distance(c1, c2)
                distances[(c1, c2)] = dist
                distances[(c2, c1)] = dist
        
        return distances
    
    def _build_memory_index(self) -> None:
        """
        Build in-memory lookup indices from Redis ontology data.

        Creates two inverted indices for O(1) tag→class lookups:
        - _key_value_index: {osm_key: {osm_value: class_name}}  for exact matches
        - _key_class_index: {osm_key: class_name}                for key-only matches
        - _class_depths:     {class_name: depth}                  for tie-breaking
        - _class_superclasses: {class_name: [parent_classes]}     for hierarchy
        - _class_wikidata:   {class_name: wikidata_uri}           for NCA alignment
        - _legacy_tags_index: {class_name: {tag_key: tag_value}}  for fallback

        This eliminates per-entity Redis round-trips during batch enrichment.
        """
        all_classes = self.get_all_classes()
        if not all_classes:
            logger.warning("_build_memory_index: no classes in Redis, skipping")
            return

        self._key_value_index.clear()
        self._key_class_index.clear()
        self._class_depths.clear()
        self._class_superclasses.clear()
        self._class_wikidata.clear()
        self._legacy_tags_index.clear()
        self._class_to_key_value.clear()
        self._key_class_index_reverse.clear()

        for class_name in all_classes:
            osm_key = self.get_canonical_osm_key(class_name)
            osm_value = self.get_canonical_osm_value(class_name)
            depth = self.get_depth(class_name)
            superclasses = self.get_superclasses(class_name)
            wikidata_uri = self.get_wikidata_equivalent(class_name)
            legacy_tags = self.get_canonical_tags(class_name)

            self._class_depths[class_name] = depth
            if superclasses:
                self._class_superclasses[class_name] = list(superclasses)
            if wikidata_uri:
                self._class_wikidata[class_name] = wikidata_uri
            if legacy_tags:
                self._legacy_tags_index[class_name] = legacy_tags

            if osm_key:
                if osm_value:
                    self._key_value_index.setdefault(osm_key, {})[osm_value] = class_name
                    if class_name not in self._class_to_key_value:
                        self._class_to_key_value[class_name] = (osm_key, osm_value)
                else:
                    self._key_class_index[osm_key] = class_name
                    self._key_class_index_reverse[class_name] = osm_key

        self._index_built = True
        logger.info(
            f"Built in-memory ontology index: "
            f"{len(self._key_value_index)} key→value mappings, "
            f"{len(self._key_class_index)} key-only classes, "
            f"{len(self._class_depths)} total classes"
        )

    def _ensure_index(self) -> None:
        """Build the in-memory index if not already built."""
        if not self._index_built:
            self._build_memory_index()

    def predict_class_from_tags(self, tags: Dict[str, str]) -> Optional[str]:
        """
        Predict WorldKG class from OSM tags using in-memory index.

        Uses pre-built inverted indices for O(entities × entity_tags) instead
        of O(entities × all_classes) with per-class Redis round-trips.

        Matching strategy (same as original, just in-memory):
        - Exact key+value match → depth-2 subclass (highest priority)
        - Key-only match → depth-1 key class (fallback)
        - Legacy canonical_tags match → last resort

        Returns the most specific (deepest) matching class.
        """
        self._ensure_index()

        if not self._index_built:
            return None

        best_class = None
        best_depth = -1

        for osm_key, osm_value in tags.items():
            value_map = self._key_value_index.get(osm_key)
            if value_map:
                cls = value_map.get(osm_value)
                if cls:
                    depth = self._class_depths.get(cls, 0)
                    if depth > best_depth:
                        best_class, best_depth = cls, depth
                continue

            cls = self._key_class_index.get(osm_key)
            if cls:
                depth = self._class_depths.get(cls, 0)
                if depth > best_depth:
                    best_class, best_depth = cls, depth

        if best_class:
            return best_class

        for class_name, canonical in self._legacy_tags_index.items():
            if all(tags.get(k) == v for k, v in canonical.items() if v != '*'):
                depth = self._class_depths.get(class_name, 0)
                if depth > best_depth:
                    best_class, best_depth = class_name, depth

        return best_class
    
    def clear_cache(self) -> None:
        """Clear all WorldKG ontology data from Redis and in-memory index."""
        pattern = f"{self._cache_prefix}:*"
        keys = self.redis_client.keys(pattern)
        if keys:
            self.redis_client.delete(*keys)
        self._index_built = False
        self._key_value_index.clear()
        self._key_class_index.clear()
        self._class_depths.clear()
        self._class_superclasses.clear()
        self._class_wikidata.clear()
        self._legacy_tags_index.clear()
        self._class_to_key_value.clear()
        self._key_class_index_reverse.clear()
        logger.info(f"Cleared {len(keys)} WorldKG ontology keys from Redis")


# Singleton instance
_ontology_service = None

def get_worldkg_ontology_service() -> WorldKGOntologyService:
    """Get singleton instance of WorldKG ontology service."""
    global _ontology_service
    if _ontology_service is None:
        _ontology_service = WorldKGOntologyService()
    return _ontology_service
