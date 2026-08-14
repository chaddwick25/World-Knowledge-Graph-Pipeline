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


class PolygonFileListView(generics.ListAPIView):
    queryset = PolygonFile.objects.filter(is_active=True)
    serializer_class = PolygonFileSerializer


class PbfFileListView(generics.ListCreateAPIView):
    queryset = PbfFile.objects.all().order_by('-registered_at')
    serializer_class = PbfFileSerializer
    
    def get_queryset(self):
        """Filter PBF files by parent_pbf and extraction_level if provided."""
        queryset = super().get_queryset()
        
        parent_pbf_id = self.request.query_params.get('parent_pbf')
        extraction_level = self.request.query_params.get('extraction_level')
        
        if parent_pbf_id:
            queryset = queryset.filter(parent_pbf_id=parent_pbf_id)
        
        if extraction_level:
            queryset = queryset.filter(extraction_level=extraction_level)
        
        return queryset

    def create(self, request, *args, **kwargs):
        """Register a new PBF file and optionally link it to a region hierarchy."""
        path = request.data.get('path')
        region_hierarchy_id = request.data.get('region_hierarchy_id') # Optional ID

        if not path:
            return Response({'error': 'path is required'}, status=status.HTTP_400_BAD_REQUEST)

        # Use update_or_create to handle existing records gracefully
        pbf_file, created = PbfFile.objects.get_or_create(path=path)
        if not created and pbf_file.status == PbfFile.PbfStatus.COMPLETED:
            # If file is already registered and complete, we can still proceed to link it
            pass
        else:
            # Validate with osmium for new files or incomplete records
            try:
                from extraction.services.osmium_facade import OsmiumFacade
                facade = OsmiumFacade()
                result = facade.file_info(path)
                if result.get('exit_code') != 0:
                    return Response({'error': result.get('error', 'Unknown error')}, status=status.HTTP_400_BAD_REQUEST)

                info = result.get('info', {})
                file_info = info.get('file', {})
                has_history = 'history' in result.get('raw_output', '').lower()
                
                # Update the PBF file record with details
                pbf_file.status = PbfFile.PbfStatus.COMPLETED
                pbf_file.size_bytes = file_info.get('size')
                pbf_file.has_history = has_history
                pbf_file.format_version = file_info.get('version')
                pbf_file.file_type = file_info.get('data_format')
                pbf_file.compression = file_info.get('compression')
                pbf_file.generator = file_info.get('generator')
                pbf_file.raw_info = result.get('raw_output', '')
                
                # Extract timestamps if available in fileinfo
                if info.get('data', {}).get('timestamp'):
                    pbf_file.min_timestamp = info['data']['timestamp'].get('min')
                    pbf_file.max_timestamp = info['data']['timestamp'].get('max')
                
                pbf_file.temporal_metadata_source = PbfFile.TemporalMetadataSource.AUTO_DETECTED
                pbf_file.save()
                
                logger.info(f"Registered PBF file {path} with size {pbf_file.size_bytes}")

            except Exception as e:
                return Response({'error': f'Failed to validate or register PBF file: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        # --- Optional Linking Logic ---
        if region_hierarchy_id:
            try:
                region_node = RegionHierarchy.objects.get(id=region_hierarchy_id)
                region_node.corresponding_pbf = pbf_file
                region_node.save()
            except RegionHierarchy.DoesNotExist:
                return Response({'error': f'RegionHierarchy node with id {region_hierarchy_id} not found.'}, status=status.HTTP_404_NOT_FOUND)

        serializer = self.get_serializer(pbf_file)
        return Response(serializer.data, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)





class RegisterPlanetPbfView(APIView):
    """
    Registers a new Planet PBF file in the system.
    """
    def post(self, request, *args, **kwargs):
        file_path = request.data.get('path')

        if not file_path:
            return Response({'error': 'The "path" field is required.'}, status=status.HTTP_400_BAD_REQUEST)

        # Basic validation: check if the file exists on the server
        if not Path(file_path).exists():
            return Response({'error': f'File not found at path: {file_path}'}, status=status.HTTP_400_BAD_REQUEST)

        # Check if a planet file is already registered
        if PbfFile.objects.filter(pbf_file_type=PbfFile.PbfType.PLANET).exists():
            return Response({'error': 'A planet file is already registered. Only one is allowed.'}, status=status.HTTP_409_CONFLICT)

        # Create the PBF file record
        try:
            pbf_file = PbfFile.objects.create(
                path=file_path,
                pbf_file_type=PbfFile.PbfType.PLANET,
                status=PbfFile.PbfStatus.COMPLETED,
                size_bytes=Path(file_path).stat().st_size,
                raw_info={},
                extraction_level=PbfFile.ExtractionLevel.PLANET
            )
            serializer = PbfFileSerializer(pbf_file)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        except Exception as e:
            return Response({'error': f'Failed to register planet file: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class ExtractionChainView(APIView):
    """
    Build extraction chain configuration for a target region.
    Similar to recipe scripts like run_canada_recipe.py
    """
    def post(self, request, *args, **kwargs):
        from extraction.services.extraction_chain_builder import ExtractionChainBuilder
        
        target_region_id = request.data.get('region_id')
        
        if not target_region_id:
            return Response({
                'error': 'region_id is required'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            # Build extraction chain
            chain_config = ExtractionChainBuilder.generate_recipe_config(target_region_id)
            
            # Validate chain
            validation = ExtractionChainBuilder.validate_chain(target_region_id)
            
            return Response({
                'config': chain_config,
                'validation': validation
            }, status=status.HTTP_200_OK)
            
        except ValueError as e:
            return Response({
                'error': str(e)
            }, status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            logger.error(f"Failed to build extraction chain: {str(e)}", exc_info=True)
            return Response({
                'error': f'Failed to build extraction chain: {str(e)}'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


