import logging
import requests
from typing import Dict, List, Optional, Tuple
from datetime import datetime
import warnings

from django.contrib.gis.geos import Polygon
from django.db import transaction
from django.utils import timezone

from extraction.services.osm_wikidata_resolver import (
    resolve_country_bbox,
)
from worldkg_nca.models import OsmEntity
from .ontology_service import get_worldkg_ontology_service

warnings.warn(
    "worldkg_nca.services.enrichment_service is deprecated. "
    "Use semantic_search.services.worldkg_enrichment_service instead.",
    DeprecationWarning,
    stacklevel=2,
)

logger = logging.getLogger(__name__)

# TODO: Load these from a config file
WKGS_BASE_URI = "http://schema.worldkg.org/"
WKG_BASE_URI = "http://www.worldkg.org/"
OSMN_BASE_URI = "https://www.openstreetmap.org/node/"


class WorldKGEnrichmentService:
    """
    Service for enriching OSM entities with WorldKG semantic type assertions.

    Two enrichment modes:
    1. SPARQL endpoint queries against https://www.worldkg.org/sparql (online)
    2. Local OSM key/value matching against ontology cache (offline)

    WorldKG RDF schema (Dsouza et al. CIKM 2021):
    - wkgs:  = http://schema.worldkg.org/   (classes + properties)
    - wkg:   = http://www.worldkg.org/       (entity instance URIs)
    - osmn:  = https://www.openstreetmap.org/node/  (OSM node links)
    - Entity typed via rdf:type wkgs:{ClassName}
    - Entity linked back to OSM via wkgs:osmLink osmn:{osm_id}
    - Geometry linked via wkgs:spatialObject -> sf:Point with geo:asWKT
    - Wikidata alignment via owl:equivalentClass on the *class* (not instance)

    The service populates on OsmEntity:
    - wkg_class:        'wkgs:Restaurant' (correct wkgs: namespace)
    - wkg_superclasses: ['wkgs:Amenity', 'wkgs:WKGObject']
    - wikidata_uri:     Wikidata class URI from NCA alignment
    - wkg_depth:        0=WKGObject, 1=key class, 2=value subclass
    - wkg_type_key:     OSM key that asserted the type (e.g., 'amenity')
    - wkg_type_value:   OSM value that asserted the type (e.g., 'restaurant')
    """
    
    SPARQL_ENDPOINT = "https://www.worldkg.org/sparql"
    TIMEOUT = 10  # seconds
    
    def __init__(self):
        self.ontology = get_worldkg_ontology_service()
        self.stats = {
            'enriched': 0,
            'failed': 0,
            'skipped': 0,
            'sparql_queries': 0,
            'local_predictions': 0
        }
    
    def enrich_entity(self, entity: OsmEntity, use_sparql: bool = False) -> Dict:
        """
        Enrich a single OSM entity with WorldKG classification.
        
        Args:
            entity: OsmEntity instance
            use_sparql: If True, query WorldKG SPARQL endpoint; else use local prediction
        
        Returns:
            Dict with enrichment results: {
                'wkg_class': str,
                'wkg_superclasses': List[str],
                'wikidata_uri': Optional[str],
                'wkg_depth': int,
                'method': 'sparql' | 'local'
            }
        """
        if use_sparql:
            result = self._enrich_via_sparql(entity)
            if result:
                self.stats['sparql_queries'] += 1
                return result
            # Fallback to local if SPARQL fails
            logger.warning(f"SPARQL failed for {entity}, falling back to local prediction")
        
        # Local tag-based prediction
        result = self._enrich_via_local_tags(entity)
        if result:
            self.stats['local_predictions'] += 1
        return result
    
    def _enrich_via_sparql(self, entity: OsmEntity) -> Optional[Dict]:
        """
        Query WorldKG SPARQL endpoint for entity classification using the correct schema.

        WorldKG entities are identified by OSM node ID via wkgs:osmLink.
        Classification is stored as rdf:type assertions (not custom properties).
        Wikidata alignment is owl:equivalentClass on the class (NCA result).

        Correct SPARQL structure:
            ?entity wkgs:osmLink osmn:{osm_id} ;
                    rdf:type ?wkgsClass .
            OPTIONAL { ?wkgsClass owl:equivalentClass ?wikidataClass }
        """
        try:
            if not entity.osm_id:
                return None

            query = f"""
            PREFIX wkgs: <{WKGS_BASE_URI}>
            PREFIX osmn: <{OSMN_BASE_URI}>
            PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
            PREFIX owl: <http://www.w3.org/2002/07/owl#>

            SELECT ?wkgsClass ?wikidataClass WHERE {{
                ?entity wkgs:osmLink osmn:{entity.osm_id} ;
                        rdf:type ?wkgsClass .
                OPTIONAL {{
                    ?wkgsClass owl:equivalentClass ?wikidataClass .
                    FILTER(STRSTARTS(STR(?wikidataClass), "http://www.wikidata.org/"))
                }}
                FILTER(STRSTARTS(STR(?wkgsClass), "{WKGS_BASE_URI}"))
            }}
            LIMIT 1
            """

            response = requests.post(
                self.SPARQL_ENDPOINT,
                data={'query': query},
                headers={'Accept': 'application/sparql-results+json'},
                timeout=self.TIMEOUT
            )

            if response.status_code != 200:
                logger.error(f"SPARQL query failed: {response.status_code} for osm_id={entity.osm_id}")
                return None

            bindings = response.json().get('results', {}).get('bindings', [])
            if not bindings:
                return None

            row = bindings[0]
            # Extract short class name: http://schema.worldkg.org/Restaurant -> wkgs:Restaurant
            wkgs_uri = row['wkgsClass']['value']
            class_short = 'wkgs:' + wkgs_uri[len(WKGS_BASE_URI):]

            wikidata_uri = row.get('wikidataClass', {}).get('value')

            superclasses = self.ontology.get_superclasses(class_short)
            depth = self.ontology.get_depth(class_short)

            # Derive type key/value from ontology cache
            wkg_type_key = self.ontology.get_canonical_osm_key(class_short)
            wkg_type_value = self.ontology.get_canonical_osm_value(class_short)

            return {
                'wkg_class': class_short,
                'wkg_superclasses': superclasses,
                'wikidata_uri': wikidata_uri,
                'wkg_depth': depth,
                'wkg_type_key': wkg_type_key,
                'wkg_type_value': wkg_type_value,
                'method': 'sparql'
            }

        except requests.RequestException as e:
            logger.error(f"SPARQL request error: {e}")
            return None
        except Exception as e:
            logger.error(f"SPARQL parsing error: {e}")
            return None

    def fetch_entities_in_bbox(
        self,
        bbox: List[float],
        class_filter: Optional[str] = None,
        limit: int = 1000
    ) -> List[Dict]:
        """
        Fetch WorldKG entities within a bounding box via GeoSPARQL.

        Uses the live SPARQL endpoint. Useful for seeding a new region.

        Args:
            bbox: [min_lon, min_lat, max_lon, max_lat]
            class_filter: Optional wkgs: class short name (e.g., 'wkgs:Restaurant')
            limit: Max results

        Returns:
            List of dicts with osm_id, wkg_class, wkt geometry
        """
        min_lon, min_lat, max_lon, max_lat = bbox
        wkt_polygon = (
            f"POLYGON(({min_lon} {min_lat}, {max_lon} {min_lat}, "
            f"{max_lon} {max_lat}, {min_lon} {max_lat}, {min_lon} {min_lat}))"
        )

        type_triple = ""
        if class_filter:
            long_uri = WKGS_BASE_URI + class_filter.replace('wkgs:', '')
            type_triple = f"?entity rdf:type <{long_uri}> ."

        query = f"""
        PREFIX wkgs: <{WKGS_BASE_URI}>
        PREFIX osmn: <{OSMN_BASE_URI}>
        PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
        PREFIX geo: <http://www.opengis.net/ont/geosparql#>
        PREFIX geof: <http://www.opengis.net/def/function/geosparql/>
        PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>

        SELECT ?entity ?type ?label ?wkt ?osmLink WHERE {{
            {type_triple}
            ?entity rdf:type ?type ;
                    wkgs:spatialObject ?geom ;
                    wkgs:osmLink ?osmLink .
            OPTIONAL {{ ?entity rdfs:label ?label }}
            ?geom geo:asWKT ?wkt .
            FILTER(geof:sfWithin(?wkt, "{wkt_polygon}"^^geo:wktLiteral))
            FILTER(STRSTARTS(STR(?type), "{WKGS_BASE_URI}"))
        }}
        LIMIT {limit}
        """

        try:
            response = requests.post(
                self.SPARQL_ENDPOINT,
                data={'query': query},
                headers={'Accept': 'application/sparql-results+json'},
                timeout=30
            )
            if response.status_code != 200:
                logger.error(f"GeoSPARQL bbox query failed: {response.status_code}")
                return []

            results = []
            for row in response.json().get('results', {}).get('bindings', []):
                wkgs_uri = row['type']['value']
                class_short = 'wkgs:' + wkgs_uri[len(WKGS_BASE_URI):]
                osm_link = row.get('osmLink', {}).get('value', '')
                osm_id = int(osm_link.split('/')[-1]) if osm_link else None
                results.append({
                    'osm_id': osm_id,
                    'wkg_class': class_short,
                    'label': row.get('label', {}).get('value'),
                    'wkt': row.get('wkt', {}).get('value'),
                })
            return results

        except requests.RequestException as e:
            logger.error(f"GeoSPARQL request error: {e}")
            return []
        except Exception as e:
            logger.error(f"GeoSPARQL parsing error: {e}")
            return []
    
    def _enrich_via_local_tags(self, entity: OsmEntity) -> Optional[Dict]:
        """
        Predict WorldKG class using OSM key/value matching against ontology cache.

        Returns the wkgs:-namespaced class plus the type-asserting OSM key and value
        so callers can populate wkg_type_key / wkg_type_value on OsmEntity.
        """
        wkg_class = self.ontology.predict_class_from_tags(entity.tags)

        if not wkg_class:
            return None

        superclasses = self.ontology.get_superclasses(wkg_class)
        depth = self.ontology.get_depth(wkg_class)
        wkg_type_key = self.ontology.get_canonical_osm_key(wkg_class)
        wkg_type_value = self.ontology.get_canonical_osm_value(wkg_class)
        # Wikidata equivalent class URI via NCA alignment (class-level, not instance)
        wikidata_uri = self.ontology.get_wikidata_equivalent(wkg_class)

        return {
            'wkg_class': wkg_class,
            'wkg_superclasses': superclasses,
            'wikidata_uri': wikidata_uri,
            'wkg_depth': depth,
            'wkg_type_key': wkg_type_key,
            'wkg_type_value': wkg_type_value,
            'method': 'local'
        }
    
    @transaction.atomic(using='vectors')
    def batch_enrich_region(
        self,
        region: Optional[str] = None,
        poly_file: Optional[str] = None,
        snapshot_id: Optional[str] = None,
        batch_size: int = 1000,
        use_sparql: bool = False,
        skip_enriched: bool = True,
        limit: Optional[int] = None
    ) -> Dict:
        """
        Batch enrich OSM entities for a region or snapshot.

        Args:
            region: Filter by region name (ISO code or slug)
            poly_file: Path to .poly boundary file for exact geometry filtering
            snapshot_id: Filter by source snapshot UUID
            batch_size: Number of entities to process per batch
            use_sparql: Use SPARQL endpoint (slower but more accurate)
            skip_enriched: Skip entities already enriched
            limit: Limit total number of entities to process (useful for testing)

        Returns:
            Statistics dict
        """
        # Build query
        query = OsmEntity.objects.using('vectors').all()
        
        # Spatial scoping: poly_file has highest priority, then region name
        bbox: Optional[Tuple[float, float, float, float]] = None

        if poly_file:
            # Direct .poly file resolution (most accurate)
            from extraction.services.osm_wikidata_resolver import parse_poly_bbox
            bbox = parse_poly_bbox(poly_file)
            if not bbox:
                logger.warning(
                    "batch_enrich_region: could not parse bbox from poly_file=%s",
                    poly_file,
                )
        elif region:
            # Fallback to ISO code / region name resolution
            # resolve_country_bbox now pre-resolves QIDs, country names, and
            # synthetic ISOs to real ISO codes via resolve_iso_code().
            bbox = resolve_country_bbox(region, None)

            if bbox:
                min_lon, min_lat, max_lon, max_lat = bbox
                polygon = Polygon.from_bbox((min_lon, min_lat, max_lon, max_lat))
                query = query.filter(geom__within=polygon)
            else:
                logger.warning(
                    "batch_enrich_region: could not resolve bbox for region=%s; "
                    "no spatial filtering applied",
                    region,
                )
        
        if snapshot_id:
            query = query.filter(source_snapshot_id=snapshot_id)
        
        if skip_enriched:
            query = query.filter(wkg_class__isnull=True)

        if limit:
            query = query[:limit]

        total = query.count()
        logger.info(f"Starting batch enrichment of {total} entities")

        self.stats = {
            'enriched': 0,
            'failed': 0,
            'skipped': 0,
            'sparql_queries': 0,
            'local_predictions': 0
        }

        # Process in batches
        for offset in range(0, total, batch_size):
            batch = list(query[offset:offset + batch_size])
            enriched_entities = []
            
            for entity in batch:
                result = self.enrich_entity(entity, use_sparql=use_sparql)
                
                if result:
                    entity.wkg_class = result['wkg_class']
                    entity.wkg_superclasses = result['wkg_superclasses']
                    entity.wikidata_uri = result.get('wikidata_uri')
                    entity.wkg_depth = result['wkg_depth']
                    entity.wkg_type_key = result.get('wkg_type_key')
                    entity.wkg_type_value = result.get('wkg_type_value')
                    entity.wkg_enriched_at = timezone.now()
                    enriched_entities.append(entity)
                    self.stats['enriched'] += 1
                else:
                    self.stats['failed'] += 1

            # Bulk update
            if enriched_entities:
                OsmEntity.objects.using('vectors').bulk_update(
                    enriched_entities,
                    ['wkg_class', 'wkg_superclasses', 'wikidata_uri', 'wkg_depth',
                     'wkg_type_key', 'wkg_type_value', 'wkg_enriched_at']
                )
            
            logger.info(
                f"Batch {offset//batch_size + 1}/{(total + batch_size - 1)//batch_size}: "
                f"{len(enriched_entities)} enriched"
            )
        
        logger.info(f"Batch enrichment complete: {self.stats}")
        return self.stats
    
    def get_entities_by_class(
        self,
        wkg_class: str,
        include_subclasses: bool = True,
        limit: int = 100
    ) -> List[OsmEntity]:
        """
        Retrieve entities belonging to a WorldKG class.

        Args:
            wkg_class: WorldKG class name using wkgs: namespace (e.g., 'wkgs:Cafe')
            include_subclasses: If True, include all subclasses
            limit: Maximum number of entities to return
        
        Returns:
            List of OsmEntity instances
        """
        if include_subclasses:
            # Get all subclasses
            subclasses = self.ontology.get_subclasses(wkg_class, recursive=True)
            class_filter = [wkg_class] + subclasses
            
            return list(
                OsmEntity.objects.using('vectors')
                .filter(wkg_class__in=class_filter)
                .order_by('-wkg_depth')[:limit]
            )
        else:
            return list(
                OsmEntity.objects.using('vectors')
                .filter(wkg_class=wkg_class)[:limit]
            )
    
    def get_class_distribution(
        self,
        snapshot_id: Optional[str] = None,
        region: Optional[str] = None
    ) -> Dict[str, int]:
        """
        Get WorldKG class distribution for a snapshot or region.
        
        Returns:
            Dict mapping class name -> count
        """
        query = OsmEntity.objects.using('vectors').exclude(wkg_class__isnull=True)

        if snapshot_id:
            query = query.filter(source_snapshot_id=snapshot_id)

        if region:
            # Mirror the region → bbox resolution used in batch_enrich_region
            # so that per-country distributions align with enrichment scope.
            bbox: Optional[Tuple[float, float, float, float]] = None

            # resolve_country_bbox now pre-resolves QIDs, country names, and
            # synthetic ISOs to real ISO codes via resolve_iso_code().
            bbox = resolve_country_bbox(region, None)

            if bbox:
                min_lon, min_lat, max_lon, max_lat = bbox
                polygon = Polygon.from_bbox((min_lon, min_lat, max_lon, max_lat))
                query = query.filter(geom__within=polygon)
                logger.info(
                    "get_class_distribution: spatial filtering via region=%s [bbox=%s]",
                    region,
                    bbox,
                )
            else:
                logger.warning(
                    "get_class_distribution: could not resolve bbox for region=%s; "
                    "falling back to unbounded distribution",
                    region,
                )
        
        # Aggregate by class
        from django.db.models import Count
        distribution = query.values('wkg_class').annotate(count=Count('wkg_class'))
        
        return {item['wkg_class']: item['count'] for item in distribution}


# Singleton instance
_enrichment_service = None

def get_worldkg_enrichment_service() -> WorldKGEnrichmentService:
    """Get singleton instance of WorldKG enrichment service."""
    global _enrichment_service
    if _enrichment_service is None:
        _enrichment_service = WorldKGEnrichmentService()
    return _enrichment_service
