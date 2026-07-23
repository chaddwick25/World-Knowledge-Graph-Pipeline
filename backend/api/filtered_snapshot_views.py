"""
API views for Filtered Snapshot Generation

Handles generation of filtered daily snapshots from monthly snapshots
for trend analysis workflows.
"""
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.db import transaction
from django.utils import timezone
from datetime import datetime
from api.models import ProcessingSession, TemporalSnapshot, PbfFile
import logging

# NOTE: FilteredSnapshotService imported lazily inside GenerateFilteredSnapshotsView.post

logger = logging.getLogger(__name__)


class GenerateFilteredSnapshotsView(APIView):
    """
    Generate filtered daily snapshots for trend analysis.
    
    POST /api/trend-analysis/<session_id>/generate-filtered-snapshots/
    
    Request body: (optional overrides)
    {
        "output_directory": "data/filtered_daily_snapshots",
        "generate_assets": true
    }
    """
    
    def post(self, request, session_id):
        try:
            # Get the trend analysis session
            session = ProcessingSession.objects.get(
                id=session_id,
                session_type='TREND_ANALYSIS'
            )
            
            config = session.configuration
            
            # Parse and make dates timezone-aware
            start_date = config['start_date']
            end_date = config['end_date']
            
            # Convert to datetime if string
            if isinstance(start_date, str):
                start_dt = datetime.fromisoformat(start_date.replace('Z', '+00:00'))
            else:
                start_dt = start_date
            
            if isinstance(end_date, str):
                end_dt = datetime.fromisoformat(end_date.replace('Z', '+00:00'))
            else:
                end_dt = end_date
            
            # Ensure timezone-aware
            if timezone.is_naive(start_dt):
                start_dt = timezone.make_aware(start_dt)
            if timezone.is_naive(end_dt):
                end_dt = timezone.make_aware(end_dt)
            
            # Get the source PbfFile (region extract)
            source_pbf = PbfFile.objects.get(id=config['region_id'])
            
            # Get monthly PbfFile extracts (not TemporalSnapshots)
            # Monthly extracts are children of yearly extracts, which are children of the region
            monthly_extracts = PbfFile.objects.filter(
                extraction_level='REGION_MONTHLY',
                parent_pbf__parent_pbf=source_pbf,
                min_timestamp__gte=start_dt,
                max_timestamp__lte=end_dt
            ).order_by('min_timestamp')
            
            if not monthly_extracts.exists():
                return Response({
                    'error': 'No monthly extracts found in the specified date range'
                }, status=status.HTTP_404_NOT_FOUND)
            
            # Prepare configuration for filtered snapshot service
            service_config = {
                'monthly_pbf_ids': [str(f.id) for f in monthly_extracts],
                'target_tags': config['target_tags'],
                'session_id': str(session_id),  # Use session ID as folder name
                'region_name': config['region_name'],
                'output_directory': request.data.get(
                    'output_directory',
                    'data/filtered_daily_snapshots'
                ),
                'generate_assets': request.data.get('generate_assets', True)
            }
            
            # Update session status
            with transaction.atomic():
                session.status = 'COLLECTING'
                session.results = session.results or {}
                session.results['filtered_snapshot_generation'] = {
                    'status': 'in_progress',
                    'monthly_extracts_count': monthly_extracts.count()
                }
                session.save()
            
            # Generate filtered daily snapshots
            logger.info(f"Starting filtered snapshot generation for session {session_id}")
            from extraction.services.filtered_snapshot_service import FilteredSnapshotService
            service = FilteredSnapshotService()
            result = service.generate_filtered_daily_snapshots(service_config)
            
            if result['success']:
                # Update session with results
                with transaction.atomic():
                    session.status = 'ANALYZING'
                    session.results['filtered_snapshot_generation'] = {
                        'status': 'completed',
                        'daily_snapshots_created': result['daily_snapshots_created'],
                        'total_size_mb': result['total_size_mb'],
                        'original_size_mb': result['original_size_mb'],
                        'size_reduction_percent': result['size_reduction_percent'],
                        'snapshots': result['snapshots']
                    }
                    session.save()
                
                logger.info(f"Filtered snapshot generation completed for session {session_id}")
                
                return Response({
                    'success': True,
                    'session_id': str(session.id),
                    'daily_snapshots_created': result['daily_snapshots_created'],
                    'total_size_mb': result['total_size_mb'],
                    'size_reduction_percent': result['size_reduction_percent'],
                    'message': f"Generated {result['daily_snapshots_created']} filtered daily snapshots",
                    'next_step': 'Ready for SVD analysis'
                }, status=status.HTTP_200_OK)
            else:
                # Update session with error
                with transaction.atomic():
                    session.status = 'FAILED'
                    session.results['filtered_snapshot_generation'] = {
                        'status': 'failed',
                        'error': result.get('error')
                    }
                    session.save()
                
                return Response({
                    'success': False,
                    'error': result.get('error', 'Unknown error')
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
            
        except ProcessingSession.DoesNotExist:
            return Response({
                'error': f'Trend analysis session {session_id} not found'
            }, status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            logger.error(f'Error generating filtered snapshots: {e}', exc_info=True)
            return Response({
                'error': f'Failed to generate filtered snapshots: {str(e)}'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
