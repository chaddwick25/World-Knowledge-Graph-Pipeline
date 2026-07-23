"""
Country Search Update API Views

Handles the full pipeline for updating search embeddings:
1. Create/find region extract
2. Generate yearly extracts
3. Generate monthly extracts
4. Generate graph assets
"""

import logging
import json
from pathlib import Path
from typing import Optional
import threading

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import AllowAny

from django.conf import settings
from extraction.models import RegionHierarchy, OSMWikiDataHierarchy
from api.models import (
    PbfFile,
    ProcessingSession,
    CountrySearchProcessing,
    PolygonFile,
    Task
)

# NOTE: Heavy service imports (osm_wikidata_resolver, regional_path_service,
# extraction_service, temporal_orchestrator_service, graph_asset_service)
# are imported lazily inside methods that use them to avoid slow startup.

import glob
from pathlib import Path
from django.conf import settings
import uuid

logger = logging.getLogger(__name__)


def _resolve_iso_from_country_name(country_name: str) -> str:
    """
    Resolve ISO code from country name using OSMWikiDataHierarchy.

    Args:
        country_name: Country name (e.g., "Ireland")

    Returns:
        ISO code (e.g., "IE") or None if not found
    """
    from extraction.services.osm_wikidata_resolver import get_country_relations_dict
    from extraction.services.regional_path_service import normalize_country_slug
    relations = get_country_relations_dict()
    if not relations:
        return None

    search_term = normalize_country_slug(country_name)

    for key, data in relations.items():
        slug = normalize_country_slug(data.get('slug', ''))
        name = normalize_country_slug(data.get('name', ''))

        if slug == search_term or name == search_term or search_term in slug or slug in search_term:
            # Prefer explicit iso_code field (ISO2/ISO3) when available; fall back to the dict key.
            iso_code = (data.get('iso_code') or key or '').upper()
            return iso_code or None

    return None


class CountrySearchUpdateView(APIView):
    """
    Trigger full search update pipeline for a country.
    
    Pipeline: Region Extract → Yearly → Monthly → Graph Assets
    """
    permission_classes = [AllowAny]
    
    def post(self, request):
        """
        POST /api/country-search-update/
        
        Payload:
        {
            "country_name": "Grenada",
            "region_id": "uuid-of-region-hierarchy-node"  # Optional
        }
        """
        country_name = request.data.get('country_name')
        region_id = request.data.get('region_id')
        
        if not country_name:
            return Response({
                'error': 'country_name is required'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            # Check if already processed
            country_processing, created = CountrySearchProcessing.objects.get_or_create(
                country_name=country_name
            )
            
            if country_processing.is_processed:
                return Response({
                    'error': f'{country_name} has already been processed',
                    'processing_completed_at': country_processing.processing_completed_at,
                    'yearly_extracts_count': country_processing.yearly_extracts_count,
                    'monthly_extracts_count': country_processing.monthly_extracts_count,
                    'asset_bundles_count': country_processing.asset_bundles_count
                }, status=status.HTTP_400_BAD_REQUEST)
            
            # Create processing session
            session = ProcessingSession.objects.create(
                session_name=f"Search Update: {country_name}",
                session_type='SEARCH_UPDATE',
                status='IN_PROGRESS',
                configuration={
                    'country_name': country_name,
                    'region_id': region_id,
                    'temporal_start': '2021-09-01',
                    'temporal_end': '2025-01-31'
                }
            )
            
            country_processing.processing_session = session
            country_processing.processing_started_at = timezone.now()
            country_processing.save()
            
            # Execute pipeline
            result = self._execute_pipeline(country_name, region_id, session, country_processing)
            
            return Response(result, status=status.HTTP_200_OK)
            
        except Exception as e:
            logger.error(f"Country search update failed: {e}", exc_info=True)
            return Response({
                'error': str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    
    def _execute_pipeline(self, country_name, region_id, session, country_processing):
        """Execute the full pipeline using existing API patterns"""
        from extraction.services.graph_asset_service import GraphAssetService
        graph_service = GraphAssetService()
        
        result = {
            'status': 'processing',
            'country_name': country_name,
            'processing_session_id': str(session.id),
            'region_extract_created': False,
            'yearly_extracts_count': 0,
            'monthly_extracts_count': 0,
            'asset_bundles_count': 0,
            'steps_completed': []
        }
        
        try:
            # Step 1: Find or create region extract
            region_pbf = self._get_or_create_region_extract(country_name, region_id)
            
            if not region_pbf:
                raise ValueError(f"Could not find or create region extract for {country_name}")
            
            country_processing.region_pbf = region_pbf
            country_processing.save()
            result['region_extract_created'] = True
            result['steps_completed'].append('region_extract_ready')
            
            # Step 2: Generate yearly extracts (2021-2024)
            # Use TemporalExtractService directly (same as Home page pattern)
            logger.info(f"Generating yearly extracts for {country_name}")
            temporal_service = TemporalExtractService()
            
            yearly_result = temporal_service.generate_temporal_extracts({
                'source_pbf_id': str(region_pbf.id),
                'granularity': 'yearly',
                'start_year': 2021,
                'end_year': 2024
            })
            
            yearly_extract_ids = yearly_result.get('extract_ids', [])
            result['yearly_extracts_count'] = len(yearly_extract_ids)
            result['steps_completed'].append('yearly_extracts_generated')
            
            # Step 3: Generate monthly extracts from yearly extracts
            # Following Home page pattern: generate monthly from each yearly
            logger.info(f"Generating monthly extracts for {country_name}")
            all_monthly_ids = []
            
            for yearly_id in yearly_extract_ids:
                monthly_result = temporal_service.generate_temporal_extracts({
                    'source_pbf_id': yearly_id,
                    'granularity': 'monthly',
                    'start_year': 2021,
                    'end_year': 2025
                })
                all_monthly_ids.extend(monthly_result.get('extract_ids', []))
            
            # Filter to Sep 2021 - Jan 2025 range
            monthly_pbfs = PbfFile.objects.filter(id__in=all_monthly_ids)
            filtered_monthly_ids = []
            
            for pbf in monthly_pbfs:
                if pbf.min_timestamp:
                    pbf_date = pbf.min_timestamp.date()
                    if date(2021, 9, 1) <= pbf_date <= date(2025, 1, 31):
                        filtered_monthly_ids.append(str(pbf.id))
            
            result['monthly_extracts_count'] = len(filtered_monthly_ids)
            result['steps_completed'].append('monthly_extracts_generated')
            
            # Step 4: Generate graph assets from monthly extracts (parallel)
            logger.info(f"Generating graph assets using multiprocessing")
            logger.info(f"Monthly extracts to process: {len(filtered_monthly_ids)}")
            
            from extraction.services.graph_asset_parallel_service import GraphAssetParallelService
            
            asset_service = GraphAssetParallelService()
            asset_result = asset_service.batch_generate_parallel(
                monthly_pbf_ids=filtered_monthly_ids,
                country_name=country_name,
                max_workers=20
            )
            
            if asset_result['success']:
                logger.info(f"Graph assets generated successfully")
                logger.info(f"  New: {asset_result['new']}, Skipped: {asset_result['skipped']}")
                result['asset_bundles_count'] = asset_result['successful']
            else:
                logger.error(f"Graph asset generation failed: {asset_result.get('error')}")
                result['asset_bundles_count'] = 0
            
            result['steps_completed'].append('graph_assets_generated')
            
            # Mark as completed
            country_processing.is_processed = True
            country_processing.processing_completed_at = timezone.now()
            country_processing.yearly_extracts_count = result['yearly_extracts_count']
            country_processing.monthly_extracts_count = result['monthly_extracts_count']
            country_processing.asset_bundles_count = result['asset_bundles_count']
            country_processing.temporal_start = date(2021, 9, 1)
            country_processing.temporal_end = date(2025, 1, 31)
            country_processing.save()
            
            session.status = 'COMPLETED'
            session.completed_at = timezone.now()
            session.results = result
            session.save()
            
            result['status'] = 'completed'
            
            return result
            
        except Exception as e:
            logger.error(f"Pipeline execution failed: {e}", exc_info=True)
            session.status = 'FAILED'
            session.results = {'error': str(e)}
            session.save()
            raise
    
    def _get_or_create_region_extract(self, country_name, region_id=None):
        """Find existing region extract or create it automatically"""
        # Try to find existing region PBF by country name
        region_pbf = PbfFile.objects.filter(
            path__icontains=country_name.lower(),
            extraction_level='REGION'
        ).first()
        
        if region_pbf:
            logger.info(f"Found existing region extract: {region_pbf.path}")
            return region_pbf
        
        # If region_id provided, try to find via RegionHierarchy
        if region_id:
            try:
                region = RegionHierarchy.objects.get(id=region_id)
                if hasattr(region, 'corresponding_pbf') and region.corresponding_pbf:
                    return region.corresponding_pbf
            except RegionHierarchy.DoesNotExist:
                pass
        
        # Region extract doesn't exist - create it automatically
        logger.info(f"No region extract found for {country_name}. Creating automatically...")
        return self._create_region_extract(country_name)
    
    def _create_region_extract(self, country_name):
        """
        Create region extract from continent using Task-based extraction.
        Follows the same pattern as Home page CreatePbfExtractTaskView.
        """
        try:
            # Find polygon file for this country
            polygon_file = self._find_polygon_file(country_name)
            
            if not polygon_file:
                raise ValueError(f"No polygon file found for {country_name}")
            
            # Find appropriate continent PBF to extract from
            continent_pbf = self._find_continent_for_country(country_name)
            
            if not continent_pbf:
                raise ValueError(f"No continent PBF found for {country_name}")
            
            logger.info(f"Creating region extract for {country_name}")
            logger.info(f"  Source: {continent_pbf.path}")
            logger.info(f"  Polygon: {polygon_file}")
            
            # Generate hierarchical output path based on continent PBF location
            from extraction.services.regional_path_service import normalize_country_slug
            region_name_clean = normalize_country_slug(country_name)
            continent_dir = Path(continent_pbf.path).parent
            region_dir = continent_dir / region_name_clean
            region_dir.mkdir(parents=True, exist_ok=True)
            output_path = str(region_dir / f"{region_name_clean}.pbf")
            
            # Create Task (same as Home page CreatePbfExtractTaskView)
            task = Task.objects.create(
                task_type=Task.TaskType.EXTRACT_PBF,
                parameters={
                    "source_pbf_id": str(continent_pbf.id),
                    "poly_file_path": polygon_file,
                    "output_pbf_path": output_path,
                    "cpu_core_id": 1  # Use E-core 1
                }
            )
            
            # Run extraction synchronously using run_pbf_extraction
            from extraction.services.extraction_service import run_pbf_extraction
            task_id = str(task.id)
            run_pbf_extraction(
                source_pbf_id=str(continent_pbf.id),
                poly_file_path=polygon_file,
                output_pbf_path=output_path,
                cpu_core_id=1,
                task_id=task_id
            )
            
            # Refresh task to get the result
            task.refresh_from_db()
            
            # Find the created PBF file - try multiple approaches
            region_pbf = None
            
            # Approach 1: Check task result for pbf_id
            if task.result and 'pbf_id' in task.result:
                try:
                    region_pbf = PbfFile.objects.get(id=task.result['pbf_id'])
                    logger.info(f"Found PBF from task result: {region_pbf.id}")
                except PbfFile.DoesNotExist:
                    pass
            
            # Approach 2: Query by path (most recent)
            if not region_pbf:
                region_pbf = PbfFile.objects.filter(path=output_path).order_by('-created_at').first()
                if region_pbf:
                    logger.info(f"Found PBF by path: {region_pbf.id}")
            
            # Approach 3: Query by RegionHierarchy link
            if not region_pbf:
                try:
                    region_hierarchy = RegionHierarchy.objects.filter(
                        name__iexact=country_name
                    ).first()
                    if region_hierarchy and hasattr(region_hierarchy, 'corresponding_pbf'):
                        region_pbf = region_hierarchy.corresponding_pbf
                        logger.info(f"Found PBF via RegionHierarchy: {region_pbf.id}")
                except:
                    pass
            
            if region_pbf:
                logger.info(f"Region extract created successfully: {region_pbf.path}")
                return region_pbf
            else:
                raise ValueError(f"Region PBF not found after extraction. Task status: {task.status}")
                
        except Exception as e:
            logger.error(f"Failed to create region extract for {country_name}: {e}", exc_info=True)
            raise
    
    def _find_polygon_file(self, country_name):
        """Find polygon file for country"""
        # Search in polygon files directory
        polygon_base = getattr(settings, 'POLYGON_FILES_DIR', 'data/osm_polygon_files')
        
        # Try different patterns
        from extraction.services.regional_path_service import normalize_country_slug
        patterns = [
            f"{polygon_base}/**/{country_name.lower()}.poly",
            f"{polygon_base}/**/{normalize_country_slug(country_name)}.poly",
            f"{polygon_base}/**/{country_name.lower().replace(' ', '-')}.poly",
        ]
        
        for pattern in patterns:
            matches = glob.glob(pattern, recursive=True)
            if matches:
                return matches[0]
        
        # Try PolygonFile model
        try:
            poly_file = PolygonFile.objects.filter(
                path__icontains=country_name.lower()
            ).first()
            if poly_file:
                return poly_file.path
        except:
            pass
        
        return None
    
    def _find_continent_for_country(self, country_name):
        """Find the appropriate continent PBF for a country by searching the hierarchy"""
        try:
            # First find the country in RegionHierarchy
            country_region = RegionHierarchy.objects.filter(name__iexact=country_name).first()
            if not country_region:
                # Fallback to direct PbfFile search if hierarchy doesn't exist yet
                pbf = PbfFile.objects.filter(path__icontains=country_name).first()
                if pbf and hasattr(pbf, 'parent_pbf'):
                    current_pbf = pbf.parent_pbf
                    while current_pbf:
                        # Flexible check: CONTINENT type OR path contains continent name
                        if current_pbf.extraction_level == 'CONTINENT' or current_pbf.pbf_file_type == 'CONTINENT':
                            return current_pbf
                        
                        # Fallback: if it's the root of the hierarchy and registered as a REGION, it's likely a mislabeled continent
                        if current_pbf.pbf_file_type == 'REGION' and (pbf_file_path.parent.name == 'osm_wikidata_extractions'):
                             logger.info(f"Found root-level PBF for {continent_name}, treating as CONTINENT source.")
                             return current_pbf
                        current_pbf = current_pbf.parent_pbf
                return None
                
            # Climb the hierarchy tree to find a node with a corresponding_pbf
            # that is an actual CONTINENT level extraction
            current_region = country_region.parent
            while current_region:
                if current_region.corresponding_pbf:
                    pbf = current_region.corresponding_pbf
                    if pbf.extraction_level == 'CONTINENT' or pbf.pbf_file_type == 'CONTINENT':
                        return pbf
                current_region = current_region.parent
                
        except Exception as e:
            logger.error(f"Error finding continent for {country_name}: {e}")
            
        return None


class CountrySearchStatusView(APIView):
    """
    Check processing status for a country.
    Now performs a strict check of the temporal hierarchy (History + Snapshots + Polys).
    """
    permission_classes = [AllowAny]
    
    def get(self, request, country_name):
        """
        GET /api/country-search-status/{country_name}/
        """
        from extraction.services.regional_path_service import regional_path_service, normalize_country_slug
        from extraction.services.country_override_service import get_country_slug
        from api.models import RegionHierarchy
        import calendar
        
        try:
            # TODO: rethink this (overrides)
            # Simplified: just try direct DB lookup with the provided name
            country_processing = CountrySearchProcessing.objects.filter(
                country_name__iexact=country_name
            ).first()

            if country_processing:
                is_processed = country_processing.is_processed
                started_at = country_processing.processing_started_at
                completed_at = country_processing.processing_completed_at
            else:
                is_processed = False
                started_at = None
                completed_at = None

            return Response({
                'country_name': country_name,
                'is_processed': is_processed,
                'is_db_processed': is_processed,
                'history_count': 0,
                'monthly_extracts_count': 0,
                'poly_count': 0,
                'has_pickle': False,
                'has_db_embeddings': False,
                'db_entity_count': 0,
                'processing_started_at': started_at,
                'processing_completed_at': completed_at
            })
            
        except Exception as e:
            logger.error(f"Error checking country search status: {e}", exc_info=True)
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class CountryPreProcessView(APIView):
    """
    Pre-process a country: generate temporal extracts, graph assets, and Parquet files.
    
    POST /api/country-preprocess/
    {
        "country_name": "Andorra",
        "year_filter": 2025  // Optional: only process this year for testing
    }
    """
    
    def post(self, request):
        country_name = request.data.get('country_name')
        year_filter = request.data.get('year_filter')  # Optional year filter for testing
        force = request.data.get('force', False)  # Force reprocessing

        if not country_name:
            return Response(
                {'error': 'country_name is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            # Check if country has already been processed
            from orchestration.models import CountrySearchProcessing
            existing = CountrySearchProcessing.objects.filter(country_name__iexact=country_name).first()
            if existing and existing.is_processed and not force:
                logger.info(f"[HEARTBEAT-V2] Country {country_name} already processed, skipping")
                return Response({
                    'status': 'already_processed',
                    'country_name': country_name,
                    'message': f'{country_name} has already been preprocessed. Use force=true to reprocess.',
                    'processed_at': existing.processing_completed_at
                }, status=status.HTTP_200_OK)

            logger.info(f"[HEARTBEAT-V2] Starting parallel temporal pre-processing for {country_name}")
            print(f"DEBUG: Starting parallel temporal pre-processing for {country_name}")
            
            # Find continent for the country
            region = RegionHierarchy.objects.filter(name__iexact=country_name).first()
            if not region or not region.parent:
                return Response({'error': f'Continent not found for {country_name} in RegionHierarchy'}, status=status.HTTP_400_BAD_REQUEST)
            
            continent = region.parent.name

            # Create a ProcessingSession for websocket tracking
            session = ProcessingSession.objects.create(
                session_name=f"Country Preprocess: {country_name}",
                session_type=ProcessingSession.SessionType.TEMPORAL_CORPUS,
                configuration={
                    'country': country_name,
                    'continent': continent,
                    'single_snapshot_mode': True
                }
            )

            # Return immediately with session_id so frontend can connect to websocket
            response_data = {
                'status': 'started',
                'country_name': country_name,
                'session_id': str(session.id),
                'message': 'Preprocessing started in background'
            }

            # Run preprocessing in background thread
            def run_preprocessing_background():
                try:
                    from extraction.services.temporal_orchestrator_service import TemporalOrchestratorService
                    from channels.layers import get_channel_layer
                    from asgiref.sync import async_to_sync
                    channel_layer = get_channel_layer()
                    group_name = f'pipeline_{session.id}'

                    def push_update(name, status, message, pct=0):
                        try:
                            async_to_sync(channel_layer.group_send)(
                                group_name,
                                {'type': 'step_update', 'step': 0, 'total': 4,
                                 'name': name, 'status': status, 'message': message, 'pct': pct}
                            )
                        except Exception:
                            pass

                    def push_complete(status, error=''):
                        try:
                            async_to_sync(channel_layer.group_send)(
                                group_name,
                                {'type': 'pipeline_complete', 'session_id': str(session.id),
                                 'status': status, 'error': error}
                            )
                        except Exception:
                            pass

                    # Instantiate orchestrator with session_id for websocket updates
                    orchestrator = TemporalOrchestratorService(session_id=str(session.id))

                    # Trigger parallel extraction with single snapshot mode
                    results = orchestrator.run_pipeline(
                        continent=continent,
                        country=country_name,
                        source_pbf_path=settings.PLANET_OSM_FILE_PATH,
                        start_year=2021,
                        end_year=2025,
                        phases=[1, 2, 3, 4],  # Setup + Preprocessing + Polys + GeoVectors pickle
                        single_snapshot_mode=True  # Force single snapshot mode
                    )

                    if not results.get('success'):
                        logger.error(f"Temporal pipeline failed for {country_name}: {results.get('error')}")
                        push_complete('failed', results.get('error', 'Temporal pipeline failed'))
                        return

                    # After temporal snapshot succeeds, run subgraph generation via Celery
                    iso = _resolve_iso_from_country_name(country_name)
                    if not iso:
                        logger.error(f"Failed to resolve ISO for country {country_name}")
                        push_complete('failed', 'Failed to resolve ISO')
                        return

                    logger.info(f"Subgraph generation handled by pipeline Phase 3 for {iso}")
                    push_update('subgraph_generation', 'completed', 'Subgraph generation complete', 100)

                    # Enqueue step 5 (GeoVectors Encode) for this country and session.
                    from orchestration.models import Task as OrchestratorTask
                    OrchestratorTask.objects.create(
                        task_type=OrchestratorTask.TaskType.GEOVECTORS_ENCODE,
                        parameters={
                            'country_name': country_name,
                            'iso': iso,
                            'force': True,
                            'session_id': str(session.id),
                        },
                        processing_session=session,
                    )
                    logger.info(f"Enqueued GEOVECTORS_ENCODE task for {country_name} (session={session.id})")

                    push_complete('completed')

                except Exception as e:
                    logger.error(f"Background preprocessing failed for {country_name}: {e}", exc_info=True)
                    push_complete('failed', str(e))


            # Start background thread
            thread = threading.Thread(target=run_preprocessing_background, daemon=True)
            thread.start()

            return Response(response_data, status=status.HTTP_202_ACCEPTED)
        
        except Exception as e:
            logger.error(f"Country pre-processing failed: {e}", exc_info=True)
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    def _delete_yearly_extractions(self, country_name: str):
        """Delete yearly PBF extractions to save disk space after monthly Parquet files are generated."""
        try:
            # Find and delete yearly PBF files for this country
            yearly_pbfs = PbfFile.objects.filter(
                path__icontains=country_name.lower(),
                extraction_level='REGION_YEARLY'
            )
            
            deleted_count = 0
            freed_mb = 0
            
            for pbf in yearly_pbfs:
                try:
                    # Get file size before deleting
                    if pbf.path and Path(pbf.path).exists():
                        size_mb = Path(pbf.path).stat().st_size / (1024 * 1024)
                        Path(pbf.path).unlink()  # Delete the file
                        freed_mb += size_mb
                    
                    pbf.delete()  # Delete the database record
                    deleted_count += 1
                    logger.info(f"Deleted yearly extraction: {pbf.path}")
                except Exception as e:
                    logger.warning(f"Failed to delete {pbf.path}: {e}")
            
            logger.info(f"Cleanup complete: Deleted {deleted_count} yearly extractions, freed {freed_mb:.1f} MB")
            
        except Exception as e:
            logger.warning(f"Error during yearly extraction cleanup: {e}")


class CountrySubgraphsView(APIView):
    """List subgraphs and their artifacts for a given country.

    The response is derived from the filesystem layout under
    OSM_WIKIDATA_EXTRACTIONS_DIR/{continent}/{country}/subgraphs.
    """

    permission_classes = [AllowAny]

    def get(self, request, country_name: str):
        if not country_name:
            return Response(
                {"error": "country_name is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        from extraction.services.osm_wikidata_resolver import get_country_relations_dict
        from extraction.services.regional_path_service import (
            normalize_country_slug,
            normalize_continent_slug,
            regional_path_service,
        )
        relations = get_country_relations_dict()
        if not relations:
            return Response(
                {"error": "country relations metadata not available"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        search_term = normalize_country_slug(country_name)
        entry = None
        for _, v in relations.items():
            slug = normalize_country_slug(v.get("slug", ""))
            name = normalize_country_slug(v.get("name", ""))
            if (
                slug == search_term
                or name == search_term
                or search_term in slug
                or slug in search_term
            ):
                entry = v
                break

        if not entry:
            return Response(
                {"error": f"Could not resolve country metadata for {country_name}"},
                status=status.HTTP_404_NOT_FOUND,
            )

        continent_raw = entry.get("continent_name") or entry.get("parent_slug") or ""
        continent = normalize_continent_slug(continent_raw) if continent_raw else "unknown"
        raw_slug = entry.get("slug") or country_name
        country_slug = normalize_country_slug(raw_slug)

        base_dir = getattr(settings, "OSM_WIKIDATA_EXTRACTIONS_DIR", None)
        if not base_dir:
            return Response(
                {"error": "OSM_WIKIDATA_EXTRACTIONS_DIR is not configured"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        # Ensure subgraphs directory exists (regional_path_service will create it if missing)
        subgraphs_root = regional_path_service.get_subgraphs_dir(continent, country_slug)

        if not subgraphs_root.exists():
            return Response(
                {
                    "country_name": country_name,
                    "continent": continent,
                    "country_slug": country_slug,
                    "subgraphs": [],
                },
                status=status.HTTP_200_OK,
            )

        subgraphs = []
        for sg_dir in sorted(p for p in subgraphs_root.iterdir() if p.is_dir()):
            slug = sg_dir.name
            name = slug.replace("_", " ")

            poly_files = sorted(sg_dir.glob("*.osm.poly"))
            pbf_files = sorted(sg_dir.glob("*.osm.pbf"))

            poly_path = str(poly_files[0]) if poly_files else None
            pbf_path = str(pbf_files[0]) if pbf_files else None

            # Subgraph pickle path follows the GeoVectors convention:
            # OSM_WIKIDATA_EXTRACTIONS_DIR/{continent}/{country}/pickles/{subgraph}/wdw.pickle
            from pathlib import Path

            base_extractions = Path(base_dir)
            pickle_dir = (
                base_extractions
                / continent.lower()
                / country_slug
                / "pickles"
                / slug.lower()
            )
            pickle_path_obj = pickle_dir / "wdw.pickle"
            pickle_path = str(pickle_path_obj) if pickle_path_obj.exists() else None

            subgraphs.append(
                {
                    "name": name,
                    "slug": slug,
                    "poly_path": poly_path,
                    "pbf_path": pbf_path,
                    "pickle_path": pickle_path,
                    "has_poly": bool(poly_path),
                    "has_pbf": bool(pbf_path),
                    "has_pickle": bool(pickle_path),
                }
            )

        return Response(
            {
                "country_name": country_name,
                "continent": continent,
                "country_slug": country_slug,
                "subgraphs": subgraphs,
            },
            status=status.HTTP_200_OK,
        )
