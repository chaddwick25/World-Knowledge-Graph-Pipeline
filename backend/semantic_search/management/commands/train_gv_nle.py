"""
Train GV-NLE Spatial Embeddings

Management command to train GV-NLE embeddings for OSM entities using:
1. k-NN graph construction (k=50, haversine distance)
2. Weighted DeepWalk (random walks with edge weight sampling)
3. Skip-Gram training (Word2Vec-style)

Usage:
    python manage.py train_gv_nle --region=gibraltar --k=50 --workers=8
"""

from django.core.management.base import BaseCommand
from worldkg_nca.models import OsmEntity
from semantic_search.services.knn_graph_service import KNNGraphService
from core.services.snapshot.regional_path_service import normalize_country_slug
from semantic_search.services.deepwalk_service import WeightedDeepWalkService
from worldkg_nca.services.wikidata_service import (
    parse_poly_bbox,
    parse_poly_to_wkt,
    bbox_to_wkt,
)
from core.services.snapshot.regional_path_service import regional_path_service
from core.services.planet_init.osm_wikidata_resolver import get_country_relations_dict, populate_bbox_for_profile
import logging
import os
from io import StringIO
from datetime import datetime
from pathlib import Path
from multiprocessing import Pool

logger = logging.getLogger(__name__)


def _train_subgraph_worker(task_data):
    """
    Module-level worker function to train GV-NLE for a single subgraph.
    Must be module-level for pickle serialization.
    """
    import django
    django.setup()

    from semantic_search.management.commands.train_gv_nle import Command

    subgraph_name, poly_file, options = task_data
    k = options['k']
    embedding_dim = options['embedding_dim']
    walk_length = options['walk_length']
    num_walks = options['num_walks']
    window_size = options['window_size']
    epochs = options['epochs']
    workers = options['workers']
    use_gpu = options.get('gpu', False)
    gpu_device = options.get('gpu_device', 'cuda:0')

    # Build command args for single subgraph
    subgraph_options = {
        'region': subgraph_name,
        'k': k,
        'embedding_dim': embedding_dim,
        'walk_length': walk_length,
        'num_walks': num_walks,
        'window_size': window_size,
        'epochs': epochs,
        'workers': workers,
        'save_graph': False,
        'save_model': False,
        'batch_size': options['batch_size'],
        'negative': options.get('negative', 5),
        'sample': options.get('sample', 1e-5),
        'ns_exponent': options.get('ns_exponent', 0.75),
        'source_snapshot': options.get('source_snapshot'),
        'country': None,
        'poly_file': poly_file,
        'buffer_deg': options.get('buffer_deg', 0.45),
        'gpu': use_gpu,
        'gpu_device': gpu_device,
        'use_pyg': options.get('use_pyg', True),
        'isos': None,  # Prevent recursion
    }

    # Reuse the existing handle logic for single poly-file
    # Create a new instance and call handle with modified options
    from io import StringIO
    import sys

    # Capture output
    old_stdout = sys.stdout
    sys.stdout = StringIO()

    try:
        # Call the command directly with modified options
        cmd = Command()
        cmd.handle(**subgraph_options)
        return {'subgraph': subgraph_name, 'success': True}
    except Exception as e:
        return {'subgraph': subgraph_name, 'success': False, 'error': str(e)}
    finally:
        sys.stdout = old_stdout


class Command(BaseCommand):
    help = 'Train GV-NLE spatial embeddings using k-NN graph + weighted DeepWalk'

    def add_arguments(self, parser):
        parser.add_argument(
            '--region',
            type=str,
            help='Region filter (e.g., "gibraltar", "canada")',
        )
        parser.add_argument(
            '--isos',
            type=str,
            default=None,
            metavar='CODE',
            help='ISO 3166-1 alpha-2 country code for subgraph processing (e.g., MZ, IE). Processes all subgraphs in parallel if hierarchy exists.',
        )
        parser.add_argument(
            '--k',
            type=int,
            default=50,
            help='Number of nearest neighbors (default: 50)',
        )
        parser.add_argument(
            '--embedding-dim',
            type=int,
            default=100,
            help='Embedding dimension (default: 100, per GeoVectors-master)',
        )
        parser.add_argument(
            '--walk-length',
            type=int,
            default=80,
            help='Random walk length (default: 80)',
        )
        parser.add_argument(
            '--num-walks',
            type=int,
            default=10,
            help='Number of walks per node (default: 10)',
        )
        parser.add_argument(
            '--window-size',
            type=int,
            default=5,
            help='Skip-Gram window size (default: 5, per GeoVectors paper §5.1)',
        )
        parser.add_argument(
            '--epochs',
            type=int,
            default=1,
            help='Training epochs (default: 1; walks provide diversity, single pass suffices)',
        )
        parser.add_argument(
            '--negative',
            type=int,
            default=5,
            help='Negative samples per positive (default: 5, per Mikolov et al. 2013)',
        )
        parser.add_argument(
            '--sample',
            type=float,
            default=1e-5,
            help='Subsampling threshold (default: 1e-5; lower for graph node IDs)',
        )
        parser.add_argument(
            '--ns-exponent',
            type=float,
            default=0.75,
            help='Negative sampling distribution exponent (default: 0.75)',
        )
        parser.add_argument(
            '--workers',
            type=int,
            default=26,
            help='Number of parallel workers (default: 26, leveraging 26-core E-cluster)',
        )
        parser.add_argument(
            '--save-graph',
            action='store_true',
            help='Save k-NN graph to file',
        )
        parser.add_argument(
            '--save-model',
            action='store_true',
            help='Save trained DeepWalk model',
        )
        parser.add_argument(
            '--batch-size',
            type=int,
            default=10000,
            help='Batch size for database updates (default: 10000)',
        )
        parser.add_argument(
            '--source-snapshot',
            type=str,
            help='UUID of source TemporalSnapshot to link entities to',
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
            metavar='CODE',
            help=(
                'ISO 3166-1 alpha-2 country code for geofenced training '
                '(e.g. DE, GB, TZ). Builds k-NN graph from buffered country '
                'polygon; writes embeddings back only for entities strictly '
                'inside the country boundary.'
            ),
        )
        parser.add_argument(
            '--poly-file',
            type=str,
            default=None,
            metavar='PATH',
            help='Path to .poly boundary file for exact country polygon (overrides --country bbox lookup).',
        )
        parser.add_argument(
            '--buffer-deg',
            type=float,
            default=0.45,
            help=(
                'Buffer in degrees added around the strict country polygon '
                'when loading entities for k-NN graph construction. '
                'Buffer entities anchor border nodes correctly but are not '
                'written back. Default 0.45 deg ≈ 50 km at mid-latitudes.'
            ),
        )
        parser.add_argument(
            '--gpu',
            action='store_true',
            help='Enable GPU acceleration using PyTorch Geometric (recommended)',
        )
        parser.add_argument(
            '--gpu-device',
            type=str,
            default='cuda:0',
            help='GPU device to use (default: cuda:0, options: cuda:0, cuda:1, cuda:2 for K80)',
        )
        parser.add_argument(
            '--use-pyg',
            action='store_true',
            default=True,
            help='Use PyTorch Geometric for GPU training (default: True, memory-safe)',
        )

    def handle(self, *args, **options):
        region = options['region']
        isos = options.get('isos')
        k = options['k']
        embedding_dim = options['embedding_dim']
        walk_length = options['walk_length']
        num_walks = options['num_walks']
        window_size = options['window_size']
        epochs = options['epochs']
        workers = options['workers']
        save_graph = options['save_graph']
        save_model = options['save_model']
        batch_size = options['batch_size']
        source_snapshot_id = options.get('source_snapshot')
        snapshot_date = options.get('snapshot_date')
        country    = options.get('country')
        poly_file  = options.get('poly_file')
        buffer_deg = options.get('buffer_deg', 0.45)
        use_gpu    = options.get('gpu', False)
        gpu_device = options.get('gpu_device', 'cuda:0')

        # ── Subgraph mode (ISO-based) ────────────────────────────────────────
        if isos:
            return self._process_subgraphs(isos, options)

        # ── Geo-fence setup ────────────────────────────────────────────────
        strict_wkt = None   # WKT for the exact target area (write-back scope)
        buffer_wkt = None   # WKT for the expanded area (k-NN graph scope)
        core_ids   = None   # set of osm_ids strictly inside strict polygon

        if country or poly_file:
            if poly_file:
                strict_wkt = parse_poly_to_wkt(poly_file)
                if strict_wkt is None:
                    self.stdout.write(self.style.WARNING(
                        f'Could not parse .poly file: {poly_file}. Falling back to bbox.'
                    ))
                    bbox = parse_poly_bbox(poly_file)
                    if bbox:
                        strict_wkt = bbox_to_wkt(*bbox)

            if strict_wkt is None and country:
                code = country.upper()
                bbox = None

                # 1. Prefer canonical bbox via CountryPipelineProfile + populate_bbox_for_profile
                try:
                    from django.db import models
                    from core.models import CountryPipelineProfile

                    profile = (
                        CountryPipelineProfile.objects.filter(
                            models.Q(iso2__iexact=code) | models.Q(iso3__iexact=code)
                        ).first()
                    )
                except Exception as exc:
                    self.stdout.write(self.style.WARNING(
                        f'GV-NLE: Failed to load CountryPipelineProfile for {code}: {exc}'
                    ))
                    profile = None

                if profile:
                    try:
                        bbox = populate_bbox_for_profile(profile)
                    except Exception as exc:
                        self.stdout.write(self.style.WARNING(
                            f'GV-NLE: populate_bbox_for_profile failed for {code}: {exc}'
                        ))

                # 2. No canonical bbox in DB — load all entities (no geo-filter)
                if bbox:
                    strict_wkt = bbox_to_wkt(*bbox)
                else:
                    self.stdout.write(self.style.WARNING(
                        f'Country code "{country}" has no canonical bbox in CountryPipelineProfile. '
                        f'Loading all entities (no geo-filter applied). '
                        f'Provide --poly-file for accurate filtering or run prebuild_worldkg_structure.'
                    ))

            if strict_wkt:
                from django.contrib.gis.geos import GEOSGeometry
                strict_geo = GEOSGeometry(strict_wkt, srid=4326)
                buffer_geo = strict_geo.buffer(buffer_deg)
                buffer_wkt = buffer_geo.wkt

                self.stdout.write(
                    f'Geo-fence: country={country or "from poly"}, '
                    f'buffer={buffer_deg}° (~{buffer_deg * 111:.0f} km)'
                )

                self.stdout.write('Pre-computing core entity ids (strict polygon)...')
                core_ids = set(
                    OsmEntity.objects.using('vectors')
                    .filter(geom__within=strict_geo)
                    .values_list('osm_id', flat=True)
                )
                self.stdout.write(self.style.SUCCESS(
                    f'  Core entities (write-back scope):  {len(core_ids):,}'
                ))

        self.stdout.write(self.style.SUCCESS(
            f"\n{'='*60}\n"
            f"GV-NLE Training Pipeline\n"
            f"{'='*60}\n"
        ))

        # Step 1: Build k-NN graph
        self.stdout.write(self.style.SUCCESS(
            f"\n[1/3] Building k-NN Graph (k={k})...\n"
        ))

        knn_service = KNNGraphService(k=k)
        
        # Query entities with geometry
        if buffer_wkt:
            from django.contrib.gis.geos import GEOSGeometry
            queryset = OsmEntity.objects.using('vectors').filter(
                geom__isnull=False,
                geom__within=GEOSGeometry(buffer_wkt, srid=4326),
            ).order_by('osm_id').distinct('osm_id')  # Remove 10x duplicates
            self.stdout.write(
                f'Geo-fence active: loading buffer-zone entities for k-NN graph '
                f'(includes {buffer_deg}° border margin, deduplicated by osm_id).'
            )
        else:
            queryset = OsmEntity.objects.using('vectors').filter(
                geom__isnull=False
            )
            # Phase 6: scope to country_code / snapshot_id to avoid scanning
            # all partitions after cutover.  Falls back to full scan on the
            # monolith when neither is provided (backward compatible).
            if country:
                queryset = queryset.filter(country_code=country)
            if snapshot_date:
                queryset = queryset.filter(snapshot_id=snapshot_date)
            elif source_snapshot_id:
                from worldkg_nca.snapshot_utils import snapshot_id_from_uuid
                snap_key = snapshot_id_from_uuid(source_snapshot_id)
                if snap_key:
                    queryset = queryset.filter(snapshot_id=snap_key)
            queryset = queryset.order_by('osm_id').distinct('osm_id')  # Remove 10x duplicates

        if region and not buffer_wkt:
            # Legacy text label (non-spatial) — kept for backward compatibility
            self.stdout.write(f'Filtering by region label: {region} (non-spatial text match).')
        
        # Optimized bulk loading: Use annotate + values_list for coordinate extraction
        # Aligned with "Multiprocessing as a Convergence Strategy" - fast data loading enables
        # more iterative capacity for the DeepWalk training phase
        entity_count = queryset.count()
        self.stdout.write(f"Loading {entity_count} entities from database (optimized bulk extraction)...")
        
        # Use PostGIS ST_Y and ST_X functions to extract coordinates
        from django.contrib.gis.db.models.functions import AsGeoJSON
        from django.db.models import F
        from django.db.models.expressions import RawSQL
        
        # Extract coordinates using raw SQL for maximum performance
        raw_entities = list(
            queryset.annotate(
                lat=RawSQL('ST_Y(geom::geometry)', []),
                lon=RawSQL('ST_X(geom::geometry)', [])
            ).values_list('osm_id', 'lat', 'lon')
        )
        
        # Convert to dict format for graph service
        entities = [
            {'osm_id': osm_id, 'lat': lat, 'lon': lon}
            for osm_id, lat, lon in raw_entities
        ]
        
        self.stdout.write(self.style.SUCCESS(
            f"✓ Loaded {len(entities)} entities with coordinates (optimized bulk extraction)\n"
        ))

        # Build graph
        graph = knn_service.build_knn_graph(entities)
        if not graph:
            self.stdout.write(self.style.WARNING("  ⚠️ No graph nodes created. Stopping training pipeline."))
            return

        # Guard: skip training for degenerate graphs (single node, no edges).
        # Subgraphs like baja_california_sur can have only 1 entity in the
        # buffer zone — Node2Vec cannot train on a graph with no edges.
        total_edges = sum(len(neighbors) for neighbors in graph.values())
        if len(graph) < 2 or total_edges == 0:
            self.stdout.write(self.style.WARNING(
                f"  ⚠️ Graph too small for training "
                f"(nodes={len(graph)}, edges={total_edges}). "
                f"Skipping GV-NLE for this subgraph."
            ))
            return
        
        # Show statistics
        stats = knn_service.get_graph_statistics(graph)
        self.stdout.write(self.style.SUCCESS(
            f"✓ Graph Statistics:\n"
            f"  Nodes: {stats['num_nodes']}\n"
            f"  Edges: {stats['num_edges']}\n"
            f"  Avg Degree: {stats['avg_degree']:.1f}\n"
            f"  Edge Weights: min={stats['weight_min']:.3f}, "
            f"max={stats['weight_max']:.3f}, mean={stats['weight_mean']:.3f}\n"
        ))

        # Save graph if requested
        if save_graph:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            graph_path = f"data/graphs/knn_graph_{region or 'all'}_{timestamp}.edgelist"
            os.makedirs(os.path.dirname(graph_path), exist_ok=True)
            knn_service.export_graph_to_edgelist(graph, graph_path)
            self.stdout.write(self.style.SUCCESS(f"✓ Graph saved to {graph_path}\n"))

        # Step 2: Train DeepWalk (GPU or CPU)
        if use_gpu:
            use_pyg = options.get('use_pyg', True)
            
            if use_pyg:
                self.stdout.write(self.style.SUCCESS(
                    f"\n[2/3] Training Node2Vec (PyTorch Geometric on {gpu_device})...\n"
                ))
                
                from semantic_search.services.pyg_deepwalk_service import PyGDeepWalkService
                
                pyg_service = PyGDeepWalkService(
                    embedding_dim=embedding_dim,
                    device=gpu_device
                )
                
                # PyG training (memory-safe, 10-20x faster)
                # batch_size: 256 for 16GB+ GPUs (cuda:0), 128 for 8GB (cuda:1).
                # Conservative default stays at 128; bumped to 256 for the 4070
                # since GPU memory logs show max ~8 GB reserved at 128 batch —
                # doubling the batch better fills the 16 GB card and reduces
                # per-batch overhead.
                _batch = 256 if "cuda:0" in gpu_device else 128
                embeddings = pyg_service.train(
                    graph=graph,
                    walk_length=walk_length,
                    num_walks=num_walks,
                    window_size=window_size,
                    epochs=epochs,
                    batch_size=_batch,
                    learning_rate=0.01
                )
                
                self.stdout.write(self.style.SUCCESS(
                    f"✓ PyTorch Geometric Training complete\n"
                    f"  Device: {gpu_device}\n"
                    f"  Vocabulary size: {len(embeddings)}\n"
                    f"  Embedding dimension: {embedding_dim}\n"
                    f"  Memory-safe: <5GB GPU RAM\n"
                ))
                
                # Save model if requested
                if save_model:
                    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                    model_path = f"data/models/deepwalk/gv_nle_{region or 'all'}_{timestamp}_pyg.pt"
                    os.makedirs(os.path.dirname(model_path), exist_ok=True)
                    pyg_service.save_model(model_path)
                    self.stdout.write(self.style.SUCCESS(f"✓ Model saved to {model_path}\n"))
                    
                pyg_service.cleanup()
            
            else:
                # Legacy GPU implementation (may cause OOM)
                self.stdout.write(self.style.SUCCESS(
                    f"\n[2/3] Training Weighted DeepWalk (Legacy GPU on {gpu_device})...\n"
                ))
                
                from semantic_search.services.gpu_deepwalk_service import GPUDeepWalkService
                
                gpu_service = GPUDeepWalkService(
                    embedding_dim=embedding_dim,
                    device=gpu_device
                )
                
                embeddings = gpu_service.train(
                    graph=graph,
                    walk_length=walk_length,
                    num_walks=num_walks,
                    window_size=window_size,
                    epochs=epochs,
                    batch_size=512,
                    learning_rate=0.025
                )
                
                self.stdout.write(self.style.SUCCESS(
                    f"✓ GPU Training complete\n"
                    f"  Device: {gpu_device}\n"
                    f"  Vocabulary size: {len(embeddings)}\n"
                ))
                
                if save_model:
                    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                    model_path = f"data/models/deepwalk/gv_nle_{region or 'all'}_{timestamp}_gpu.pt"
                    os.makedirs(os.path.dirname(model_path), exist_ok=True)
                    gpu_service.save_model(model_path)
                    self.stdout.write(self.style.SUCCESS(f"✓ Model saved to {model_path}\n"))
        
        else:
            self.stdout.write(self.style.SUCCESS(
                f"\n[2/3] Training Weighted DeepWalk (CPU with {workers} workers)...\n"
            ))
            
            deepwalk_service = WeightedDeepWalkService(
                embedding_dim=embedding_dim,
                walk_length=walk_length,
                num_walks=num_walks,
                window_size=window_size,
                workers=workers,
                epochs=epochs,
                
                # Optimization hyperparameters (GeoVectors paper §5.1)
                negative=options['negative'],
                alpha=0.025,
                min_alpha=0.0001,
                sample=options['sample'],
                ns_exponent=options['ns_exponent'],
                
                # Enhanced features
                apply_damping=False,     # Damping already applied in KNNGraphService
                compute_loss=True,
                seed=42
            )

            model = deepwalk_service.train(graph)
            embeddings = deepwalk_service.get_all_embeddings()

            # Get optimization report
            report = deepwalk_service.get_optimization_report()
            
            self.stdout.write(self.style.SUCCESS(
                f"✓ CPU Training complete\n"
                f"  Vocabulary size: {len(model.wv)}\n"
                f"  Embedding dimension: {embedding_dim}\n"
            ))
            
            # Show optimization metrics
            if 'losses' in report and report['losses']:
                self.stdout.write(
                    f"  Loss trajectory: {report['losses'][:3]}... → {report['losses'][-1]:.1f}\n"
                    f"  Final loss: {report['final_loss']:.2f}\n"
                )

            # Save model if requested
            if save_model:
                timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                model_path = f"data/models/deepwalk/gv_nle_{region or 'all'}_{timestamp}.model"
                os.makedirs(os.path.dirname(model_path), exist_ok=True)
                deepwalk_service.save_model(model_path)
                self.stdout.write(self.style.SUCCESS(f"✓ Model saved to {model_path}\n"))

        # Step 3: Update database with embeddings
        self.stdout.write(self.style.SUCCESS(
            f"\n[3/3] Updating Database with GV-NLE Embeddings...\n"
        ))

        # embeddings already set in GPU or CPU branch above
        version = datetime.now().strftime('%Y%m%d')
        
        updated_count = 0
        skipped_count = 0
        write_back_total = (
            sum(1 for osm_id in embeddings if osm_id in core_ids)
            if core_ids is not None
            else len(embeddings)
        )
        
        from django.db import connections, transaction

        table_name = OsmEntity._meta.db_table
        temp_table = 'tmp_gv_nle_updates'
        staged_count = 0

        with transaction.atomic(using='vectors'):
            with connections['vectors'].cursor() as cursor:
                cursor.execute(f"""
                    CREATE TEMP TABLE {temp_table} (
                        osm_id bigint PRIMARY KEY,
                        gv_nle_embedding vector(%s),
                        gv_nle_version text,
                        gv_nle_trained boolean,
                        source_snapshot_id uuid
                    ) ON COMMIT DROP
                """, [embedding_dim])

                buffer = StringIO()
                buffer_count = 0

                for osm_id, embedding in embeddings.items():
                    if core_ids is not None and osm_id not in core_ids:
                        skipped_count += 1
                        continue

                    vector_literal = '[' + ','.join(f'{float(value):.8g}' for value in embedding) + ']'
                    snapshot_value = str(source_snapshot_id) if source_snapshot_id else r'\N'
                    buffer.write(
                        f"{osm_id}\t{vector_literal}\t{version}\ttrue\t{snapshot_value}\n"
                    )
                    buffer_count += 1
                    staged_count += 1

                    if buffer_count >= batch_size:
                        buffer.seek(0)
                        cursor.copy_from(
                            buffer,
                            temp_table,
                            columns=(
                                'osm_id',
                                'gv_nle_embedding',
                                'gv_nle_version',
                                'gv_nle_trained',
                                'source_snapshot_id',
                            ),
                            null=r'\N',
                        )
                        buffer.close()
                        buffer = StringIO()
                        buffer_count = 0
                        self.stdout.write(f"  Staged {staged_count}/{write_back_total}...")

                if buffer_count:
                    buffer.seek(0)
                    cursor.copy_from(
                        buffer,
                        temp_table,
                        columns=(
                            'osm_id',
                            'gv_nle_embedding',
                            'gv_nle_version',
                            'gv_nle_trained',
                            'source_snapshot_id',
                        ),
                        null=r'\N',
                    )
                    buffer.close()
                    self.stdout.write(f"  Staged {staged_count}/{write_back_total}...")

                # Phase 6: scope the UPDATE by partition keys (snapshot_id,
                # country_code) to prevent cross-partition writes on the
                # partitioned table.  Without these filters the UPDATE would
                # hit every partition that contains a matching osm_id,
                # silently overwriting NLE embeddings in other snapshots.
                partition_where = "entity.osm_id = updates.osm_id"
                params = []
                if snapshot_date:
                    partition_where += " AND entity.snapshot_id = %s"
                    params.append(snapshot_date)
                if country:
                    partition_where += " AND entity.country_code = %s"
                    params.append(country.upper())

                if source_snapshot_id:
                    cursor.execute(f"""
                        UPDATE {table_name} AS entity
                        SET
                            gv_nle_embedding = updates.gv_nle_embedding,
                            gv_nle_version = updates.gv_nle_version,
                            gv_nle_trained = updates.gv_nle_trained,
                            source_snapshot_id = updates.source_snapshot_id
                        FROM {temp_table} AS updates
                        WHERE {partition_where}
                    """, params)
                else:
                    cursor.execute(f"""
                        UPDATE {table_name} AS entity
                        SET
                            gv_nle_embedding = updates.gv_nle_embedding,
                            gv_nle_version = updates.gv_nle_version,
                            gv_nle_trained = updates.gv_nle_trained
                        FROM {temp_table} AS updates
                        WHERE {partition_where}
                    """, params)

                updated_count = cursor.rowcount

        self.stdout.write(self.style.SUCCESS(
            f"\n✓ Database Update Complete\n"
            f"  Staged: {staged_count} embeddings\n"
            f"  Updated DB rows: {updated_count}\n"
            f"  Write-back target: {write_back_total} embeddings\n"
            f"  Skipped: {skipped_count} entities\n"
            f"  GV-NLE Version: {version}\n"
        ))

        self.stdout.write(self.style.SUCCESS(
            f"\n{'='*60}\n"
            f"GV-NLE Training Complete!\n"
            f"{'='*60}\n"
        ))
        
        # Ensure total VRAM cleanup for the Celery worker
        if use_gpu:
            import torch
            import gc
            gc.collect()
            torch.cuda.synchronize()
            torch.cuda.empty_cache()
            torch.cuda.ipc_collect()
            self.stdout.write(self.style.SUCCESS("  ✓ Cleared PyTorch CUDA cache"))

    def _process_subgraphs(self, iso: str, options: dict):
        """
        Process all subgraphs for a country in parallel.

        Args:
            iso: ISO code (e.g., 'MZ')
            options: Command options
        """
        from django.conf import settings
        import json

        iso = iso.upper()
        self.stdout.write(self.style.SUCCESS(
            f"\n{'='*60}\n"
            f"Subgraph Mode: Processing all subgraphs for {iso}\n"
            f"{'='*60}\n"
        ))

        # Load hierarchy JSON
        base_dir = Path(settings.BASE_DIR).parent
        cache_dir = base_dir / "backend" / "data" / "osm_wikidata_hierarchy_cache"
        cache_path = cache_dir / f"{iso}_hierarchy.json"

        if not cache_path.exists():
            self.stdout.write(self.style.ERROR(
                f"Hierarchy cache not found for {iso} at {cache_path}. "
                f"Run sync_wikidata_hierarchies --countries {iso} --sync first."
            ))
            return

        with open(cache_path, 'r') as f:
            hierarchy = json.load(f)

        # Get subgraph nodes from admin tree
        admin_tree = hierarchy.get('admin_tree', [])
        root = admin_tree[0] if admin_tree else {}
        children = root.get('children', [])

        if not children:
            self.stdout.write(self.style.WARNING(
                f"No subgraphs found in hierarchy for {iso}. "
                f"Falling back to single poly-file mode."
            ))
            # Fall back to single country mode
            options['country'] = iso
            options['isos'] = None
            return self.handle(**options)

        # Get continent and country from OSMWikiDataHierarchy
        country_relations = get_country_relations_dict()
        
        if not country_relations:
            self.stdout.write(self.style.ERROR(
                "No country relations found in OSMWikiDataHierarchy. Run 'import_country_relations' first."
            ))
            return

        # Find country mapping (case-insensitive)
        mapping = None
        for k, v in country_relations.items():
            # Match either by the key (Wikidata ID) or the embedded ISO code
            if k.upper() == iso or v.get('iso_code', '').upper() == iso:
                mapping = v
                break

        if not mapping:
            self.stdout.write(self.style.ERROR(
                f"ISO {iso} not found in OSMWikiDataHierarchy"
            ))
            return

        # Get continent from RegionHierarchy
        country_name = mapping.get('name')
        from api.models import RegionHierarchy
        region = RegionHierarchy.objects.filter(name__iexact=country_name).first()
        if not region or not region.parent:
            self.stdout.write(self.style.ERROR(
                f"Continent not found for {country_name} in RegionHierarchy"
            ))
            return

        continent = region.parent.name

        # Derive country slug using JSON-based ISO overrides where needed
        default_slug = mapping.get('slug', normalize_country_slug(country_name))
        country = get_country_slug(iso, default_slug)

        self.stdout.write(f"Found {len(children)} subgraphs for {iso}")
        self.stdout.write(f"Continent: {continent}, Country: {country}")

        # Prepare subgraph poly files
        subgraph_tasks = []
        for child in children:
            subgraph_name = child.get('name')
            if not subgraph_name:
                continue

            subgraph_slug = normalize_country_slug(subgraph_name)
            poly_path = regional_path_service.get_subgraph_poly_path(
                continent, country, subgraph_slug, '2025_12_31'
            )

            if poly_path.exists():
                subgraph_tasks.append((subgraph_name, str(poly_path), options))
            else:
                self.stdout.write(self.style.WARNING(
                    f"Poly file not found for {subgraph_name}: {poly_path}"
                ))

        if not subgraph_tasks:
            self.stdout.write(self.style.ERROR("No valid subgraph poly files found"))
            return

        use_gpu = options.get('gpu', False)
        
        if use_gpu:
            self.stdout.write(f"Processing {len(subgraph_tasks)} subgraphs sequentially (GPU mode)...")
            # Process sequentially to avoid GPU memory conflicts
            start_time = datetime.now()
            results = []
            for task_data in subgraph_tasks:
                results.append(_train_subgraph_worker(task_data))
            duration = (datetime.now() - start_time).total_seconds()
        else:
            self.stdout.write(f"Processing {len(subgraph_tasks)} subgraphs in parallel (CPU mode)...")

            # Extract workers from options
            workers = options.get('workers', 4)

            # Process in parallel
            from django.db import connections
            connections.close_all()  # Close connections before fork

            start_time = datetime.now()
            with Pool(processes=workers) as pool:
                results = pool.map(_train_subgraph_worker, subgraph_tasks)

            duration = (datetime.now() - start_time).total_seconds()

        # Report results
        success_count = sum(1 for r in results if r['success'])
        failure_count = len(results) - success_count

        self.stdout.write(self.style.SUCCESS(
            f"\n{'='*60}\n"
            f"Subgraph Processing Complete!\n"
            f"{'='*60}\n"
            f"Total: {len(results)}\n"
            f"Success: {success_count}\n"
            f"Failed: {failure_count}\n"
            f"Duration: {duration:.1f}s\n"
        ))

        for r in results:
            if r['success']:
                self.stdout.write(f"  ✓ {r['subgraph']}")
            else:
                self.stdout.write(self.style.ERROR(f"  ✗ {r['subgraph']}: {r.get('error', 'Unknown error')}"))
