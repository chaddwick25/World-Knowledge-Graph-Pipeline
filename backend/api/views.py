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
from .models import (
    RegionHierarchy, Task, PbfFile, PolygonFile
)
from .serializers import (
    RegionHierarchySerializer, PbfFileSerializer, TaskSerializer,
    PolygonFileSerializer
)


class InitialStatusView(APIView):
    """Provides the initial status for the data generation GUI."""

    def get(self, request, *args, **kwargs):
        # 1. Check for the planet file
        planet_file = PbfFile.objects.filter(
            pbf_file_type=PbfFile.PbfType.PLANET,
            status=PbfFile.PbfStatus.COMPLETED
        ).first()

        planet_file_available = planet_file is not None

        # 2. Build the region hierarchy if the planet file is available
        regions_data = []
        availability_context = {'planet_file_available': planet_file_available}

        if planet_file_available:
            top_level_regions = RegionHierarchy.objects.filter(region_type=RegionHierarchy.RegionType.CONTINENT).prefetch_related('children', 'corresponding_pbf')
            self._prepare_availability_context(top_level_regions, availability_context, parent_available=planet_file_available)
            
            serializer = RegionHierarchySerializer(top_level_regions, many=True, context=availability_context)
            regions_data = serializer.data

        # 3. Construct the final response
        response_data = {
            'planet_file_available': planet_file_available,
            'planet_file_details': PbfFileSerializer(planet_file).data if planet_file else None,
            'regions': regions_data,
            'suggested_planet_file_path': getattr(settings, 'PLANET_OSM_FILE_PATH', None)
        }

        return Response(response_data, status=status.HTTP_200_OK)

    def _prepare_availability_context(self, nodes, context, parent_available):
        """Recursively traverse nodes to build the availability context for the serializer."""
        for node in nodes:
            # A node is considered available if its parent is available.
            is_currently_available = parent_available
            context[f'is_available_{node.id}'] = is_currently_available
            
            # The availability of its children depends on whether its *own* PBF has been generated.
            children_are_available = node.corresponding_pbf is not None
            
            if node.children.exists():
                self._prepare_availability_context(node.children.all(), context, parent_available=children_are_available)


# --- PBF File and Task Management Views ---




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


class SystemInitializeView(APIView):
    """
    Initialize the system by running bootstrap tasks:
    - Sync polygon regions
    - Register services
    - Run continents recipe (extract continents + OSM boundaries)
    """
    def post(self, request, *args, **kwargs):
        from django.core.management import call_command
        from extraction.models import OsmBoundary
        
        try:
            steps_completed = []
            
            # Step 1: Sync polygon regions
            logger.info("Running sync_poly_regions management command...")
            call_command('sync_poly_regions')
            steps_completed.append('polygon_files_synced')

            # Step 2: Generate country_relations.json from Geofabrik + SPARQL + RegionHierarchy
            logger.info("Running sync_country_relations (regenerates country_relations.json)...")
            call_command('sync_country_relations', force=True)
            steps_completed.append('country_relations_generated')

            # Step 3: Import country_relations.json into OSMWikiDataHierarchy DB
            logger.info("Running import_country_relations management command...")
            call_command('import_country_relations')
            steps_completed.append('country_relations_imported')
            
            # Step 3a: Generate OSM boundaries from .poly files (for cartographic map)
            logger.info("Generating OSM boundaries from .poly files...")
            boundary_count = OsmBoundary.objects.filter(admin_level=2).count()
            if boundary_count > 100:
                logger.info(f"OSM boundaries already generated ({boundary_count}). Skipping.")
                steps_completed.append('osm_boundaries_skipped')
            else:
                try:
                    call_command('generate_osm_boundaries')
                    steps_completed.append('osm_boundaries_generated')
                    logger.info("OSM boundaries generated successfully")
                except Exception as be:
                    logger.error(f"OSM boundaries generation failed: {str(be)}", exc_info=True)
                    steps_completed.append('osm_boundaries_failed')

            # Step 3b: Run continents recipe (if not already done)
            logger.info("Checking if continents recipe needs to run...")
            continent_pbf_count = PbfFile.objects.filter(
                pbf_file_type=PbfFile.PbfType.CONTINENT,
                status=PbfFile.PbfStatus.COMPLETED
            ).count()
            
            if continent_pbf_count >= 7:
                logger.info(f"Continents already extracted ({continent_pbf_count} continents). Skipping.")
                steps_completed.append('continents_recipe_skipped')
            else:
                logger.info("Running continents recipe (this may take 30-60 minutes)...")
                try:
                    call_command('run_continents_recipe')
                    steps_completed.append('continents_recipe_completed')
                    logger.info("Continents recipe completed successfully")
                except Exception as recipe_error:
                    logger.error(f"Continents recipe failed: {str(recipe_error)}", exc_info=True)
                    steps_completed.append('continents_recipe_failed')
            
            # Step 4: Build subgraph profiles (if continents succeeded)
            if 'continents_recipe_completed' in steps_completed or continent_pbf_count >= 7:
                logger.info("Running prebuild_subgraphs...")
                try:
                    call_command('prebuild_subgraphs', all=True)
                    steps_completed.append('subgraphs_built')
                    logger.info("Subgraph profiles built successfully")
                except Exception as sg_error:
                    logger.error(f"Subgraph build failed: {str(sg_error)}", exc_info=True)
                    steps_completed.append('subgraphs_build_failed')
            
            # Step 5: Resolve country paths (if continents succeeded)
            if 'continents_recipe_completed' in steps_completed or continent_pbf_count >= 7:
                logger.info("Running prebuild_country_paths...")
                try:
                    call_command('prebuild_country_paths')
                    steps_completed.append('country_paths_resolved')
                    logger.info("Country paths resolved successfully")
                except Exception as cp_error:
                    logger.error(f"Country path resolution failed: {str(cp_error)}", exc_info=True)
                    steps_completed.append('country_paths_resolve_failed')
            
            # Step 6: Sync GeoVectors metadata into country_relations.json
            # Populates geovectors_location_tsv / geovectors_tags_tsv so the
            # frontend can determine which countries are clickable on the map.
            logger.info("Running sync_geovectors_metadata...")
            try:
                call_command('sync_geovectors_metadata', save=True)
                steps_completed.append('geovectors_metadata_synced')
                logger.info("GeoVectors metadata synced successfully")
            except Exception as gv_error:
                logger.error(f"GeoVectors metadata sync failed: {str(gv_error)}", exc_info=True)
                steps_completed.append('geovectors_metadata_failed')
            
            # Determine overall status
            has_error = any('failed' in s for s in steps_completed)
            all_done = all(s in steps_completed for s in [
                'polygon_files_synced', 'country_relations_imported',
                'continents_recipe_completed', 'subgraphs_built',

                'country_paths_resolved', 'geovectors_metadata_synced',
                'filesystem_subgraphs_synced',
            ])
            
            if all_done:
                overall_status = 'success'

                message = 'Full system initialization complete (continents + subgraphs + paths + filesystem subgraphs)'
            elif has_error:
                overall_status = 'partial_success'
                message = f'System initialized with some issues: {", ".join(s for s in steps_completed if "failed" in s)}'
            else:
                overall_status = 'partial_success'
                message = 'System initialized (continents skipped, subgraphs/paths not run)'
            
            return Response({
                'status': overall_status,
                'steps_completed': steps_completed,
                'message': message,
                'continents_extracted': continent_pbf_count >= 7,
                'boundaries_count': boundary_count
            }, status=status.HTTP_200_OK)
            
        except Exception as e:
            logger.error(f"System initialization failed: {str(e)}", exc_info=True)
            return Response({
                'status': 'error',
                'error': f'System initialization failed: {str(e)}'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class RegionMapDataView(APIView):
    """
    Returns a flat list of countries with GeoJSON geometries and status.
    Uses OSM boundaries if available, falls back to polygon file GeoJSON.
    Each country includes its parent continent name for backend resolution.
    """
    def get(self, request, *args, **kwargs):
        from extraction.services.region_status_service import RegionStatusService
        from extraction.services.polygon_geojson_service import PolygonGeoJsonService
        from extraction.models import RegionHierarchy, OsmBoundary
        
        try:
            # Check if we have OSM boundaries with polygon file links
            osm_boundaries_with_poly = OsmBoundary.objects.filter(
                admin_level=2,
                polygon_file__isnull=False
            ).exists()

            if osm_boundaries_with_poly:
                # Use OSM boundaries (cartographic quality)
                countries = self._get_countries_from_osm_boundaries()
            else:
                # Fallback to polygon file GeoJSON
                countries = self._get_countries_from_polygon_files()
            
            return Response({
                'countries': countries,
                'total': len(countries),
                'source': 'osm_boundaries' if osm_boundaries_with_poly else 'polygon_files'
            }, status=status.HTTP_200_OK)
            
        except Exception as e:
            logger.error(f"Failed to get region map data: {str(e)}", exc_info=True)
            return Response({
                'error': f'Failed to get region map data: {str(e)}'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    
    def _get_countries_from_osm_boundaries(self):
        """Get countries from OSM boundaries (preferred method)

        Deduplicates by OsmBoundary name so that regions with multiple
        entries (e.g. 'malaysia_singapore_brunei' found under different
        RegionHierarchy paths) only appear once on the map.
        """
        from extraction.services.region_status_service import RegionStatusService
        from extraction.models import OsmBoundary, RegionHierarchy
        
        countries = []
        seen_names = set()
        
        # Get all country-level OSM boundaries with polygon file links
        boundaries = OsmBoundary.objects.filter(
            admin_level=2,
            polygon_file__isnull=False
        ).select_related('polygon_file__region_hierarchy__parent').order_by('name')
        
        # Load relations for GeoVectors support check from OSMWikiDataHierarchy
        from extraction.services.osm_wikidata_resolver import get_country_relations_dict
        relations = get_country_relations_dict()

        for boundary in boundaries:
            # Deduplicate by name — keep the first occurrence
            name_lower = boundary.name.lower()
            if name_lower in seen_names:
                continue
            seen_names.add(name_lower)

            try:
                region = boundary.polygon_file.region_hierarchy
                if not region:
                    continue

                continent = region.parent
                if not continent or continent.name == 'overrides':
                    continue
                
                status_data = RegionStatusService.get_region_status(region)
                
                # Check GeoVectors support
                from extraction.services.regional_path_service import normalize_country_slug
                is_supported = False
                search_term_underscore = normalize_country_slug(region.name)
                search_term_hyphen = search_term_underscore.replace('_', '-')
                for k, v in relations.items():
                    slug = v.get('slug', '')
                    name = normalize_country_slug(v.get('name', ''))
                    if slug == search_term_hyphen or slug == search_term_underscore or name == search_term_underscore:
                        if v.get('geovectors_location_tsv'):
                            is_supported = True
                        break

                countries.append({
                    'id': str(region.id),
                    'name': boundary.name,
                    'iso_code': boundary.iso_code or '',
                    'continent': continent.name,
                    'continent_id': str(continent.id),
                    'geometry': boundary.geometry,
                    'status': status_data,
                    'is_geovectors_supported': is_supported
                })
            except Exception as e:
                logger.warning(f"Failed to process boundary {boundary.name}: {str(e)}")
                continue
        
        return countries
    
    def _get_countries_from_polygon_files(self):
        """Fallback: Get countries from polygon files (old method)

        Recursively collects countries — including children of composite
        countries like united_kingdom (england, scotland, wales) so the
        map shows individual clickable regions after TSV splits.
        """
        from extraction.services.region_status_service import RegionStatusService
        from extraction.services.polygon_geojson_service import PolygonGeoJsonService
        from extraction.models import RegionHierarchy
        
        countries = []
        
        # Load relations for GeoVectors support check from OSMWikiDataHierarchy
        from extraction.services.osm_wikidata_resolver import get_country_relations_dict
        relations = get_country_relations_dict()
        
        def _collect(region, continent_name, continent_id, depth=0):
            """Recursively collect a region and its children into the map."""
            if depth > 3:
                return  # safety: prevent infinite recursion
            
            geometry = None
            if region.polygon_file:
                try:
                    geometry = PolygonGeoJsonService.get_or_generate_geojson(region.polygon_file)
                except Exception as e:
                    logger.warning(f"Could not get GeoJSON for {region.name}: {str(e)}")
            
            if geometry is not None:
                status_data = RegionStatusService.get_region_status(region)
                
                # Check GeoVectors support
                from extraction.services.regional_path_service import normalize_country_slug
                is_supported = False
                search_term_underscore = normalize_country_slug(region.name)
                search_term_hyphen = search_term_underscore.replace('_', '-')
                
                # 1. Check OSMWikiDataHierarchy relations (authoritative)
                for k, v in relations.items():
                    slug = v.get('slug', '')
                    name = normalize_country_slug(v.get('name', ''))
                    if slug == search_term_hyphen or slug == search_term_underscore or name == search_term_underscore:
                        if v.get('geovectors_location_tsv'):
                            is_supported = True
                            break
                
                # 2. Fallback: check if a location TSV exists on disk
                #    (covers split countries like england/scotland/wales
                #     that have TSVs but no OSMWikiDataHierarchy entry)
                if not is_supported:
                    from django.conf import settings
                    emb_root = Path(settings.EMBEDDINGS_ROOT)
                    disk_path = emb_root / continent_name / search_term_underscore / f"{search_term_underscore}-location.tsv.gz"
                    if disk_path.exists():
                        is_supported = True
                
                countries.append({
                    'id': str(region.id),
                    'name': region.name,
                    'continent': continent_name,
                    'continent_id': str(continent_id),
                    'geometry': geometry,
                    'status': status_data,
                    'is_geovectors_supported': is_supported
                })
            
            # Recurse into children
            children = RegionHierarchy.objects.filter(parent=region).select_related('polygon_file', 'corresponding_pbf')
            for child in children:
                _collect(child, continent_name, continent_id, depth + 1)
        
        # Get all continents and collect their entire subtree
        continents = RegionHierarchy.objects.filter(
            region_type=RegionHierarchy.RegionType.CONTINENT
        ).prefetch_related(
            'children__polygon_file',
            'children__corresponding_pbf'
        )
        
        for continent in continents:
            if continent.name == 'overrides':
                continue
            for country in continent.children.all():
                _collect(country, continent.name, continent.id)
        
        return countries


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
            from extraction.services.temporal_extract_service import TemporalExtractService
            
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





