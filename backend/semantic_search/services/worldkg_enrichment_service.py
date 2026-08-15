import logging
import os
import requests
from typing import Dict, List, Optional, Tuple
from datetime import datetime
from django.contrib.gis.geos import Polygon
from django.db import transaction
from django.utils import timezone

from extraction.services.osm_wikidata_resolver import (
    resolve_country_bbox,
)
from worldkg_nca.models import OsmEntity
from worldkg_nca.services.ontology_service import get_worldkg_ontology_service

logger = logging.getLogger(__name__)


WKGS_BASE_URI = "http://schema.worldkg.org/"
WKG_BASE_URI = "http://www.worldkg.org/"
OSMN_BASE_URI = "https://www.openstreetmap.org/node/"


# ── Parallel enrichment worker (module-level for threading) ─────────────────

def _enrich_chunk_thread(
    service: "WorldKGEnrichmentService",
    chunk: List[Tuple[int, dict]],
) -> List[Tuple[int, Optional[Dict]]]:
    """Enrich a chunk of (osm_id, tags) tuples in a worker thread.

    Uses ThreadPoolExecutor — safe within Celery's daemonic worker
    processes (unlike multiprocessing.Pool which is blocked by Python's
    "daemonic processes are not allowed to have children" assertion).

    The ontology is shared in-process (thread-safe for read-only access),
    so no fork/copy overhead is needed.
    """
    from collections import namedtuple
    _MinimalEntity = namedtuple('_MinimalEntity', ['osm_id', 'tags'])

    results = []
    for osm_id, tags in chunk:
        entity = _MinimalEntity(osm_id=osm_id, tags=tags)
        result = service._enrich_via_local_tags(entity)
        results.append((osm_id, result))

    return results


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
        self.ontology._ensure_index()
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
        Predict WorldKG class using in-memory ontology index.

        Uses the pre-built inverted index for O(1) tag→class lookups.
        Returns the wkgs:-namespaced class plus metadata from the index.
        """
        wkg_class = self.ontology.predict_class_from_tags(entity.tags)

        if not wkg_class:
            return None

        superclasses = self.ontology._class_superclasses.get(wkg_class, [])
        depth = self.ontology._class_depths.get(wkg_class, 0)
        wkg_type_key = None
        wkg_type_value = None
        for osm_key, value_map in self.ontology._key_value_index.items():
            for osm_val, cls in value_map.items():
                if cls == wkg_class:
                    wkg_type_key = osm_key
                    wkg_type_value = osm_val
                    break
            if wkg_type_key:
                break
        if not wkg_type_key:
            for osm_key, cls in self.ontology._key_class_index.items():
                if cls == wkg_class:
                    wkg_type_key = osm_key
                    break
        wikidata_uri = self.ontology._class_wikidata.get(wkg_class)

        return {
            'wkg_class': wkg_class,
            'wkg_superclasses': superclasses,
            'wikidata_uri': wikidata_uri,
            'wkg_depth': depth,
            'wkg_type_key': wkg_type_key,
            'wkg_type_value': wkg_type_value,
            'method': 'local'
        }

    def _parallel_enrich_batch(
        self,
        executor,
        entities: List[OsmEntity],
        num_workers: int,
        batch_timestamp,
    ) -> List[OsmEntity]:
        """Enrich a batch of entities using a ThreadPoolExecutor.

        Extracts ``(osm_id, tags)`` from each entity, splits into
        ``num_workers`` sub-chunks, dispatches to the thread pool, then
        applies results back to the model instances for bulk_update.

        Uses threads (not processes) because Celery worker processes
        are daemonic and Python forbids daemonic processes from
        spawning children.  The ontology is read-only and thread-safe
        for concurrent reads.

        Returns the list of enriched model instances (those with a
        non-None result).
        """
        from concurrent.futures import as_completed

        # Extract (osm_id, tags) tuples.
        items = [(e.osm_id, e.tags) for e in entities]

        # Split into num_workers sub-chunks (roughly equal).
        chunk_size = max(1, len(items) // num_workers)
        chunks = [
            items[i:i + chunk_size]
            for i in range(0, len(items), chunk_size)
        ]

        # Dispatch to thread pool and collect results.
        futures = [
            executor.submit(_enrich_chunk_thread, self, chunk)
            for chunk in chunks
        ]
        chunk_results = [f.result() for f in as_completed(futures)]

        # Build a lookup: {osm_id: result_dict}
        result_map = {}
        for chunk_result in chunk_results:
            for osm_id, result in chunk_result:
                if result is not None:
                    result_map[osm_id] = result

        # Apply results back to model instances.
        enriched = []
        for entity in entities:
            result = result_map.get(entity.osm_id)
            if result:
                entity.wkg_class = result['wkg_class']
                entity.wkg_superclasses = result['wkg_superclasses']
                entity.wikidata_uri = result.get('wikidata_uri')
                entity.wkg_depth = result['wkg_depth']
                entity.wkg_type_key = result.get('wkg_type_key')
                entity.wkg_type_value = result.get('wkg_type_value')
                entity.wkg_enriched_at = batch_timestamp
                enriched.append(entity)

        return enriched

    def batch_enrich_region(
        self,
        region: Optional[str] = None,
        poly_file: Optional[str] = None,
        snapshot_id: Optional[str] = None,
        batch_size: int = 5000,
        use_sparql: bool = False,
        skip_enriched: bool = True,
        limit: Optional[int] = None,
        num_workers: Optional[int] = None,
    ) -> Dict:
        """Batch enrich OSM entities for a region or snapshot.

        This method processes entities in batches and performs a bulk update
        per batch so progress is committed incrementally. This avoids losing
        all work if the database connection drops mid-run.

        When ``use_sparql=False`` (the default) and ``num_workers > 1``,
        enrichment is parallelized across ``num_workers`` threads using
        ``ThreadPoolExecutor``.  Threads are used instead of processes
        because Celery worker processes are daemonic and Python forbids
        daemonic processes from spawning children.  The ontology is
        read-only and thread-safe for concurrent reads.

        Args:
            region: Filter by region name
            poly_file: Path to .poly boundary file for exact geometry filtering.
                       If provided, entities are filtered spatially by this polygon.
            snapshot_id: Filter by source snapshot UUID
            batch_size: Number of entities to process per batch (default 5000)
            use_sparql: Use SPARQL endpoint (slower but more accurate)
            skip_enriched: Skip entities already enriched
            limit: Limit total entities to process (useful for testing)
            num_workers: Number of parallel worker processes for local
                         enrichment.  Defaults to ``ENRICHMENT_WORKERS`` env
                         var or 8.  Set to 1 for serial enrichment.

        Returns:
            Statistics dict
        """
        # Build query and only fetch columns needed for enrichment to reduce I/O
        query = (
            OsmEntity.objects.using('vectors')
            .only(
                'tags',
                'wkg_class',
                'wkg_superclasses',
                'wikidata_uri',
                'wkg_depth',
                'wkg_type_key',
                'wkg_type_value',
                'wkg_enriched_at',
                'source_snapshot_id',
            )
        )
        
        # Spatial filtering
        bbox: Optional[Tuple[float, float, float, float]] = None

        if poly_file:
            from extraction.services.osm_wikidata_resolver import parse_poly_bbox
            bbox = parse_poly_bbox(poly_file)
            if bbox:
                logger.info(
                    "batch_enrich_region: spatial filtering via poly_file=%s "
                    "[bbox=%s]", poly_file, bbox,
                )
            else:
                logger.warning(
                    "batch_enrich_region: could not parse bbox from poly_file=%s",
                    poly_file,
                )
        elif region:
            # resolve_country_bbox now pre-resolves QIDs, country names, and
            # synthetic ISOs to real ISO codes via resolve_iso_code().
            bbox = resolve_country_bbox(region, None)

            if bbox:
                logger.info(
                    "batch_enrich_region: spatial filtering via region=%s [bbox=%s]",
                    region, bbox,
                )
            else:
                logger.warning(
                    "batch_enrich_region: could not resolve bbox for region=%s; "
                    "no spatial filtering applied",
                    region,
                )

        if bbox:
            min_lon, min_lat, max_lon, max_lat = bbox
            polygon = Polygon.from_bbox((min_lon, min_lat, max_lon, max_lat))
            query = query.filter(geom__within=polygon)
        
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

        # Resolve worker count: env var > parameter > default 8.
        # Set to 1 for serial enrichment (e.g. when use_sparql=True).
        if num_workers is None:
            num_workers = int(os.environ.get("ENRICHMENT_WORKERS", "8"))
        if use_sparql:
            num_workers = 1  # SPARQL hits external endpoint — no parallelism

        # Process in batches using server-side cursor for memory efficiency
        total_batches = (total + batch_size - 1) // batch_size if total else 0
        batch_num = 0
        enriched_entities = []
        batch_timestamp = timezone.now()

        if num_workers > 1:
            # ── Parallel enrichment path (ThreadPoolExecutor) ──────────
            # Use threads instead of processes because Celery worker
            # processes are daemonic and Python forbids daemonic
            # processes from spawning children.  The ontology is
            # read-only and thread-safe for concurrent reads.
            from concurrent.futures import ThreadPoolExecutor

            logger.info(
                f"Parallel enrichment: workers={num_workers} batch_size={batch_size}"
            )

            # Collect entities from the server-side cursor into batches,
            # then dispatch each batch to the thread pool as sub-chunks.
            current_batch = []
            executor = ThreadPoolExecutor(max_workers=num_workers)
            try:
                for entity in query.iterator(chunk_size=batch_size):
                    current_batch.append(entity)

                    if len(current_batch) >= batch_size:
                        enriched_entities = self._parallel_enrich_batch(
                            executor, current_batch, num_workers, batch_timestamp,
                        )
                        OsmEntity.objects.using('vectors').bulk_update(
                            enriched_entities,
                            ['wkg_class', 'wkg_superclasses', 'wikidata_uri', 'wkg_depth',
                             'wkg_type_key', 'wkg_type_value', 'wkg_enriched_at']
                        )
                        batch_num += 1
                        logger.info(
                            f"Batch {batch_num}/{total_batches}: "
                            f"{len(enriched_entities)} enriched"
                        )
                        self.stats['enriched'] += len(enriched_entities)
                        self.stats['failed'] += len(current_batch) - len(enriched_entities)
                        self.stats['local_predictions'] += len(enriched_entities)
                        current_batch = []
                        enriched_entities = []
                        batch_timestamp = timezone.now()

                # Flush remaining
                if current_batch:
                    enriched_entities = self._parallel_enrich_batch(
                        executor, current_batch, num_workers, batch_timestamp,
                    )
                    OsmEntity.objects.using('vectors').bulk_update(
                        enriched_entities,
                        ['wkg_class', 'wkg_superclasses', 'wikidata_uri', 'wkg_depth',
                         'wkg_type_key', 'wkg_type_value', 'wkg_enriched_at']
                    )
                    batch_num += 1
                    logger.info(
                        f"Batch {batch_num}/{total_batches}: "
                        f"{len(enriched_entities)} enriched (final)"
                    )
                    self.stats['enriched'] += len(enriched_entities)
                    self.stats['failed'] += len(current_batch) - len(enriched_entities)
                    self.stats['local_predictions'] += len(enriched_entities)
            finally:
                executor.shutdown(wait=True)
        else:
            # ── Serial enrichment path (legacy) ────────────────────────
            for entity in query.iterator(chunk_size=batch_size):
                result = self.enrich_entity(entity, use_sparql=use_sparql)

                if result:
                    entity.wkg_class = result['wkg_class']
                    entity.wkg_superclasses = result['wkg_superclasses']
                    entity.wikidata_uri = result.get('wikidata_uri')
                    entity.wkg_depth = result['wkg_depth']
                    entity.wkg_type_key = result.get('wkg_type_key')
                    entity.wkg_type_value = result.get('wkg_type_value')
                    entity.wkg_enriched_at = batch_timestamp
                    enriched_entities.append(entity)
                    self.stats['enriched'] += 1
                    if not use_sparql:
                        self.stats['local_predictions'] += 1
                    else:
                        self.stats['sparql_queries'] += 1
                else:
                    self.stats['failed'] += 1

                if len(enriched_entities) >= batch_size:
                    OsmEntity.objects.using('vectors').bulk_update(
                        enriched_entities,
                        ['wkg_class', 'wkg_superclasses', 'wikidata_uri', 'wkg_depth',
                         'wkg_type_key', 'wkg_type_value', 'wkg_enriched_at']
                    )
                    batch_num += 1
                    logger.info(
                        f"Batch {batch_num}/{total_batches}: "
                        f"{len(enriched_entities)} enriched"
                    )
                    enriched_entities = []
                    batch_timestamp = timezone.now()

            # Flush remaining
            if enriched_entities:
                OsmEntity.objects.using('vectors').bulk_update(
                    enriched_entities,
                    ['wkg_class', 'wkg_superclasses', 'wikidata_uri', 'wkg_depth',
                     'wkg_type_key', 'wkg_type_value', 'wkg_enriched_at']
                )
                batch_num += 1
                logger.info(
                    f"Batch {batch_num}/{total_batches}: "
                    f"{len(enriched_entities)} enriched (final)"
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

        # Optional spatial / snapshot scoping
        if snapshot_id:
            query = query.filter(source_snapshot_id=snapshot_id)

        if region:
            # Mirror the region to bbox resolution used in batch_enrich_region
            # so that per-country distributions align with enrichment scope.
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
