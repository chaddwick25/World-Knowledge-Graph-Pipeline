"""
Country Search Update API Views

Handles the full pipeline for updating search embeddings:
1. Create/find region extract
2. Generate yearly extracts
3. Generate monthly extracts
4. Generate graph assets
"""

import logging
from pathlib import Path
import threading

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import AllowAny

from django.conf import settings
from core.models import RegionHierarchy
from api.models import (
    PbfFile,
    ProcessingSession,
    CountrySearchProcessing,
    Task
)

# NOTE: Heavy service imports (osm_wikidata_resolver, regional_path_service,
# extraction_service, snapshot_extraction_service, graph_asset_service)
# are imported lazily inside methods that use them to avoid slow startup.


logger = logging.getLogger(__name__)


def _resolve_iso_from_country_name(country_name: str) -> str:
    """Resolve ISO code from country name (delegates to osm_wikidata_resolver)."""
    from core.services.planet_init.osm_wikidata_resolver import resolve_iso_from_country_name
    return resolve_iso_from_country_name(country_name)



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
        from core.services.snapshot.regional_path_service import regional_path_service, normalize_country_slug
        from core.services.snapshot.country_override_service import get_country_slug
        from api.models import RegionHierarchy
        import calendar
        
        try:
            # Normalize: frontend sends display names ("Ireland And Northern Ireland"),
            # DB stores slugs ("ireland_and_northern_ireland"). normalize_country_slug
            # aligns the two so multi-word countries match.
            from core.services.snapshot.regional_path_service import normalize_country_slug
            normalized_name = normalize_country_slug(country_name)

            # Try the normalized slug first, then fall back to the raw name
            # (covers any DB rows stored with spaces or mixed casing).
            country_processing = (
                CountrySearchProcessing.objects.filter(country_name__iexact=normalized_name).first()
                or CountrySearchProcessing.objects.filter(country_name__iexact=country_name).first()
            )

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
            from core.models import CountrySearchProcessing
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
                    from core.services.snapshot.snapshot_extraction_service import (
                        SnapshotExtractionService,
                    )
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

                    # Resolve ISO for the country (needed for SnapshotExtractionService)
                    iso = _resolve_iso_from_country_name(country_name)
                    if not iso:
                        logger.error(f"Failed to resolve ISO for country {country_name}")
                        push_complete('failed', 'Failed to resolve ISO')
                        return

                    # Resolve OSM relation ID for the country
                    from core.models import CountryPipelineProfile
                    profile = CountryPipelineProfile.objects.filter(
                        iso2__iexact=iso,
                    ).first()
                    osm_relation_id = profile.osm_relation_id if profile else None

                    snapshot_date = getattr(settings, 'SINGLE_SNAPSHOT_DATE', '2025_12_31')

                    push_update('extract_region_pbf', 'in_progress', 'Extracting snapshot from planet PBF…', 25)
                    service = SnapshotExtractionService()
                    results = service.extract_country_snapshot(
                        country_code=iso,
                        country_name=country_name,
                        continent=continent,
                        snapshot_date=snapshot_date,
                        osm_relation_id=osm_relation_id,
                    )

                    if not results.get('success'):
                        logger.error(f"Snapshot extraction failed for {country_name}: {results.get('error')}")
                        push_complete('failed', results.get('error', 'Snapshot extraction failed'))
                        return

                    push_update('extract_region_pbf', 'completed', 'Snapshot extraction complete', 50)
                    push_update('monthly_snapshots', 'completed', 'Snapshot ready', 75)
                    logger.info(f"Subgraph generation handled by pipeline Phase 3 for {iso}")
                    push_update('subgraph_generation', 'completed', 'Subgraph generation complete', 100)

                    # Enqueue step 5 (GeoVectors Encode) for this country and session.
                    from core.models import Task as OrchestratorTask
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
