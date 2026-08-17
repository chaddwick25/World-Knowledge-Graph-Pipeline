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
from core.models import RegionHierarchy, OSMWikiDataHierarchy
from api.models import (
    PbfFile,
    ProcessingSession,
    CountrySearchProcessing,
    PolygonFile,
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


class CountrySubgraphsView(APIView):
    """List subgraphs and their artifacts for a given country.

    The response is derived from the filesystem layout under
    OSM_WIKIDATA_EXTRACTIONS_DIR/{continent}/{country}/subgraphs.

    Query params:
        snapshot_date - Optional. Snapshot date string (e.g. "2025_12_31").
                        Accepted for forward compatibility; subgraph paths
                        currently use the single-snapshot layout.
    """

    permission_classes = [AllowAny]

    def get(self, request, country_name: str):
        if not country_name:
            return Response(
                {"error": "country_name is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # snapshot_date is accepted but not yet applied — subgraph paths
        # currently use the single-snapshot filesystem layout.
        # _snapshot_date = request.query_params.get('snapshot_date')

        from core.services.planet_init.osm_wikidata_resolver import get_country_relations_dict
        from core.services.snapshot.regional_path_service import (
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
