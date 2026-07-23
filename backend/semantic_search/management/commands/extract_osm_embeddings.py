"""
Extract OSM entities from PBF and generate GeoVectors embeddings.

Generates both dual embeddings per entity:
  - GV-Tags:  FastText weighted average (immediate, no retraining)
  - GV-NLE:   InductiveSpatialService proximity-weighted mean (no retraining)
              Falls back gracefully if no trained pool exists yet.

Usage:
    python manage.py extract_osm_embeddings --pbf-file=/path/to/file.osm.pbf
    python manage.py extract_osm_embeddings --pbf-file=... --snapshot-id=<uuid>
    python manage.py extract_osm_embeddings --pbf-file=... --no-inductive-nle
"""

from django.core.management.base import BaseCommand
from worldkg_nca.models import OsmEntity
from semantic_search.services.fasttext_service import FastTextEmbeddingService
from semantic_search.services.inductive_spatial_service import InductiveSpatialService
from django.contrib.gis.geos import Point
from django.db import transaction
import osmium
import logging
import uuid as uuid_module
from tqdm import tqdm
import multiprocessing as mp

logger = logging.getLogger(__name__)


class StreamingOsmExtractor(osmium.SimpleHandler):
    """
    Streaming OSM extractor that deduplicates entities by osm_id/type
    and only flushes the latest version found in the PBF stream.
    """

    def __init__(self, chunk_size, flush_callback):
        super().__init__()
        self.chunk_size = chunk_size
        self.flush_callback = flush_callback
        self._buf = []
        # Key: (type, id) -> version (compact map for deduplication)
        self._version_map = {}
        self.count = 0
        self.unique_count = 0

    def _push(self, record):
        self.count += 1
        key = (record['osm_type'], record['osm_id'])
        version = record['version']
        
        # In-memory deduplication: only keep the record if it's potentially the latest
        # This still requires holding the record in _buf until chunk is full.
        # For historical files, we might see multiple versions.
        # To truly stream, we should just push everything and let the DB handle it via update_conflicts.
        # But to optimize, we can deduplicate within the current chunk.
        
        self._buf.append(record)
        
        if len(self._buf) >= self.chunk_size:
            self.flush_buffer()

        if self.count % 100000 == 0:
            logger.info(f"  Streaming: {self.count} records seen...")

    def flush_buffer(self):
        """Flush the current buffer to the callback."""
        if not self._buf:
            return
            
        # Optional: intra-chunk deduplication to reduce overhead
        latest_in_chunk = {}
        for rec in self._buf:
            key = (rec['osm_type'], rec['osm_id'])
            if key not in latest_in_chunk or rec['version'] > latest_in_chunk[key]['version']:
                latest_in_chunk[key] = rec
        
        chunk = list(latest_in_chunk.values())
        self.unique_count += len(chunk)
        self.flush_callback(chunk)
        self._buf = []

    def flush_all(self):
        """Final flush."""
        self.flush_buffer()
        logger.info(f"Finished streaming. {self.count} records processed, {self.unique_count} unique-ish entities sent.")

    def node(self, n):
        if len(n.tags) > 0:
            self._push({
                'osm_type': 'node',
                'osm_id': n.id,
                'tags': dict(n.tags),
                'lon': n.location.lon,
                'lat': n.location.lat,
                'version': n.version,
                'timestamp': n.timestamp,
            })

    def way(self, w):
        if len(w.tags) > 0:
            coords = []
            for n in w.nodes:
                try:
                    coords.append((n.lon, n.lat))
                except (osmium.InvalidLocationError, AttributeError):
                    pass
            
            lon, lat = None, None
            if coords:
                lon = sum(c[0] for c in coords) / len(coords)
                lat = sum(c[1] for c in coords) / len(coords)

            self._push({
                'osm_type': 'way',
                'osm_id': w.id,
                'tags': dict(w.tags),
                'lon': lon,
                'lat': lat,
                'version': w.version,
                'timestamp': w.timestamp,
            })

    def relation(self, r):
        if len(r.tags) > 0:
            self._push({
                'osm_type': 'relation',
                'osm_id': r.id,
                'tags': dict(r.tags),
                'version': r.version,
                'timestamp': r.timestamp,
            })


_ft_service_instance = None
_inductive_svc_instance = None


def _init_ft_worker():
    """
    Pool initializer: load the FastText model once per worker process.
    Using fork (Linux default) the parent already has the model in memory
    and workers inherit it via copy-on-write — so this is effectively free.
    Declared at module level so it is picklable.
    """
    global _ft_service_instance
    _ft_service_instance = FastTextEmbeddingService()


def _embed_entity_worker(entity_data):
    """
    Generate dual embeddings for a single entity (runs inside parallel worker processes).
    Must be a module-level function so mp.Pool can pickle it.
    """
    global _ft_service_instance, _inductive_svc_instance
    if _ft_service_instance is None:
        _ft_service_instance = FastTextEmbeddingService()
        
    tags = entity_data['tags']
    tag_counts = {f"{k}={v}": 1 for k, v in tags.items()}
    entity_data['gv_tags_embedding'] = _ft_service_instance.calculate_embedding(tag_counts)
    entity_data['gv_nle_embedding'] = None
    
    if _inductive_svc_instance is not None:
        if entity_data.get('lat') is not None and entity_data.get('lon') is not None:
            emb = _inductive_svc_instance.embed_entity(
                entity_data['lat'], entity_data['lon']
            )
            if emb is not None:
                entity_data['gv_nle_embedding'] = emb.tolist()
                
    return entity_data


class Command(BaseCommand):
    help = 'Extract OSM entities from PBF and generate dual GeoVectors embeddings (GV-Tags + GV-NLE)'
    
    def add_arguments(self, parser):
        parser.add_argument(
            '--pbf-file',
            type=str,
            required=True,
            help='Path to OSM PBF file'
        )
        parser.add_argument(
            '--batch-size',
            type=int,
            default=10000,
            help='Batch size for bulk insert (default: 10000)'
        )
        parser.add_argument(
            '--workers',
            type=int,
            default=min(4, mp.cpu_count()),
            help='Number of parallel workers for embedding generation (default: 4)'
        )
        parser.add_argument(
            '--chunk-size',
            type=int,
            default=50000,
            help='Entities processed per streaming chunk (controls peak RAM, default: 50000)'
        )
        parser.add_argument(
            '--limit',
            type=int,
            default=None,
            help='Limit number of entities to process (for testing)'
        )
        parser.add_argument(
            '--snapshot-id',
            type=str,
            default=None,
            help='TemporalSnapshot UUID to tag entities with for provenance tracking',
        )
        parser.add_argument(
            '--no-inductive-nle',
            action='store_true',
            help='Skip inductive GV-NLE computation (faster; NLE left null until train_gv_nle)',
        )
        parser.add_argument(
            '--websocket-group',
            type=str,
            default=None,
            help='Channels group name for progress updates'
        )
        parser.add_argument(
            '--total-entities',
            type=int,
            default=None,
            help='Total entities for percentage calculation'
        )
    
    def handle(self, *args, **options):
        pbf_file = options['pbf_file']
        batch_size = options['batch_size']
        num_workers = options['workers']
        chunk_size = options['chunk_size']
        limit = options['limit']
        snapshot_id_str = options.get('snapshot_id')
        skip_inductive = options.get('no_inductive_nle', False)
        self._websocket_group = options.get('websocket_group')
        self._total_entities = options.get('total_entities')

        snapshot_id = None
        if snapshot_id_str:
            try:
                snapshot_id = uuid_module.UUID(snapshot_id_str)
            except ValueError:
                self.stdout.write(self.style.ERROR(f"Invalid snapshot-id UUID: {snapshot_id_str}"))
                return

        self.stdout.write(f"Extracting OSM entities from: {pbf_file}")
        self.stdout.write(f"Chunk size: {chunk_size}, Batch size: {batch_size}, Workers: {num_workers}")
        if snapshot_id:
            self.stdout.write(f"Snapshot ID: {snapshot_id}")

        # Inductive NLE service loaded once (single-process, requires pool)
        inductive_svc = None
        if not skip_inductive:
            self.stdout.write("\nLoading inductive GV-NLE pool from DB...")
            inductive_svc = InductiveSpatialService(k=50)
            pool_size = inductive_svc.load_pool_from_db()
            if pool_size == 0:
                self.stdout.write(self.style.WARNING(
                    "  No trained GV-NLE pool found — skipping inductive NLE. "
                    "(This is normal for initial runs. Step 9 will generate the authoritative pool later.)"
                ))
                inductive_svc = None
            else:
                self.stdout.write(f"  Pool size: {pool_size:,} trained entities")

        self._inserted = 0
        self._gv_nle_count = 0
        self._seen = 0
        self._limit = limit
        self._batch_size = batch_size
        self._num_workers = num_workers
        self._snapshot_id = snapshot_id
        self._inductive_svc = inductive_svc
        
        # Expose exactly to workers via global (Memory copy-on-write in Linux forks)
        global _inductive_svc_instance
        _inductive_svc_instance = inductive_svc
        
        self._pool = mp.Pool(
            num_workers,
            initializer=_init_ft_worker,
        )

        self.stdout.write(f"\nStreaming PBF in chunks of {chunk_size}...")
        try:
            extractor = StreamingOsmExtractor(
                chunk_size=chunk_size,
                flush_callback=self._process_chunk,
            )
            extractor.apply_file(pbf_file, locations=True)
            extractor.flush_all()
        finally:
            self._pool.close()
            self._pool.join()

        self.stdout.write(self.style.SUCCESS(
            f"\n✓ Processed {self._inserted:,} OSM entities\n"
            f"  GV-Tags computed:       {self._seen:,}\n"
            f"  GV-NLE (inductive):     {self._gv_nle_count:,}\n"
            f"  GV-NLE (full retrain):  run train_gv_nle for authoritative embeddings"
        ))

    def _process_chunk(self, chunk):
        """Embed and insert one streaming chunk — keeps peak RAM at chunk_size."""
        if self._limit is not None and self._seen >= self._limit:
            return
        if self._limit is not None:
            chunk = chunk[:max(0, self._limit - self._seen)]
        if not chunk:
            return

        self._seen += len(chunk)

        # Use massive chunk sizes for IPC efficiency across 24 cores
        entities_with_tags = list(
            self._pool.imap(_embed_entity_worker, chunk, chunksize=2500)
        )

        for entity in entities_with_tags:
            if entity.get('gv_nle_embedding') is not None:
                self._gv_nle_count += 1

        self._inserted += self.bulk_insert_entities(
            entities_with_tags, self._batch_size, snapshot_id=self._snapshot_id
        )
        self.stdout.write(
            f"  chunk done — total inserted: {self._inserted:,} (seen {self._seen:,})"
        )

        if self._websocket_group and self._total_entities:
            from asgiref.sync import async_to_sync
            from channels.layers import get_channel_layer
            pct = min(100, int((self._seen / self._total_entities) * 100))
            try:
                channel_layer = get_channel_layer()
                async_to_sync(channel_layer.group_send)(
                    self._websocket_group,
                    {
                        'type': 'step_update',
                        'step': 5,
                        'total': 8,
                        'name': 'embed_osm_entities',
                        'status': 'in_progress',
                        'message': f'Ingested {self._inserted:,} / {self._total_entities:,} entities',
                        'pct': pct
                    }
                )
            except Exception:
                pass
    
    def bulk_insert_entities(self, entities_with_embeddings, batch_size, snapshot_id=None):
        """Bulk insert OSM entities with dual embeddings to database using background worker logic."""
        if not entities_with_embeddings:
            return 0
        
        # Prepare all OsmEntity objects
        osm_entities = []
        for entity_data in entities_with_embeddings:
            geom = None
            if entity_data.get('lat') is not None and entity_data.get('lon') is not None:
                geom = Point(entity_data['lon'], entity_data['lat'], srid=4326)

            osm_entity = OsmEntity.create_from_osm(
                osm_type=entity_data['osm_type'],
                osm_id=entity_data['osm_id'],
                tags=entity_data['tags'],
                gv_tags_embedding=entity_data.get('gv_tags_embedding'),
                gv_nle_embedding=entity_data.get('gv_nle_embedding'),
                geom=geom,
                version=entity_data.get('version'),
                timestamp=entity_data.get('timestamp'),
            )
            if snapshot_id is not None:
                osm_entity.source_snapshot_id = snapshot_id
            osm_entities.append(osm_entity)
        
        # Using update_conflicts ensures the LATEST version eventually survives in the DB
        # for historical files, while allowing true streaming processing.
        # We target the 'vectors' database explicitly for consistency.
        with transaction.atomic(using='vectors'):
            OsmEntity.objects.using('vectors').bulk_create(
                osm_entities,
                batch_size=batch_size,
                update_conflicts=True,
                update_fields=['tags', 'gv_tags_embedding', 'gv_nle_embedding', 'geom', 'version', 'timestamp', 'source_snapshot_id'],
                unique_fields=['osm_type', 'osm_id', 'gv_tags_version']
            )
        return len(osm_entities)
