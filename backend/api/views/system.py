import logging

logger = logging.getLogger(__name__)

from django.conf import settings
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny

# NOTE: Heavy service imports (OsmiumFacade, osm_wikidata_resolver, regional_path_service)
# are imported lazily inside the methods that use them to avoid slow startup.
from api.models import (
    RegionHierarchy, PbfFile
)
from api.serializers import (
    RegionHierarchySerializer, PbfFileSerializer
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


class SystemStatusView(APIView):
    """GET /api/system/status/ — planet-init readiness + home-page hydration.

    Readiness is derived from a *finalized* ``PlanetSnapshot`` row — the
    ``finalize`` step of the ``init_planet`` Docker startup command writes
    ``snapshot_date_str`` + ``COMPLETED`` (mirroring ``init_planet``'s own
    ``_acquire_lock`` semantics). A COMPLETED planet ``PbfFile`` row is NOT
    a readiness signal: ``register_planet`` (the first init step) creates
    one before the rest of init has run.

    Returns everything the home page needs to hydrate in one request:
    readiness, latest snapshot info, suggested planet path, extraction /
    embedding counts, and the snapshot-date range for the year selector.
    """
    permission_classes = [AllowAny]

    def get(self, request, *args, **kwargs):
        from core.models import PlanetSnapshot, CountryPipelineProfile, PbfFile
        from api.views.pipeline_status import build_snapshot_dates_payload

        # register_planet rows leave snapshot_date_str NULL; finalize rows
        # set it — so this selects the most recent *finalized* snapshot.
        latest = (
            PlanetSnapshot.objects.exclude(snapshot_date_str__isnull=True)
            .order_by("-snapshot_date", "-created_at")
            .first()
        )

        ready = (
            latest is not None
            and latest.status == PlanetSnapshot.SnapshotStatus.COMPLETED
        )

        snapshot = None
        if latest is not None:
            snapshot = {
                "status": latest.status,
                "snapshot_date": latest.snapshot_date_str,
                "planet_osm_path": latest.planet_osm_path,
                "created_at": (
                    latest.created_at.isoformat() if latest.created_at else None
                ),
                "completed_at": (
                    latest.completed_at.isoformat() if latest.completed_at else None
                ),
            }

        continents_extracted = PbfFile.objects.filter(
            pbf_file_type=PbfFile.PbfType.CONTINENT,
            status=PbfFile.PbfStatus.COMPLETED,
        ).count()

        total_countries = CountryPipelineProfile.objects.count()
        countries_with_embeddings = CountryPipelineProfile.objects.filter(
            has_embeddings=True
        ).count()

        data = {
            "ready": ready,
            "snapshot": snapshot,
            "suggested_planet_file_path": getattr(
                settings, "PLANET_OSM_FILE_PATH", None
            ),
            "continents_extracted": continents_extracted,
            "countries_with_embeddings": countries_with_embeddings,
            "total_countries": total_countries,
        }
        data.update(build_snapshot_dates_payload())
        return Response(data)


class RegionMapDataView(APIView):
    """
    Returns a flat list of countries with GeoJSON geometries and status.
    Uses OSM boundaries if available, falls back to polygon file GeoJSON.
    Each country includes its parent continent name for backend resolution.
    """
    def get(self, request, *args, **kwargs):
        from core.services.snapshot.region_status_service import RegionStatusService
        from core.services.planet_init.polygon_geojson_service import PolygonGeoJsonService
        from core.models import RegionHierarchy, OsmBoundary
        
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
        from core.services.snapshot.region_status_service import RegionStatusService
        from core.models import OsmBoundary, RegionHierarchy
        
        countries = []
        seen_names = set()
        
        # Get all country-level OSM boundaries with polygon file links
        boundaries = OsmBoundary.objects.filter(
            admin_level=2,
            polygon_file__isnull=False
        ).select_related('polygon_file__region_hierarchy__parent').order_by('name')
        
        # Load relations for GeoVectors support check from OSMWikiDataHierarchy
        from core.services.planet_init.osm_wikidata_resolver import get_country_relations_dict
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
                from core.services.snapshot.regional_path_service import normalize_country_slug
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
        from core.services.snapshot.region_status_service import RegionStatusService
        from core.services.planet_init.polygon_geojson_service import PolygonGeoJsonService
        from core.models import RegionHierarchy
        
        countries = []
        
        # Load relations for GeoVectors support check from OSMWikiDataHierarchy
        from core.services.planet_init.osm_wikidata_resolver import get_country_relations_dict
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
                from core.services.snapshot.regional_path_service import normalize_country_slug
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


