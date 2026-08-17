"""
Search Update Pipeline API Views

Handles artifact management and search update orchestration.
"""

import logging
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import AllowAny

from api.models import ProcessingSession, CountryArtifact

logger = logging.getLogger(__name__)


class SearchUpdateView(APIView):
    """
    Trigger full search update pipeline for a country.
    """
    permission_classes = [AllowAny]
    
    def post(self, request):
        """
        Trigger search update.
        
        Payload:
        {
            "country_code": "GRD",
            "country_name": "Grenada",
            "start_year": 2020,
            "end_year": 2024,
            "google_drive_backup": true
        }
        """
        required_fields = ['country_code', 'country_name', 'start_year', 'end_year']
        for field in required_fields:
            if field not in request.data:
                return Response({
                    'error': f'{field} is required'
                }, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            from core.services.pipeline.search_update_orchestrator import SearchUpdateOrchestrator
            
            orchestrator = SearchUpdateOrchestrator()
            result = orchestrator.execute_full_pipeline(request.data)
            
            return Response(result, status=status.HTTP_200_OK)
            
        except Exception as e:
            logger.error(f"Search update failed: {e}", exc_info=True)
            return Response({
                'error': str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class SearchUpdateStatusView(APIView):
    """
    Get status of a search update session.
    """
    permission_classes = [AllowAny]
    
    def get(self, request, session_id):
        try:
            from core.services.pipeline.search_update_orchestrator import SearchUpdateOrchestrator
            
            orchestrator = SearchUpdateOrchestrator()
            status_info = orchestrator.get_session_status(session_id)
            
            return Response(status_info, status=status.HTTP_200_OK)
            
        except ProcessingSession.DoesNotExist:
            return Response({
                'error': 'Session not found'
            }, status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            return Response({
                'error': str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class CountryArtifactsView(APIView):
    """
    Get all artifacts for a country.
    Used for downstream task selection.
    """
    permission_classes = [AllowAny]
    
    def get(self, request, country_code):
        """
        Get artifacts for a country.
        
        Query params:
        - artifact_type: Filter by type
        - status: Filter by status
        """
        artifacts = CountryArtifact.objects.filter(
            country_code=country_code
        )
        
        # Apply filters
        artifact_type = request.query_params.get('artifact_type')
        if artifact_type:
            artifacts = artifacts.filter(artifact_type=artifact_type)
        
        artifact_status = request.query_params.get('status')
        if artifact_status:
            artifacts = artifacts.filter(artifact_status=artifact_status)
        
        # Organize by type
        result = {
            'country_code': country_code,
            'artifacts': {
                'region_extract': None,
                'monthly_extracts': [],
                'asset_bundles': [],
                'fasttext_models': [],
                'embeddings': []
            },
            'summary': {
                'total_artifacts': artifacts.count(),
                'total_size_mb': 0,
                'google_drive_backup': False
            }
        }
        
        for artifact in artifacts:
            if artifact.artifact_type == 'REGION_EXTRACT':
                result['artifacts']['region_extract'] = {
                    'id': str(artifact.id),
                    'path': artifact.artifact_path,
                    'status': artifact.artifact_status,
                    'size_mb': artifact.file_size_mb
                }
            
            elif artifact.artifact_type == 'MONTHLY_EXTRACT':
                result['artifacts']['monthly_extracts'].append({
                    'id': str(artifact.id),
                    'path': artifact.artifact_path,
                    'temporal_range': f"{artifact.temporal_start} to {artifact.temporal_end}",
                    'status': artifact.artifact_status,
                    'size_mb': artifact.file_size_mb
                })
            
            elif artifact.artifact_type == 'ASSET_BUNDLE':
                result['artifacts']['asset_bundles'].append({
                    'id': str(artifact.id),
                    'path': artifact.artifact_path,
                    'temporal_range': f"{artifact.temporal_start} to {artifact.temporal_end}",
                    'metadata': artifact.metadata
                })
            
            elif artifact.artifact_type == 'FASTTEXT_MODEL':
                result['artifacts']['fasttext_models'].append({
                    'id': str(artifact.id),
                    'path': artifact.artifact_path,
                    'size_mb': artifact.file_size_mb,
                    'metadata': artifact.metadata
                })
            
            elif artifact.artifact_type == 'EMBEDDINGS':
                result['artifacts']['embeddings'].append({
                    'id': str(artifact.id),
                    'embedding_type': artifact.metadata.get('embedding_type'),
                    'dimension': artifact.metadata.get('dimension'),
                    'total_entities': artifact.metadata.get('total_entities')
                })
            
            # Update summary
            if artifact.file_size_mb:
                result['summary']['total_size_mb'] += artifact.file_size_mb
            
            if artifact.google_drive_path and artifact.backup_confirmed:
                result['summary']['google_drive_backup'] = True
        
        return Response(result)


class CheckMonthlyAvailabilityView(APIView):
    """
    Check if monthly snapshots are available for a country.
    Used before triggering downstream tasks.
    """
    permission_classes = [AllowAny]
    
    def get(self, request, country_code):
        monthly_extracts = CountryArtifact.objects.filter(
            country_code=country_code,
            artifact_type='MONTHLY_EXTRACT',
            artifact_status='ACTIVE'
        ).order_by('temporal_start')
        
        if not monthly_extracts.exists():
            return Response({
                'available': False,
                'message': f'No monthly extracts found for {country_code}'
            })
        
        # Get temporal coverage
        temporal_coverage = []
        for extract in monthly_extracts:
            temporal_coverage.append({
                'start': extract.temporal_start,
                'end': extract.temporal_end,
                'path': extract.artifact_path
            })
        
        return Response({
            'available': True,
            'country_code': country_code,
            'total_monthly_extracts': monthly_extracts.count(),
            'temporal_coverage': temporal_coverage,
            'earliest_date': monthly_extracts.first().temporal_start,
            'latest_date': monthly_extracts.last().temporal_end
        })
