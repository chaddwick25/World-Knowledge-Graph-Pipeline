"""
Tag Discovery API Views
Provides endpoints for discovering available tags in snapshots.
Uses the same filtered snapshot generation workflow as Trend Analysis.
"""

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.db import transaction
from django.utils import timezone
from django.conf import settings
from pathlib import Path
import pandas as pd
from collections import defaultdict
from datetime import datetime
import logging
import uuid

from api.models import ProcessingSession, PbfFile

# NOTE: parallel_snapshot_service imported lazily inside TagDiscoveryView.post

logger = logging.getLogger(__name__)


class TagDiscoveryView(APIView):
    """
    Discover available tags by generating filtered snapshots and analyzing their tags.
    
    POST /api/tag-discovery/analyze/
    
    Request:
    {
        "region_id": "uuid",
        "region_name": "canada",
        "start_date": "2024-01-01",
        "end_date": "2024-12-31",
        "tag_keys": ["building", "amenity", "highway"],  // OSM tag KEYS only
        "name": "urban_analysis_2025"  // Optional name for AssetBundles
    }
    
    Response:
    {
        "status": "success",
        "session_id": "uuid",
        "snapshots_processed": 12,
        "asset_bundles_created": 36,  // 12 snapshots × 3 tag keys
        "tag_keys": ["building", "amenity", "highway"],
        "name": "urban_analysis_2025",
        "snapshots": [...]
    }
    """
    
    def post(self, request):
        try:
            region_id = request.data.get('region_id')
            region_name = request.data.get('region_name')
            start_date = request.data.get('start_date')
            end_date = request.data.get('end_date')
            
            # Accept both 'tag_keys' (new) and 'target_tags' (old) for backward compatibility
            tag_keys = request.data.get('tag_keys') or request.data.get('target_tags', [])
            asset_name = request.data.get('name', None)
            
            # Validation
            if not region_id or not region_name:
                return Response(
                    {'error': 'region_id and region_name are required'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            if not start_date or not end_date:
                return Response(
                    {'error': 'start_date and end_date are required'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # Parse dates
            start_dt = datetime.fromisoformat(start_date.replace('Z', '+00:00'))
            end_dt = datetime.fromisoformat(end_date.replace('Z', '+00:00'))
            
            if timezone.is_naive(start_dt):
                start_dt = timezone.make_aware(start_dt)
            if timezone.is_naive(end_dt):
                end_dt = timezone.make_aware(end_dt)
            
            # Get the source PbfFile (region extract)
            try:
                source_pbf = PbfFile.objects.get(id=region_id)
            except PbfFile.DoesNotExist:
                return Response(
                    {'error': f'Region {region_name} not found'},
                    status=status.HTTP_404_NOT_FOUND
                )
            
            # Get monthly PbfFile extracts
            monthly_extracts = PbfFile.objects.filter(
                extraction_level='REGION_MONTHLY',
                parent_pbf__parent_pbf=source_pbf,
                min_timestamp__gte=start_dt,
                max_timestamp__lte=end_dt
            ).order_by('min_timestamp')
            
            if not monthly_extracts.exists():
                return Response({
                    'error': f'No monthly extracts found for {region_name} in the specified date range'
                }, status=status.HTTP_404_NOT_FOUND)
            
            # Create a ProcessingSession for tag discovery
            session = ProcessingSession.objects.create(
                session_type='TAG_DISCOVERY',
                status='COLLECTING',
                configuration={
                    'region_id': str(region_id),
                    'region_name': region_name,
                    'start_date': start_date,
                    'end_date': end_date,
                    'tag_keys': tag_keys,
                    'name': asset_name,
                    'monthly_extracts_count': monthly_extracts.count()
                }
            )
            
            logger.info(f"Created tag discovery session {session.id} for region {region_name}")
            
            tag_distribution = defaultdict(lambda: defaultdict(int))
            total_tags = 0
            snapshots_analyzed = 0
            snapshots_processed = 0
            asset_bundles_created = 0
            
            if not tag_keys or len(tag_keys) == 0:
                logger.info("No explicit tag keys specified. Pulling full tag distribution from base Parquet bundles.")
                try:
                    from api.models import AssetBundle
                    for extract in monthly_extracts:
                        # Locate the base Parquet bundle corresponding to this extract natively
                        base_bundle = AssetBundle.objects.filter(source_monthly_extract=extract, tag_key__isnull=True).first()
                        if not base_bundle or not base_bundle.bundle_path:
                            logger.warning(f"Base Parquet bundle missing for extract {extract.id}")
                            continue
                            
                        tags_path = Path(base_bundle.bundle_path) / 'tags.parquet'
                        if not tags_path.exists():
                            logger.warning(f"Base tags.parquet missing at {tags_path}")
                            continue
                            
                        # Read and map completely natively over Parquet table
                        df = pd.read_parquet(tags_path)
                        grouped = df.groupby(['key', 'value']).size().reset_index(name='count')
                        for _, row in grouped.iterrows():
                            tag_distribution[row['key']][row['value']] += int(row['count'])
                        
                        total_tags += len(df)
                        snapshots_analyzed += 1
                        snapshots_processed += 1
                except Exception as e:
                    logger.error(f"Failed to fetch base tag distribution: {e}")
                    raise e
            else:
                # Delegate to the ParallelSnapshotService for explicit Tag Filtering Extraction
                service_config = {
                    'monthly_pbf_ids': [str(f.id) for f in monthly_extracts],
                    'tag_keys': tag_keys,
                    'name': asset_name,
                    'session_id': str(session.id),
                    'region_name': region_name,
                    'output_directory': settings.ASSET_BUNDLES_DIR,
                    'generate_assets': True,
                    'parallel': True,
                    'max_workers': 20,
                    'cpu_cores': '8-27'
                }
                
                logger.info(f"Generating optimized filtered snapshots for tag discovery session {session.id}")

                from extraction.services.parallel_snapshot_service import ParallelSnapshotService
                service = ParallelSnapshotService()
                result = service.generate_optimized_daily_snapshots(service_config)
                
                if not result['success']:
                    with transaction.atomic():
                        session.status = 'FAILED'
                        session.results = {'error': result.get('error')}
                        session.save()
                    
                    return Response({
                        'error': result.get('error', 'Failed to generate filtered snapshots')
                    }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
                
                asset_bundles_created = result['asset_bundles_created']
                snapshots_processed = result['snapshots_processed']
                logger.info(f"Analyzing tags from {asset_bundles_created} AssetBundles across {snapshots_processed} snapshots")
                
                # Assemble tags directly from the newly created explicitly tagged subdirectories
                for snapshot_info in result.get('snapshots', []):
                    assets_dir = Path(snapshot_info.get('assets_directory'))
                    
                    if not assets_dir.exists():
                        continue
                    
                    for tag_key_dir in assets_dir.iterdir():
                        if not tag_key_dir.is_dir():
                            continue
                        
                        tags_path = tag_key_dir / 'tags.parquet'
                        if not tags_path.exists():
                            logger.warning(f"tags.parquet not found at {tags_path}")
                            continue
                        
                        df = pd.read_parquet(tags_path)
                        grouped = df.groupby(['key', 'value']).size().reset_index(name='count')
                        
                        for _, row in grouped.iterrows():
                            tag_distribution[row['key']][row['value']] += int(row['count'])
                        
                        total_tags += len(df)
                    
                    snapshots_analyzed += 1
            
            # Convert to response format
            result_distribution = {}
            for key, values_dict in tag_distribution.items():
                result_distribution[key] = [
                    {'value': value, 'count': count}
                    for value, count in values_dict.items()
                ]
                # Sort by count descending
                result_distribution[key] = sorted(
                    result_distribution[key],
                    key=lambda x: x['count'],
                    reverse=True
                )
            
            # Refresh DB connection after long parallel processing to prevent timeout
            from django.db import connection as db_connection
            db_connection.close()
            
            # Update session with results
            with transaction.atomic():
                session.status = 'COMPLETED'
                session.results = {
                    'tag_distribution': result_distribution,
                    'snapshots_analyzed': snapshots_analyzed,
                    'total_tags': total_tags,
                    'unique_keys': len(result_distribution),
                    'asset_bundle_generation': {
                        'snapshots_processed': snapshots_processed,
                        'asset_bundles_created': asset_bundles_created,
                        'tag_keys': tag_keys,
                        'name': asset_name
                    }
                }
                session.save()
            
            logger.info(f"Tag discovery completed for session {session.id}: {len(result_distribution)} tag categories, {total_tags} total tags, {asset_bundles_created} AssetBundles created")
            
            return Response({
                'status': 'success',
                'session_id': str(session.id),
                'tag_distribution': result_distribution,
                'snapshots_analyzed': snapshots_analyzed,
                'snapshots_processed': snapshots_processed,
                'asset_bundles_created': asset_bundles_created,
                'total_tags': total_tags,
                'unique_keys': len(result_distribution),
                'tag_keys': tag_keys,
                'name': asset_name
            })
            
        except Exception as e:
            logger.error(f"Tag discovery failed: {str(e)}", exc_info=True)
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
