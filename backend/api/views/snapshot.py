import json
import os
import subprocess
from pathlib import Path
import logging

logger = logging.getLogger(__name__)

from django.conf import settings
from django.db import connections
from rest_framework import generics, status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny

# NOTE: Heavy service imports (OsmiumFacade, osm_wikidata_resolver, regional_path_service)
# are imported lazily inside the methods that use them to avoid slow startup.
from api.models import (
    RegionHierarchy, Task, PbfFile, PolygonFile
)
from api.serializers import (
    RegionHierarchySerializer, PbfFileSerializer, TaskSerializer,
    PolygonFileSerializer
)


class CreateSnapshotTaskView(APIView):
    def post(self, request):
        source_pbf_path = request.data.get("pbf_file_path") # Changed from source_pbf_id
        dates = request.data.get("dates")

        logger.info(f"CreateSnapshotTaskView received request: pbf_file_path={source_pbf_path}, dates={dates}")

        if not source_pbf_path or not dates:
            error_msg = f"Missing required fields - pbf_file_path: {bool(source_pbf_path)}, dates: {bool(dates)}"
            logger.error(error_msg)
            return Response({"error": "Missing 'pbf_file_path' or 'dates'", "details": error_msg}, status=status.HTTP_400_BAD_REQUEST)

        # Validate dates is a list with at least one element
        if not isinstance(dates, list) or len(dates) == 0:
            error_msg = f"Invalid dates format. Expected list, got: {type(dates).__name__}"
            logger.error(error_msg)
            return Response({"error": "Invalid dates format", "details": error_msg}, status=status.HTTP_400_BAD_REQUEST)

        # Check if it's a URL or a local path
        is_url = source_pbf_path.startswith('http://') or source_pbf_path.startswith('https://')

        if is_url:
            # For URLs, we can't check for history beforehand. Assume it has history.
            # The snapshot script will fail if it doesn't, which is acceptable.
            logger.info(f"Processing URL-based PBF: {source_pbf_path}")
            pass
        else:
            # For local paths, ensure the file exists
            if not Path(source_pbf_path).exists():
                error_msg = f"Local file not found: {source_pbf_path}"
                logger.error(error_msg)
                return Response({"error": error_msg}, status=status.HTTP_400_BAD_REQUEST)
            logger.info(f"Processing local PBF file: {source_pbf_path}")

        task = Task.objects.create(
            task_type=Task.TaskType.CREATE_SNAPSHOT,
            parameters={
                "source_pbf_path": source_pbf_path,
                "dates": dates
            }
        )

        # The task is now pending. A separate worker process will pick it up.

        serializer = TaskSerializer(task)
        return Response(serializer.data, status=status.HTTP_202_ACCEPTED)


class CreatePbfExtractTaskView(APIView):
    def post(self, request):
        from pathlib import Path
        from django.conf import settings
        
        source_pbf_id = request.data.get("source_pbf_id")
        poly_file_path = request.data.get("poly_file_path")
        output_pbf_path = request.data.get("output_pbf_path")
        cpu_core_id = request.data.get("cpu_core_id")
        default_output_root = getattr(settings, 'OSM_WIKIDATA_EXTRACTIONS_DIR', None)
        base_output_dir = request.data.get("base_output_dir", default_output_root)

        if not all([source_pbf_id, poly_file_path, cpu_core_id]):
            return Response({"error": "Missing required parameters."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            PbfFile.objects.get(id=source_pbf_id)
        except PbfFile.DoesNotExist:
            return Response({"error": "Source PBF file not found."}, status=status.HTTP_404_NOT_FOUND)

        # If output_pbf_path not provided, create structured path from poly file
        if not output_pbf_path:
            # Extract region name from poly file (e.g., portugal.poly -> portugal)
            poly_path = Path(poly_file_path)
            region_name = poly_path.stem.lower()
            
            # Create structured path: data/temporal-extracts/{region}/{region}.pbf
            base_dir = Path(base_output_dir)
            region_dir = base_dir / region_name
            output_pbf_path = str(region_dir / f"{region_name}.pbf")

        task = Task.objects.create(
            task_type=Task.TaskType.EXTRACT_PBF,
            parameters={
                "source_pbf_id": source_pbf_id,
                "poly_file_path": poly_file_path,
                "output_pbf_path": output_pbf_path,
                "cpu_core_id": cpu_core_id
            }
        )

        serializer = TaskSerializer(task)
        return Response(serializer.data, status=status.HTTP_202_ACCEPTED)


class TaskStatusView(generics.RetrieveAPIView):
    queryset = Task.objects.all()
    serializer_class = TaskSerializer
    lookup_field = 'id'




class PbfTemporalRangesView(APIView):
    """
    Returns all PBF files with their temporal ranges for display on the home page.
    Shows min/max timestamps for each extracted region.
    """
    def get(self, request, *args, **kwargs):
        try:
            pbf_files = PbfFile.objects.filter(
                status=PbfFile.PbfStatus.COMPLETED
            ).exclude(
                min_timestamp__isnull=True,
                max_timestamp__isnull=True
            ).select_related('parent_pbf').order_by('-registered_at')
            
            results = []
            for pbf in pbf_files:
                # Extract region name from file path
                region_name = 'Unknown'
                if pbf.path:
                    path_parts = Path(pbf.path).stem.split('.')[0]
                    region_name = path_parts
                
                results.append({
                    'id': str(pbf.id),
                    'region_name': region_name,
                    'pbf_type': pbf.get_pbf_file_type_display() if hasattr(pbf, 'get_pbf_file_type_display') else pbf.pbf_file_type,
                    'extraction_level': pbf.get_extraction_level_display() if pbf.extraction_level and hasattr(pbf, 'get_extraction_level_display') else pbf.extraction_level,
                    'min_timestamp': pbf.min_timestamp.isoformat() if pbf.min_timestamp else None,
                    'max_timestamp': pbf.max_timestamp.isoformat() if pbf.max_timestamp else None,
                    'temporal_metadata_source': pbf.get_temporal_metadata_source_display() if pbf.temporal_metadata_source and hasattr(pbf, 'get_temporal_metadata_source_display') else pbf.temporal_metadata_source,
                    'has_history': pbf.has_history,
                    'size_mb': round(pbf.size_bytes / (1024 * 1024), 2) if pbf.size_bytes else None,
                    'path': pbf.path,
                    'parent_region': Path(pbf.parent_pbf.path).stem.split('.')[0] if pbf.parent_pbf and pbf.parent_pbf.path else None,
                    'registered_at': pbf.registered_at.isoformat() if pbf.registered_at else None,
                    'yearly_extracts_generated': pbf.yearly_extracts_generated,
                    'yearly_extracts_completed_at': pbf.yearly_extracts_completed_at.isoformat() if pbf.yearly_extracts_completed_at else None,
                    'yearly_extracts_count': pbf.yearly_extracts_count,
                    'yearly_extracts_year_range': pbf.yearly_extracts_year_range,
                    'monthly_extracts_generated': pbf.monthly_extracts_generated,
                    'monthly_extracts_completed_at': pbf.monthly_extracts_completed_at.isoformat() if pbf.monthly_extracts_completed_at else None,
                    'monthly_extracts_count': pbf.monthly_extracts_count,
                    'monthly_extracts_year_range': pbf.monthly_extracts_year_range
                })
            
            return Response({
                'count': len(results),
                'results': results
            }, status=status.HTTP_200_OK)
            
        except Exception as e:
            logger.error(f"Error fetching temporal ranges: {e}", exc_info=True)
            return Response({
                'error': str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)





# --- Temporal Extraction and Snapshot Views ---

class TemporalExtractView(APIView):
    """
    Generate temporal extracts (yearly or monthly) from region extracts.
    This is the preprocessing layer.
    """
    permission_classes = [AllowAny]
    
    def post(self, request):
        """
        Generate temporal extracts.
        
        Payload:
        {
            "source_pbf_id": "uuid",
            "granularity": "yearly" | "monthly",
            "start_year": 2022,
            "end_year": 2024,
            "output_directory": "/path/to/output",
            "parallel": true,
            "cpu_cores": 20
        }
        """
        required_fields = ['source_pbf_id', 'granularity', 'start_year', 'end_year']
        for field in required_fields:
            if field not in request.data:
                return Response({
                    'error': f'{field} is required'
                }, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            from core.services.snapshot.temporal_extract_service import TemporalExtractService
            
            service = TemporalExtractService()
            result = service.generate_temporal_extracts(request.data)
            
            if result.get('success'):
                return Response(result, status=status.HTTP_200_OK)
            else:
                return Response(result, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
                
        except Exception as e:
            logger.error(f"Error generating temporal extracts: {e}", exc_info=True)
            return Response({
                'error': str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)





