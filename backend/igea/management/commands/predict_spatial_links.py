"""
Predict Spatial Links (USLP)

Runs USLP (Unsupervised Spatial Link Prediction) to fill missing object-property
triples in the WorldKG-derived entity graph.

Based on:
    Mann, Dsouza, Yu, Demidova — "Spatial Link Prediction with Spatial and
    Semantic Embeddings", ISWC 2023 Best Paper.

Usage:
    python manage.py predict_spatial_links
    python manage.py predict_spatial_links --threshold 0.7 --limit 50000
    python manage.py predict_spatial_links --snapshot-id <uuid>
"""

# TODO: Refactor and take notes
from django.core.management.base import BaseCommand
from igea.services.spatial_link_prediction import (
    SpatialLinkPredictionService,
)
import logging

logger = logging.getLogger(__name__)


def _lazy_import_torch():
    """Lazily import torch so CUDA errors don't break command import.

    This prevents ImportError / OSError from missing CUDA libs (e.g. libcupti)
    from crashing Celery tasks that call this command when GPU mode is not
    actually requested.
    """
    try:
        import torch  # type: ignore
        return torch
    except Exception as e:  # ImportError or lower-level CUDA errors
        logger.error(
            "USLP: Failed to import torch/CUDA stack; GPU mode will be disabled. "
            "Error: %s",
            e,
        )
        return None


class Command(BaseCommand):
    help = 'Predict spatial entity links using USLP (Mann et al., ISWC 2023)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--threshold',
            type=float,
            default=0.7,
            help='Minimum USLP score to accept a link (default: 0.6)',
        )
        parser.add_argument(
            '--top-k',
            type=int,
            default=5,
            help='Max predicted links per (entity, relation) pair (default: 5)',
        )
        parser.add_argument(
            '--limit',
            type=int,
            default=10_000_000,
            help='Max candidate pool entities to load (default: 200000)',
        )
        parser.add_argument(
            '--snapshot-id',
            type=str,
            default=None,
            help='Only predict for entities from this TemporalSnapshot UUID',
        )
        parser.add_argument(
            '--snapshot-date',
            type=str,
            default=None,
            help='Phase 6 partition key (YYYY_MM_DD) for partition pruning',
        )
        parser.add_argument(
            '--country',
            type=str,
            default=None,
            help='Filter to entities in specific country (e.g., LU)',
        )
        parser.add_argument(
            '--poly-file',
            type=str,
            default=None,
            help='Path to .poly boundary file for exact polygon filtering (more accurate than --country)',
        )
        parser.add_argument(
            '--max-heads',
            type=int,
            default=10_000_000,
            help='Max head entities to process (default: 50000)',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Score links but do not persist to DB',
        )
        parser.add_argument(
            '--gpu',
            action='store_true',
            help='Use GPU acceleration (requires CUDA)',
        )
        parser.add_argument(
            '--gpu-device',
            type=str,
            default='cuda:0',
            help='GPU device to use (default: cuda:0)',
        )
        parser.add_argument(
            '--fp64',
            action='store_true',
            help='Use FP64 precision for haversine (recommended for K80)',
        )
        parser.add_argument(
            '--session-id',
            type=str,
            default=None,
            help='ProcessingSession ID for websocket updates',
        )

    def handle(self, *args, **options):
        threshold = options['threshold']
        top_k = options['top_k']
        limit = options['limit']
        snapshot_id = options.get('snapshot_id')
        snapshot_date = options.get('snapshot_date')
        country = options.get('country')
        poly_file = options.get('poly_file')
        max_heads = options['max_heads']
        dry_run = options['dry_run']
        use_gpu = options.get('gpu', False)
        gpu_device = options.get('gpu_device', 'cuda:0')
        use_fp64 = options.get('fp64', False)
        session_id = options.get('session_id')

        # WebSocket support
        if session_id:
            from asgiref.sync import async_to_sync
            from channels.layers import get_channel_layer

            def _push_ws(msg_type, **kwargs):
                try:
                    channel_layer = get_channel_layer()
                    group_name = f'pipeline_{session_id}'
                    async_to_sync(channel_layer.group_send)(
                        group_name,
                        {'type': msg_type, **kwargs}
                    )
                except Exception as exc:
                    self.stdout.write(self.style.WARNING(f'Channel push failed: {exc}'))
        else:
            _push_ws = None

        # GPU availability check
        torch_mod = None
        if use_gpu:
            torch_mod = _lazy_import_torch()
            if torch_mod is None or not torch_mod.cuda.is_available():
                self.stdout.write(self.style.WARNING(
                    "⚠️  GPU requested but CUDA not available or torch import failed. "
                    "Falling back to CPU."
                ))
                use_gpu = False

        self.stdout.write(self.style.SUCCESS(
            f"\n{'='*60}\n"
            f"USLP Spatial Link Prediction\n"
            f"  threshold={threshold}, top_k={top_k}, limit={limit}\n"
            f"  GPU: {'Yes (' + gpu_device + ')' if use_gpu else 'No (CPU)'}\n"
            f"{'='*60}\n"
        ))

        # Send websocket start event
        if _push_ws:
            _push_ws('step_update',
                    name='predict_triples',
                    status='in_progress',
                    message='Starting USLP spatial link prediction',
                    pct=0)

        # Initialize service (GPU or CPU)
        if use_gpu:
            from igea.services.torch_uslp_service import TorchUSLP
            service = TorchUSLP(device=gpu_device, use_fp64=use_fp64)
            
            # Print GPU info
            gpu_info = service.get_gpu_info()
            if gpu_info.get('cuda_available'):
                self.stdout.write(self.style.SUCCESS(
                    f"🎮 GPU: {gpu_info['device_name']}\n"
                    f"   Device: {gpu_info['device']}\n"
                    f"   Memory: {gpu_info['memory_allocated_gb']:.2f} GB allocated"
                ))
        else:
            service = SpatialLinkPredictionService()

        # Parse poly-file to WKT if provided (must happen before pool load)
        polygon_wkt = None
        if poly_file:
            from worldkg_nca.services.wikidata_service import parse_poly_to_wkt
            polygon_wkt = parse_poly_to_wkt(poly_file)
            if polygon_wkt:
                self.stdout.write(f"  Using polygon geofence from {poly_file}")
            else:
                self.stdout.write(self.style.WARNING(
                    f"Could not parse poly-file for geofencing: {poly_file}"
                ))

        # Load candidate pool (scoped by polygon or country if provided)
        self.stdout.write("[1/3] Loading candidate entity pool from DB...")
        pool_size = service.load_candidate_pool_from_db(
            limit=limit, polygon_wkt=polygon_wkt, country=country,
            snapshot_id=snapshot_date,
        )
        self.stdout.write(self.style.SUCCESS(f"  ✓ Pool: {pool_size:,} candidates"))

        if _push_ws:
            _push_ws('step_update',
                    name='predict_triples',
                    status='in_progress',
                    message=f'Loaded {pool_size:,} candidate entities',
                    pct=30)

        if pool_size == 0:
            self.stdout.write(self.style.WARNING(
                "  No candidates found. Ensure OSM entities with coordinates exist."
            ))
            if _push_ws:
                _push_ws('step_update',
                        name='predict_triples',
                        status='failed',
                        message='No candidate entities found',
                        pct=0)
            return

        # Load head entities (those with spatial literal tags)
        self.stdout.write("[2/3] Loading head entities with spatial literal tags...")
        
        from worldkg_nca.models import OsmEntity
        from django.contrib.gis.geos import GEOSGeometry

        # Spatial literal tag keys used to identify head entities
        spatial_keys = {
            'is_in', 'is_in:country', 'is_in:state', 'is_in:county',
            'addr:country', 'addr:state', 'addr:county', 'addr:city',
            'addr:suburb', 'addr:hamlet', 'addr:village', 'addr:town',
        }

        # Only fetch fields needed for head-entity filtering
        qs = (
            OsmEntity.objects.using('vectors')
            .only('osm_id', 'geom', 'tags', 'source_snapshot_id')
            .filter(geom__isnull=False)
        )

        # Phase 6: use VARCHAR snapshot_id partition key for pruning
        if snapshot_date:
            qs = qs.filter(snapshot_id=snapshot_date)
        elif snapshot_id:
            qs = qs.filter(source_snapshot_id=snapshot_id)

        # Apply polygon filter if provided
        if polygon_wkt:
            poly = GEOSGeometry(polygon_wkt, srid=4326)
            qs = qs.filter(geom__within=poly)

        # Apply bbox filter from CountryPipelineProfile when --country is specified
        # (same logic as load_candidate_pool_from_db to avoid scanning all 32M entities).
        # We now apply this even when snapshot_id is provided so snapshot-scoped runs
        # still benefit from country-level spatial filtering.
        if country and not polygon_wkt:
            from core.services.planet_init import osm_wikidata_resolver
            try:
                country_upper = country.upper()
                relations = osm_wikidata_resolver.get_country_relations_dict()
                meta = relations.get(country_upper) or relations.get(country)
            except Exception as exc:
                self.stdout.write(self.style.WARNING(
                    f"  get_country_relations_dict failed for {country}: {exc}"
                ))
                meta = None

            if not meta:
                meta = (
                    osm_wikidata_resolver.get_country_by_iso(country)
                    or osm_wikidata_resolver.get_country_by_name(country)
                )

            iso_code = meta.get("iso_code") if meta else None

            if iso_code:
                from django.db import models
                from core.models import CountryPipelineProfile

                profile = (
                    CountryPipelineProfile.objects.filter(
                        models.Q(iso2__iexact=iso_code)
                        | models.Q(iso3__iexact=iso_code)
                    ).first()
                )

                bbox = None
                if profile and getattr(profile, "country_relations_payload", None):
                    bb = profile.country_relations_payload.get("bbox")
                    if bb:
                        try:
                            bbox = (
                                float(bb["min_lon"]),
                                float(bb["min_lat"]),
                                float(bb["max_lon"]),
                                float(bb["max_lat"]),
                            )
                        except Exception as exc:
                            self.stdout.write(self.style.WARNING(
                                f"  Invalid bbox on CountryPipelineProfile for {iso_code}: {exc}"
                            ))

                if bbox:
                    try:
                        min_lon, min_lat, max_lon, max_lat = bbox
                        bbox_wkt = (
                            f"POLYGON(({min_lon} {min_lat}, {max_lon} {min_lat}, "
                            f"{max_lon} {max_lat}, {min_lon} {max_lat}, {min_lon} {min_lat}))"
                        )
                        poly_geom = GEOSGeometry(bbox_wkt, srid=4326)
                        qs = qs.filter(geom__within=poly_geom)
                        self.stdout.write(
                            f"  Using CountryPipelineProfile bbox for head entities: {iso_code}"
                        )
                    except Exception as exc:
                        self.stdout.write(self.style.WARNING(
                            f"  Bbox filter failed for {iso_code}: {exc}"
                        ))

        # Push spatial tag existence filter into the DB to avoid scanning
        # rows that cannot possibly be head entities. On PostgreSQL JSONField
        # maps to jsonb, so tags__has_any_keys uses jsonb ?| operator and can
        # leverage a GIN index when present.
        qs = qs.filter(tags__has_any_keys=list(spatial_keys))

        head_entities = []

        # Pre-resolve country/ISO once instead of per-row to avoid redundant work
        from core.services.planet_init.osm_wikidata_resolver import resolve_iso_code
        country_upper = country.upper() if country else None
        iso_code_for_tags = (
            resolve_iso_code(country).upper()
            if country
            else None
        )
        matches = None
        if country_upper:
            matches = (country_upper, iso_code_for_tags) if iso_code_for_tags else (country_upper,)

        for e in qs.iterator(chunk_size=10_000):
            # Only include if entity has at least one spatial literal tag
            tags = e.tags or {}

            # Apply country filter if specified (fallback if no polygon)
            if country and not polygon_wkt and matches:
                tag_country = (tags.get('addr:country') or '').upper()
                is_in_country = (tags.get('is_in:country') or '').upper()
                is_in = (tags.get('is_in') or '').upper()
                if not any(m in (tag_country, is_in_country) or m in is_in for m in matches):
                    continue

            if any(k in tags for k in spatial_keys):
                head_entities.append({
                    'osm_id': e.osm_id,
                    'lat': e.geom.y,
                    'lon': e.geom.x,
                    'tags': tags,
                })

                # Apply head limit
                if len(head_entities) >= max_heads:
                    self.stdout.write(self.style.WARNING(
                        f"  → Limiting to {max_heads:,} head entities"
                    ))
                    break

        self.stdout.write(self.style.SUCCESS(
            f"  ✓ {len(head_entities):,} entities with spatial literal tags"
        ))

        if _push_ws:
            _push_ws('step_update',
                    name='predict_triples',
                    status='in_progress',
                    message=f'Loaded {len(head_entities):,} head entities with spatial tags',
                    pct=60)

        # Predict
        self.stdout.write("[3/3] Scoring candidate tail entities (USLP)...")
        links = service.predict_links_batch(
            head_entities, threshold=threshold, top_k=top_k
        )
        self.stdout.write(self.style.SUCCESS(f"  ✓ {len(links):,} links accepted"))

        if _push_ws:
            _push_ws('step_update',
                    name='predict_triples',
                    status='in_progress',
                    message=f'Predicted {len(links):,} spatial links',
                    pct=90)

        if not dry_run:
            # Use snapshot_date (YYYY_MM_DD) as the snapshot_id for
            # partition-scoped spatial link queries.  Falls back to the
            # legacy UUID snapshot_id if snapshot_date is not provided.
            persist_snapshot_id = snapshot_date or snapshot_id
            n_saved = service.persist_links(links, snapshot_id=persist_snapshot_id, country_name=country)
            self.stdout.write(self.style.SUCCESS(
                f"\n✓ Persisted {n_saved:,} SpatialLink records"
            ))
        else:
            self.stdout.write(self.style.WARNING(
                f"\n[dry-run] Would persist {len(links):,} links (not saved)"
            ))
            for link in links[:10]:
                self.stdout.write(
                    f"  {link['head_osm_id']} --[{link['relation']}]--> "
                    f"{link['tail_osm_id']}  score={link['score']:.3f}"
                )

        # Send websocket completion event
        if _push_ws:
            _push_ws('step_update',
                    name='predict_triples',
                    status='completed',
                    message=f'USLP complete: {n_saved if not dry_run else len(links)} links',
                    pct=100)

        self.stdout.write(self.style.SUCCESS(
            f"\n{'='*60}\n"
            f"USLP Prediction Complete\n"
            f"{'='*60}\n"
        ))

        # Clean up GPU memory for the Celery worker (only if torch was imported)
        if use_gpu and 'torch_mod' in locals() and torch_mod is not None:
            try:
                torch_mod.cuda.empty_cache()
                self.stdout.write(self.style.SUCCESS("  ✓ Cleared PyTorch CUDA cache"))
            except Exception as e:
                # Don't let cleanup failures break the pipeline
                logger.warning("USLP: Failed to clear CUDA cache: %s", e)
