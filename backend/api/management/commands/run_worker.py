import time
import traceback
import logging
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from api.models import Task, CountrySearchProcessing

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = 'Runs a worker to process pending tasks from the database.'

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS('Starting worker...'))
        while True:
            try:
                self.process_pending_task()
            except Exception as e:
                self.stdout.write(self.style.ERROR(f'Worker loop error: {e}'))
                traceback.print_exc()
            time.sleep(5) # Poll every 5 seconds

    def _get_wikidata_cache_path(self, country_name: str) -> str:
        """Return a stable cache path for Wikidata candidates for a country."""
        cache_dir = Path(settings.BASE_DIR).parent / 'data' / 'wikidata_cache'
        cache_dir.mkdir(parents=True, exist_ok=True)
        country_slug = (country_name or '').lower().strip().replace(' ', '_').replace('-', '_')
        return str(cache_dir / f'{country_slug}_candidates.json')

    def _push_ws(self, session_id, msg_type, **kwargs):
        """Push a message to the WebSocket channel if session_id is provided."""
        if not session_id:
            return
        try:
            from asgiref.sync import async_to_sync
            from channels.layers import get_channel_layer
            channel_layer = get_channel_layer()
            group_name = f'pipeline_{session_id}'
            async_to_sync(channel_layer.group_send)(
                group_name,
                {'type': msg_type, **kwargs}
            )
        except Exception as exc:
            self.stdout.write(self.style.WARNING(f'Channel push failed: {exc}'))

    def process_pending_task(self):
        task = None
        try:
            with transaction.atomic():
                # Lock the first available pending task
                task_to_process = Task.objects.select_for_update(skip_locked=True).filter(
                    status=Task.TaskStatus.PENDING
                ).order_by('created_at').first()

                if not task_to_process:
                    return # No pending tasks to process

                # Mark the task as in-progress
                task_to_process.status = Task.TaskStatus.IN_PROGRESS
                task_to_process.save()
                task = task_to_process

            # --- Transaction finished, lock released ---
            self.stdout.write(self.style.SUCCESS(f'Processing task {task.id} ({task.get_task_type_display()})'))

            # Execute the task logic based on its type
            if task.task_type == Task.TaskType.DOWNLOAD_PBF:
                pbf_file_id = task.parameters.get('pbf_file_id')
                # service = DownloaderService(pbf_file_id=pbf_file_id)
                # service.download_and_register()

            elif task.task_type == Task.TaskType.CREATE_SNAPSHOT:
                # service = SnapshotService(task_id=task.id)
                # service.create_snapshots()
                print("CREATE_SNAPSHOT")

            elif task.task_type == Task.TaskType.EXTRACT_PBF:
                from extraction.services.extraction_service import run_pbf_extraction
                params = task.parameters
                run_pbf_extraction(
                    source_pbf_id=params['source_pbf_id'],
                    poly_file_path=params['poly_file_path'],
                    output_pbf_path=params['output_pbf_path'],
                    cpu_core_id=params['cpu_core_id'],
                    task_id=task.id
                )

            elif task.task_type == Task.TaskType.GEOVECTORS_ENCODE:
                # Step 5: GeoVectors encoding
                params = task.parameters or {}
                country_name = params.get('country_name')
                force = params.get('force', False)
                session_id = params.get('session_id')
                iso = params.get('iso')

                if not country_name:
                    raise ValueError("GEOVECTORS_ENCODE task missing 'country_name' parameter")

                # Derive ISO code from country_name if not provided
                if not iso:
                    from extraction.services.osm_wikidata_resolver import get_country_by_name
                    country_data = get_country_by_name(country_name)
                    if country_data:
                        iso = country_data.get('iso2') or country_data.get('wikidata_id')
                    if not iso:
                        logger.warning(f"Could not resolve ISO code for country: {country_name}")

                # Emit websocket start event
                self._push_ws(session_id, 'step_update',
                              step=5, total=8,
                              name='geovectors_encode',
                              status='in_progress',
                              message=f'Starting GeoVectors encoding for {country_name}',
                              pct=0)

                # Use 2 workers to prevent memory exhaustion (each worker loads PBF + FastText + NLE models)
                from geovectors_encoder.services.geovectors_service import GeoVectorsEncoderService
                service = GeoVectorsEncoderService(n_jobs=2)
                service.run_for_country(country_name, force=force)

                # Mark search processing as completed now that GeoVectors encoding succeeded
                country_proc, _ = CountrySearchProcessing.objects.get_or_create(
                    country_name=country_name,
                    defaults={"is_processed": False},
                )
                country_proc.is_processed = True
                country_proc.processing_completed_at = timezone.now()
                country_proc.save()

                # Emit websocket done event
                self._push_ws(session_id, 'step_update',
                              step=5, total=8,
                              name='geovectors_encode',
                              status='completed',
                              message=f'GeoVectors encoding completed for {country_name}',
                              pct=100)

                # Auto-chain: enqueue Harvest + IGEA (step 7) on success
                next_params = {
                    'country_name': country_name,
                    'iso': iso,
                    'session_id': session_id,
                }
                Task.objects.create(
                    task_type=Task.TaskType.HARVEST_IGEA,
                    parameters=next_params,
                    processing_session=task.processing_session,
                )

            elif task.task_type == Task.TaskType.HARVEST_IGEA:
                # Step 7: Harvest Wikidata candidates + run IGEA
                params = task.parameters or {}
                country_name = params.get('country_name')
                iso = (params.get('iso') or '').upper()
                session_id = params.get('session_id')

                if not iso:
                    raise ValueError("HARVEST_IGEA task missing 'iso' parameter")

                cache_file = self._get_wikidata_cache_path(country_name or iso)

                # Step 7a: Harvest Wikidata candidates
                self._push_ws(session_id, 'step_update',
                              step=7, total=8,
                              name='harvest_wikidata',
                              status='in_progress',
                              message=f'Harvesting Wikidata candidates for {iso}',
                              pct=0)

                call_command(
                    'harvest_wikidata_candidates',
                    country=iso,
                    cache_file=cache_file,
                    limit=50000,
                    enrich_classes=True,
                    run_igea=False,
                )

                self._push_ws(session_id, 'step_update',
                              step=7, total=8,
                              name='harvest_wikidata',
                              status='completed',
                              message=f'Wikidata candidates harvested for {iso}',
                              pct=50)

                # Step 7b: Run IGEA alignment
                self._push_ws(session_id, 'step_update',
                              step=7, total=8,
                              name='run_igea',
                              status='in_progress',
                              message=f'Running IGEA alignment for {iso}',
                              pct=60)

                call_command(
                    'run_igea_alignment',
                    country=iso,
                    iterations=3,
                    threshold=0.7,
                    max_distance=2500.0,
                    candidate_limit=100000,
                    candidate_cache=cache_file,
                )

                self._push_ws(session_id, 'step_update',
                              step=7, total=8,
                              name='run_igea',
                              status='completed',
                              message=f'IGEA alignment completed for {iso}',
                              pct=100)

                # Auto-chain: enqueue Predict Links (step 8)
                next_params = {
                    'country_name': country_name,
                    'iso': iso,
                    'session_id': session_id,
                }
                Task.objects.create(
                    task_type=Task.TaskType.PREDICT_LINKS,
                    parameters=next_params,
                    processing_session=task.processing_session,
                )

            elif task.task_type == Task.TaskType.PREDICT_LINKS:
                # Step 8: Predict spatial links (USLP) - subgraph-level with country-level fallback
                params = task.parameters or {}
                country_name = params.get('country_name')
                iso = (params.get('iso') or '').upper()
                session_id = params.get('session_id')

                if not country_name:
                    raise ValueError("PREDICT_LINKS task missing 'country_name' parameter")

                # Build subgraph list using shared service (auto-all mode for pipeline)
                from extraction.services.subgraph_list_service import build_subgraph_list

                subgraphs = build_subgraph_list(
                    country_name=country_name,
                    auto_all=True,  # Pipeline always uses auto-all
                )

                # Use settings defaults for USLP parameters
                from django.conf import settings
                threshold = settings.USLP_THRESHOLD
                top_k = settings.USLP_TOP_K
                limit = settings.USLP_LIMIT
                max_heads = settings.USLP_MAX_HEADS
                use_gpu_setting = settings.USLP_USE_GPU
                gpu_device = settings.USLP_GPU_DEVICE
                use_fp64 = settings.USLP_USE_FP64

                # GPU handling: 'auto' means detect, 'true'/'false' means force
                if use_gpu_setting == 'auto':
                    try:
                        import torch
                        use_gpu = torch.cuda.is_available()
                        logger.info(f"GPU auto-detection for {country_name}: CUDA available = {use_gpu}")
                    except ImportError:
                        use_gpu = False
                        logger.warning("PyTorch not available, falling back to CPU")
                else:
                    use_gpu = bool(use_gpu_setting)

                # If no subgraphs found, fall back to country-level USLP
                if not subgraphs:
                    self.stdout.write(
                        self.style.WARNING(f'No subgraphs found for {country_name}, falling back to country-level USLP')
                    )
                    self._push_ws(session_id, 'step_update',
                                  step=8, total=8,
                                  name='predict_links',
                                  status='in_progress',
                                  message=f'No subgraphs found for {country_name}, running country-level USLP',
                                  pct=0)

                    # Use country-level USLP via SpatialLinkPredictionService
                    from igea.services.spatial_link_prediction import SpatialLinkPredictionService
                    from worldkg_nca.models import OsmEntity

                    # Initialize service (GPU or CPU)
                    if use_gpu:
                        try:
                            import torch
                            from igea.services.gpu_uslp_service import GPUAcceleratedUSLP
                            if not torch.cuda.is_available():
                                logger.warning("GPU requested but CUDA not available, falling back to CPU")
                                service = SpatialLinkPredictionService()
                            else:
                                logger.info(f"Country-level USLP: Using GPU device {gpu_device}")
                                service = GPUAcceleratedUSLP(device=gpu_device, use_fp64=use_fp64)
                        except Exception as exc:
                            logger.warning(f"Failed to initialize GPU USLP ({exc}), falling back to CPU")
                            service = SpatialLinkPredictionService()
                    else:
                        logger.info("Country-level USLP: Using CPU mode")
                        service = SpatialLinkPredictionService()

                    # Load candidate pool scoped to the target country (via tags/ISO)
                    logger.info(f"Country-level USLP: Loading candidate pool for {country_name} (limit={limit})")
                    pool_size = service.load_candidate_pool_from_db(
                        limit=limit,
                        country=iso or country_name,
                    )
                    logger.info(f"Country-level USLP: Candidate pool size = {pool_size}")

                    # Load head entities with spatial tags, filtered by country and, when
                    # possible, by the same bbox used for the candidate pool. This mirrors
                    # the subgraph USLP path, which always scopes heads by polygon.
                    from extraction.services import osm_wikidata_resolver

                    country_data = osm_wikidata_resolver.get_country_by_name(country_name)
                    # Try multiple ISO fields: iso2, wikidata_id
                    country_iso = None
                    if country_data:
                        country_iso = country_data.get('iso2') or country_data.get('wikidata_id')

                    # Build a small set of identifiers we consider a match for this country
                    match_tokens = set()
                    if country_name:
                        match_tokens.add(country_name.upper())
                    if country_iso:
                        match_tokens.add(str(country_iso).upper())

                    head_entities = []
                    base_qs = OsmEntity.objects.using('vectors').filter(geom__isnull=False)
                    qs = base_qs

                    # Apply a bbox geofence for head entities when we can resolve an ISO
                    # code via the shared OSM/Wikidata relations dict. This keeps the
                    # head-entity scan local to the target country instead of scanning the
                    # entire table.
                    iso_for_bbox = None
                    try:
                        relations = osm_wikidata_resolver.get_country_relations_dict()
                        lookup_key = iso or country_name
                        meta = None
                        if lookup_key:
                            lookup_upper = lookup_key.upper()
                            meta = relations.get(lookup_upper) or relations.get(lookup_key)
                        if meta:
                            iso_code = meta.get("iso_code")
                            if iso_code:
                                iso_for_bbox = str(iso_code).upper()
                    except Exception as exc:
                        logger.warning(
                            "Country-level USLP: get_country_relations_dict failed for %s: %s",
                            country_name,
                            exc,
                        )

                    bbox_for_heads = None
                    if iso_for_bbox:
                        try:
                            # Prefer the same CountryPipelineProfile-driven bbox logic used
                            # for the candidate pool. This keeps geometry consistent and
                            # leverages high-resolution .poly files when available.
                            from django.db import models
                            from orchestration.models import CountryPipelineProfile

                            profile = (
                                CountryPipelineProfile.objects.filter(
                                    models.Q(iso2__iexact=iso_for_bbox)
                                    | models.Q(iso3__iexact=iso_for_bbox)
                                ).first()
                            )
                            if profile:
                                bbox_for_heads = osm_wikidata_resolver.resolve_bbox_for_profile(profile)
                        except Exception as exc:
                            logger.warning(
                                "Country-level USLP: Failed to resolve bbox from CountryPipelineProfile for %s (iso=%s): %s",
                                country_name,
                                iso_for_bbox,
                                exc,
                            )

                        # As a final fallback, try legacy resolve_country_bbox
                        if bbox_for_heads is None:
                            try:
                                bbox_for_heads = osm_wikidata_resolver.resolve_country_bbox(iso_for_bbox)
                            except Exception as exc:
                                logger.warning(
                                    "Country-level USLP: resolve_country_bbox failed for %s (iso=%s): %s",
                                    country_name,
                                    iso_for_bbox,
                                    exc,
                                )

                        if bbox_for_heads:
                            try:
                                from django.contrib.gis.geos import GEOSGeometry

                                bbox_wkt = osm_wikidata_resolver.bbox_to_wkt(*bbox_for_heads)
                                poly_geom = GEOSGeometry(bbox_wkt, srid=4326)
                                qs = qs.filter(geom__within=poly_geom)
                                logger.info(
                                    "Country-level USLP: Applied bbox filter for head entities for %s (iso=%s, bbox=%s)",
                                    country_name,
                                    iso_for_bbox,
                                    bbox_for_heads,
                                )
                            except Exception as exc:
                                logger.warning(
                                    "Country-level USLP: Failed to apply bbox filter for head entities for %s (iso=%s): %s",
                                    country_name,
                                    iso_for_bbox,
                                    exc,
                                )

                    if bbox_for_heads:
                        # When we have a reliable bbox, mirror the subgraph USLP behavior:
                        # head entities are scoped purely by geometry + spatial tags.
                        from igea.tasks import _build_head_entities

                        head_entities = _build_head_entities(qs, max_heads=max_heads)
                    else:
                        # Legacy global, tag-based head selection (no bbox available).
                        for e in qs.iterator(chunk_size=10_000):
                            tags = e.tags or {}
                            # Only include entities with spatial literal tags
                            spatial_keys = {
                                'is_in', 'is_in:country', 'is_in:state', 'is_in:county',
                                'addr:country', 'addr:state', 'addr:county', 'addr:city',
                                'addr:suburb', 'addr:hamlet', 'addr:village', 'addr:town',
                            }
                            if not any(k in tags for k in spatial_keys):
                                continue

                            # Optionally restrict to entities that belong to the target country
                            if match_tokens:
                                tag_country = (tags.get('addr:country') or '').upper()
                                is_in_country = (tags.get('is_in:country') or '').upper()
                                is_in = (tags.get('is_in') or '').upper()
                                if not any(
                                    m == tag_country
                                    or m == is_in_country
                                    or (m and m in is_in)
                                    for m in match_tokens
                                ):
                                    continue

                            head_entities.append({
                                'osm_id': e.osm_id,
                                'lat': e.geom.y,
                                'lon': e.geom.x,
                                'tags': tags,
                            })
                            if len(head_entities) >= max_heads:
                                break

                        # If no entities found with strict country filtering, retry without it
                        if not head_entities and country_iso:
                            logger.warning(
                                "Country-level USLP: No entities found with country filter, "
                                "retrying without filter",
                            )
                            head_entities = []
                            for e in base_qs.iterator(chunk_size=10_000):
                                tags = e.tags or {}
                                spatial_keys = {
                                    'is_in', 'is_in:country', 'is_in:state', 'is_in:county',
                                    'addr:country', 'addr:state', 'addr:county', 'addr:city',
                                    'addr:suburb', 'addr:hamlet', 'addr:village', 'addr:town',
                                }
                                if any(k in tags for k in spatial_keys):
                                    head_entities.append({
                                        'osm_id': e.osm_id,
                                        'lat': e.geom.y,
                                        'lon': e.geom.x,
                                        'tags': tags,
                                    })
                                    if len(head_entities) >= max_heads:
                                        break

                    logger.info(f"Country-level USLP: {len(head_entities)} head entities with spatial tags")

                    # Retrieve snapshot_id from ProcessingSession
                    from orchestration.models import ProcessingSession
                    snapshot_id = None
                    if session_id:
                        session = ProcessingSession.objects.filter(id=session_id).first()
                        if session and session.configuration:
                            snapshot_id = session.configuration.get('snapshot_id')
                            if snapshot_id:
                                logger.info(f"Country-level USLP: Using snapshot_id={snapshot_id}")

                    # Predict links
                    links = service.predict_links_batch(head_entities, threshold=threshold, top_k=top_k)
                    logger.info(f"Country-level USLP: {len(links)} links accepted")

                    # Persist links with country_name
                    n_saved = service.persist_links(links, snapshot_id=snapshot_id, country_name=country_name, threshold=threshold)
                    logger.info(f"Country-level USLP: Persisted {n_saved} links")

                    self._push_ws(session_id, 'step_update',
                                  step=8, total=8,
                                  name='predict_links',
                                  status='completed',
                                  message=f'Country-level USLP completed for {country_name}: {n_saved} links',
                                  pct=100)
                    # Mark task as completed and exit early
                    task.status = Task.TaskStatus.COMPLETED
                    task.save()
                    return

                self._push_ws(session_id, 'step_update',
                              step=8, total=8,
                              name='predict_links',
                              status='in_progress',
                              message=f'Predicting spatial links for {len(subgraphs)} subgraphs in {country_name}',
                              pct=0)

                # Use the new subgraph-level USLP task
                from igea.tasks import run_uslp_for_subgraph_batch
                from django.conf import settings

                # Use settings defaults for USLP parameters
                threshold = settings.USLP_THRESHOLD
                top_k = settings.USLP_TOP_K
                limit = settings.USLP_LIMIT
                max_heads = settings.USLP_MAX_HEADS
                use_gpu_setting = settings.USLP_USE_GPU
                gpu_device = settings.USLP_GPU_DEVICE
                use_fp64 = settings.USLP_USE_FP64

                # GPU handling: 'auto' means detect, 'true'/'false' means force
                if use_gpu_setting == 'auto':
                    try:
                        import torch
                        use_gpu = torch.cuda.is_available()
                        logger.info(f"GPU auto-detection for {country_name}: CUDA available = {use_gpu}")
                    except ImportError:
                        use_gpu = False
                        logger.warning("PyTorch not available, falling back to CPU")
                else:
                    use_gpu = bool(use_gpu_setting)

                # Get snapshot_id from session configuration if available
                from orchestration.models import ProcessingSession
                snapshot_id = None
                session = ProcessingSession.objects.filter(id=session_id).first()
                if session and session.configuration:
                    snapshot_id = session.configuration.get('snapshot_id')

                try:
                    summary = run_uslp_for_subgraph_batch(
                        country_name=country_name,
                        subgraphs=subgraphs,
                        threshold=threshold,
                        top_k=top_k,
                        limit=limit,
                        max_heads=max_heads,
                        use_gpu=use_gpu,
                        gpu_device=gpu_device,
                        use_fp64=use_fp64,
                        snapshot_id=snapshot_id,
                        session_id=session_id,
                    )
                except AttributeError:
                    # Celery not available, run synchronously
                    summary = run_uslp_for_subgraph_batch(
                        country_name=country_name,
                        subgraphs=subgraphs,
                        threshold=threshold,
                        top_k=top_k,
                        limit=limit,
                        max_heads=max_heads,
                        use_gpu=use_gpu,
                        gpu_device=gpu_device,
                        use_fp64=use_fp64,
                        snapshot_id=snapshot_id,
                        session_id=session_id,
                    )

                if not summary.get('success'):
                    raise ValueError(f"USLP subgraph batch failed: {summary.get('error', 'Unknown error')}")

                self._push_ws(session_id, 'step_update',
                              step=8, total=8,
                              name='predict_links',
                              status='completed',
                              message=f'Spatial link prediction completed for {len(subgraphs)} subgraphs - {summary.get("total_links_saved", 0)} links saved',
                              pct=100)

            else:
                raise NotImplementedError(f'Task type {task.task_type} not implemented.')

            # If we get here, the task was successful
            task.status = Task.TaskStatus.COMPLETED
            task.save()
            self.stdout.write(self.style.SUCCESS(f'Task {task.id} completed successfully.'))

        except Exception as e:
            if task:
                self.stdout.write(self.style.ERROR(f'Task {task.id} failed: {e}'))
                traceback.print_exc()

                # Emit websocket failure events for pipeline-related tasks
                params = task.parameters or {}
                session_id = params.get('session_id')
                country_name = params.get('country_name', 'unknown')

                if task.task_type == Task.TaskType.GEOVECTORS_ENCODE:
                    self._push_ws(session_id, 'step_update',
                                  step=5, total=8,
                                  name='geovectors_encode',
                                  status='failed',
                                  message=f'GeoVectors encoding failed for {country_name}: {str(e)}',
                                  pct=0)
                elif task.task_type == Task.TaskType.HARVEST_IGEA:
                    # We don't know whether failure was in harvest or IGEA, so mark both
                    self._push_ws(session_id, 'step_update',
                                  step=7, total=8,
                                  name='harvest_wikidata',
                                  status='failed',
                                  message=f'Harvest/IGEA failed for {country_name}: {str(e)}',
                                  pct=0)
                    self._push_ws(session_id, 'step_update',
                                  step=7, total=8,
                                  name='run_igea',
                                  status='failed',
                                  message=f'Harvest/IGEA failed for {country_name}: {str(e)}',
                                  pct=0)
                elif task.task_type == Task.TaskType.PREDICT_LINKS:
                    self._push_ws(session_id, 'step_update',
                                  step=8, total=8,
                                  name='predict_links',
                                  status='failed',
                                  message=f'Spatial link prediction failed for {country_name}: {str(e)}',
                                  pct=0)

                task.status = Task.TaskStatus.FAILED
                task.result = {'error': str(e), 'traceback': traceback.format_exc()}
                task.save()
            else:
                # This case is rare, but could happen if the initial lock fails
                self.stdout.write(self.style.ERROR(f'Failed to acquire task lock: {e}'))
