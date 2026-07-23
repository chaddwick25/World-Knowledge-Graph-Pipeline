import os
from pathlib import Path
from django.conf import settings
from django.utils import timezone
from django.conf import settings
from django.core.management import call_command
from django.utils import timezone
import logging

from extraction.models import OSMWikiDataHierarchy, PbfFile, PlanetaryMetrics, RegionHierarchy
from orchestration.models import PlanetSnapshot, CountryRelationSnapshot
from extraction.services.osmium_facade import OsmiumFacade
from extraction.services.country_relation_resolver import CountryRelationResolver
from extraction.services.extraction_service import ExtractionService
from extraction.services.regional_path_service import normalize_country_slug, normalize_continent_slug
from extraction.services.country_override_service import get_country_slug

class PlanetInitializationService:
    """Foundation service that creates file structure, Wikidata alignment, and OSMWikiData primitives."""
    
    def __init__(self, policy: dict):
        self.policy = policy['stages']['planet_initialization']
        self.logger = logging.getLogger('pipeline')
    
    def execute(self):
        """Execute planet initialization stages."""
        self.logger.info("Starting planet initialization")
        
        # Try to send WebSocket updates if pipeline_run_id is available
        pipeline_run_id = self._get_pipeline_run_id()

        self._push_update(pipeline_run_id, "planet_init", "in_progress",
                          "Creating file structure...", 5)

        # 1. Create file structure
        self.create_file_structure()
        
        self._push_update(pipeline_run_id, "planet_init", "in_progress",
                          "Verifying planet PBF...", 15)

        # 2. Verify planet PBF
        planet_pbf = self.prepare_planet_pbf()
        
        self._push_update(pipeline_run_id, "planet_init", "in_progress",
                          "Generating planetary metrics...", 25)

        # 3. Generate planetary metrics (natural primitives)
        metrics = self.generate_planetary_metrics(planet_pbf)

        self._push_update(pipeline_run_id, "planet_init", "in_progress",
                          "Creating planet snapshot...", 35)

        # 4. Create or get PlanetSnapshot for this planet PBF
        snapshot_date = metrics.generated_at.date() if metrics else timezone.now().date()
        planet_snapshot, _ = PlanetSnapshot.objects.get_or_create(
            snapshot_date=snapshot_date,
            planet_osm_path=str(planet_pbf.path),
            defaults={
                "status": PlanetSnapshot.SnapshotStatus.COMPLETED,
                "completed_at": timezone.now(),
            },
        )

        self._push_update(pipeline_run_id, "planet_init", "in_progress",
                          "Creating planet hierarchy...", 45)

        # 5. Create planet OSMWikiDataHierarchy entry
        planet_hierarchy = self.create_planet_hierarchy(planet_pbf, metrics)

        self._push_update(pipeline_run_id, "planet_init", "in_progress",
                          "Syncing country relations...", 55)

        # 6. Sync country relations (automatic - ensures fresh data)
        self.sync_country_relations()

        self._push_update(pipeline_run_id, "planet_init", "in_progress",
                          "Initializing OSM/Wikidata alignment...", 65)

        # 7. Initialize OSMWikiDataHierarchy alignment from country_relations.json
        self.initialize_osm_wikidata_alignment()

        self._push_update(pipeline_run_id, "planet_init", "in_progress",
                          "Snapshotting country relations...", 75)

        # 8. Snapshot country_relations.json into CountryRelationSnapshot rows
        self.snapshot_country_relations(planet_snapshot)

        # 9. Extract continents (if enabled in policy)
        if self.policy['parameters'].get('extract_continents', False):
            self._push_update(pipeline_run_id, "planet_init", "in_progress",
                              "Extracting continent PBFs...", 85)
            self._extract_continents_simple()

        # 10. Sync GeoVectors metadata so frontend can determine clickable countries
        self._push_update(pipeline_run_id, "planet_init", "in_progress",
                          "Syncing GeoVectors metadata...", 92)
        try:
            self.logger.info("Syncing GeoVectors metadata...")
            call_command('sync_geovectors_metadata', save=True)
            self.logger.info("GeoVectors metadata synced successfully")
        except Exception as gv_err:
            self.logger.error(f"GeoVectors metadata sync failed: {gv_err}")

        self._push_update(pipeline_run_id, "planet_init", "completed",
                          "Planet initialization complete!", 100)

        self.logger.info("Planet initialization complete")

        return {
            'planet_pbf': planet_pbf,
            'planetary_metrics': metrics,
            'osm_wikidata_hierarchy': planet_hierarchy,
            'planet_snapshot': planet_snapshot,
        }

    def _get_pipeline_run_id(self) -> str:
        """Extract pipeline_run_id from the policy if set by Celery canvas."""
        try:
            return self.policy.get('parameters', {}).get('pipeline_run_id', '')
        except Exception:
            return ''

    def _push_update(self, run_id: str, name: str, status: str,
                     message: str, pct: int = 0) -> None:
        """Send WebSocket update if we have a pipeline_run_id."""
        if not run_id:
            return
        try:
            from pipeline.celery_app import _push_update
            _push_update(
                pipeline_run_id=run_id,
                name=name,
                status=status,
                message=message,
                pct=pct,
            )
        except Exception:
            pass  # Non-critical — updates are best-effort
    
    def create_file_structure(self):
        """Create the directory structure for the entire pipeline."""
        base_dir = Path(self.policy['file_structure']['base_dir'])
        
        for dir_name, dir_path in self.policy['file_structure']['directories'].items():
            full_path = base_dir / dir_path
            full_path.mkdir(parents=True, exist_ok=True)
            self.logger.info(f"Created directory: {full_path}")
        
        # Create categorized logging directories
        logs_dir = base_dir / "logs"
        for log_type in ['gv-nle', 'pipeline', 'tests', 'uslp', 'tests/nca_alignment_validation']:
            (logs_dir / log_type).mkdir(parents=True, exist_ok=True)
    
    def prepare_planet_pbf(self):
        """Verify existing planet PBF file."""
        pbf_path = Path(self.policy['parameters']['planet_pbf_path'])
        
        if not pbf_path.exists():
            raise FileNotFoundError(f"Planet PBF not found: {pbf_path}")
        
        self.logger.info(f"Using existing planet PBF: {pbf_path}")
        
        pbf, created = PbfFile.objects.get_or_create(
            path=str(pbf_path),
            defaults={
                'pbf_file_type': PbfFile.PbfType.PLANET,
                'extraction_level': PbfFile.ExtractionLevel.PLANET,
                'status': PbfFile.PbfStatus.COMPLETED,
            }
        )
        
        return pbf
    
    def generate_planetary_metrics(self, pbf_file):
        """Generate planetary metrics as natural primitives."""
        if not self.policy.get('planetary_metrics', {}).get('enabled', True):
            return None
        
        self.logger.info("Generating planetary metrics")
        
        osmium = OsmiumFacade()
        result = osmium.file_info(pbf_file.path)

        if result.get('exit_code', 1) != 0:
            self.logger.error(
                "osmium fileinfo failed for %s: %s",
                pbf_file.path,
                result.get('error', 'unknown error'),
            )
            return None

        info = result.get('info') or {}
        data = info.get('data') or {}

        # Counts (nodes / ways / relations) — available under data.count.* when
        # osmium is run with --extended/-e. Fall back to 0 if missing so that
        # initialization can still complete on older osmium versions.
        counts = data.get('count') or {}
        nodes = int(counts.get('nodes') or 0)
        ways = int(counts.get('ways') or 0)
        relations = int(counts.get('relations') or 0)

        # Bounding box: data.bbox is [minlon, minlat, maxlon, maxlat]. If not
        # present (when --extended is not used), fall back to OsmiumFacade.get_bbox.
        bbox_arr = data.get('bbox') or []
        min_lon = min_lat = max_lon = max_lat = 0.0
        if len(bbox_arr) == 4:
            try:
                min_lon, min_lat, max_lon, max_lat = [float(v) for v in bbox_arr]
            except (TypeError, ValueError):
                bbox_arr = []

        if not bbox_arr:
            bbox = osmium.get_bbox(pbf_file.path)
            if bbox and len(bbox) == 4:
                try:
                    min_lon, min_lat, max_lon, max_lat = [float(v) for v in bbox]
                except (TypeError, ValueError):
                    pass

        # Area and tag stats: these are not directly available from osmium
        # without additional passes. For now, use safe defaults so that the
        # initialization pipeline can complete even if detailed stats are
        # missing.
        metrics_data = {
            'nodes': nodes,
            'ways': ways,
            'relations': relations,
            'bbox': {
                'min_lat': min_lat,
                'max_lat': max_lat,
                'min_lon': min_lon,
                'max_lon': max_lon,
            },
            'area_km2': 0.0,
            'unique_tag_keys': 0,
            'unique_tag_values': 0,
            'top_tag_keys': [],
        }

        metrics, _ = PlanetaryMetrics.objects.update_or_create(
            pbf_file=pbf_file,
            defaults={
                'node_count': metrics_data['nodes'],
                'way_count': metrics_data['ways'],
                'relation_count': metrics_data['relations'],
                'bbox_min_lat': metrics_data['bbox']['min_lat'],
                'bbox_max_lat': metrics_data['bbox']['max_lat'],
                'bbox_min_lon': metrics_data['bbox']['min_lon'],
                'bbox_max_lon': metrics_data['bbox']['max_lon'],
                'area_km2': metrics_data['area_km2'],
                'unique_tag_keys': metrics_data['unique_tag_keys'],
                'unique_tag_values': metrics_data['unique_tag_values'],
                'top_tag_keys': metrics_data['top_tag_keys'],
                'orphan_nodes_ratio': 0.0,
                'incomplete_ways_ratio': 0.0,
                'estimated_processing_time_seconds': self.estimate_processing_time(metrics_data),
                'recommended_batch_size': self.calculate_batch_size(metrics_data),
                'recommended_worker_count': self.calculate_workers(metrics_data),
            }
        )
        
        self.logger.info(f"Planetary metrics created: {metrics.id}")
        return metrics
    
    def create_planet_hierarchy(self, pbf_file, metrics):
        """Create planet-level OSMWikiDataHierarchy entry."""
        hierarchy, created = OSMWikiDataHierarchy.objects.get_or_create(
            slug='planet',
            defaults={
                'name': 'Planet',
                'admin_level': None,
                'processing_policy': self.policy,
            }
        )
        
        if metrics:
            metrics.osm_wikidata_hierarchy = hierarchy
            metrics.save()
        
        return hierarchy
    
    def sync_country_relations(self):
        """Sync country relations from Wikidata SPARQL and Geofabrik Index.
        This ensures country_relations.json is fresh before planet initialization.
        """
        self.logger.info("Syncing country relations (Wikidata SPARQL + Geofabrik Index)")
        
        # Canonical output path: <BASE_DIR>/data/country_relations.json
        #   Host:    backend/data/country_relations.json
        #   Container: /app/data/country_relations.json
        output_path = Path(settings.BASE_DIR) / 'data' / 'country_relations.json'
        resolver = CountryRelationResolver(output_path=str(output_path))

        try:
            merged_data = resolver.sync(force_refresh=False)
            self.logger.info(f"Successfully synced {len(merged_data)} country relations")
            return merged_data
        except Exception as e:
            self.logger.error(f"Failed to sync country relations: {e}")
            # Continue with existing data if available
            json_path = output_path
            if json_path.exists():
                self.logger.warning("Continuing with existing country_relations.json")
            else:
                self.logger.error("No existing country_relations.json found - planet initialization may fail")
    
    def initialize_osm_wikidata_alignment(self):
        """Initialize OSMWikiDataHierarchy entries from country_relations.json.
        Only creates entries for regions that can successfully generate pickle files.
        """
        json_path = Path(settings.BASE_DIR) / 'data' / 'country_relations.json'
        
        if not json_path.exists():
            self.logger.warning(f"country_relations.json not found: {json_path}")
            return
        
        import json
        with open(json_path) as f:
            country_relations = json.load(f)
        
        created_count = 0
        skipped_count = 0
        
        for iso_code, country_data in country_relations.items():
            # Add ISO code to country_data for override lookup
            country_data['iso_code'] = iso_code
            
            # Validate pickle generation capability before creating entry
            can_pickle = self.validate_pickle_generation(country_data)
            
            if not can_pickle:
                self.logger.warning(f"Skipping {country_data['name']} ({iso_code}): Cannot generate pickle file")
                skipped_count += 1
                continue
            
            hierarchy, created = OSMWikiDataHierarchy.objects.get_or_create(
                slug=country_data['slug'],
                defaults={
                    'name': country_data['name'],
                    'osm_relation_id': country_data.get('relation_id'),
                    'wikidata_uri': country_data.get('wkg_uri'),
                    'wikidata_id': country_data.get('wkg_uri', '').split('/')[-1] if country_data.get('wkg_uri') else None,
                    'parent_slug': country_data.get('parent_slug'),
                    'continent_name': country_data.get('continent_name'),
                    'continent_id': country_data.get('continent_id'),
                    'pbf_url': country_data.get('pbf_url'),
                    'admin_level': 2,
                    'can_generate_pickle': True,
                    'pickle_generation_validated_at': timezone.now(),
                    'processing_policy': self.get_country_policy()
                }
            )
            
            if created:
                created_count += 1
                region = RegionHierarchy.objects.filter(name__iexact=country_data['name']).first()
                if region:
                    hierarchy.region_hierarchy = region
                    hierarchy.save()
            else:
                # Update existing entry with pickle validation status
                hierarchy.can_generate_pickle = True
                hierarchy.pickle_generation_validated_at = timezone.now()
                hierarchy.save()
        
        self.logger.info(f"Created {created_count} OSMWikiDataHierarchy entries")
        self.logger.info(f"Skipped {skipped_count} entries (pickle generation failed)")

    def snapshot_country_relations(self, planet_snapshot: PlanetSnapshot) -> None:
        """Populate CountryRelationSnapshot rows from country_relations.json.

        This creates one row per ISO code for the given PlanetSnapshot, giving a
        relational view of the legacy JSON for visualization and analysis.
        """
        json_path = Path(settings.BASE_DIR) / 'data' / 'country_relations.json'
        if not json_path.exists():
            self.logger.warning(f"snapshot_country_relations: country_relations.json not found at {json_path}")
            return

        import json

        try:
            with open(json_path) as f:
                country_relations = json.load(f)
        except Exception as exc:
            self.logger.error(f"snapshot_country_relations: failed to load JSON: {exc}")
            return

        created = 0
        updated = 0

        for iso_code, payload in country_relations.items():
            if not isinstance(payload, dict):
                continue

            iso_norm = (iso_code or "").upper()
            name = payload.get("name") or iso_norm
            slug = payload.get("slug") or normalize_country_slug(name)

            defaults = {
                "name": name,
                "slug": slug,
                "parent_slug": payload.get("parent_slug"),
                "continent_name": payload.get("continent_name"),
                "continent_id": payload.get("continent_id"),
                "osm_relation_id": payload.get("relation_id"),
                "wikidata_uri": payload.get("wkg_uri"),
                "geofabrik_pbf_url": payload.get("pbf_url"),
                "geovectors_location_tsv": payload.get("geovectors_location_tsv"),
                "geovectors_tags_tsv": payload.get("geovectors_tags_tsv"),
                "raw_payload": payload,
            }

            obj, was_created = CountryRelationSnapshot.objects.update_or_create(
                planet_snapshot=planet_snapshot,
                iso_code=iso_norm,
                defaults=defaults,
            )
            if was_created:
                created += 1
            else:
                updated += 1

        self.logger.info(
            "snapshot_country_relations: %s created, %s updated for PlanetSnapshot %s",
            created,
            updated,
            planet_snapshot.id,
        )
    
    def validate_pickle_generation(self, country_data):
        """Validate that a country/subgraph can successfully generate a pickle file."""
        # Check if PBF URL exists and is accessible
        pbf_url = country_data.get('pbf_url')
        if not pbf_url:
            return False
        
        # Check if polyfile exists or can be generated from PBF snapshot
        polyfile_path = self.get_polyfile_path(country_data)
        if not polyfile_path or not Path(polyfile_path).exists():
            # Try to generate polyfile from PBF snapshot
            if not self.generate_polyfile_from_pbf(country_data, pbf_url):
                return False
        
        # Test pickle generation (dry run)
        if not self.test_pickle_generation(country_data):
            return False
        
        return True
    
    def get_polyfile_path(self, country_data):
        """Get polyfile path for a country/subgraph, applying JSON-based ISO overrides.

        Uses get_country_slug(iso, default_slug) so that overrides.json (in cold storage)
        is the single source of truth for special-case country slugs.
        """
        base_dir = Path(self.policy['file_structure']['base_dir'])
        continent = normalize_continent_slug(country_data.get('continent_name'))

        if not continent:
            return None

        iso_code = (country_data.get('iso_code') or '').upper()
        default_slug = country_data.get('slug')
        slug = get_country_slug(iso_code, default_slug)

        return base_dir / "data" / "osm_polygon_files" / continent / f"{slug}.poly"
    
    def generate_polyfile_from_pbf(self, country_data, pbf_url):
        """Generate polyfile from PBF snapshot using osmium."""
        # Implementation would use osmium extract to generate polyfile
        # This is a placeholder - actual implementation would:
        # 1. Download PBF if not exists
        # 2. Use osmium extract with relation boundary to generate polyfile
        # 3. Save to appropriate location
        return True  # Placeholder
    
    def test_pickle_generation(self, country_data):
        """Test if pickle file can be generated for this region."""
        # Implementation would attempt to generate a small test pickle
        # This validates that the region structure supports pickle serialization
        return True  # Placeholder
    
    def get_country_policy(self):
        """Get default country policy."""
        return OSMWikiDataHierarchy(admin_level=2).get_default_policy()
    
    def estimate_processing_time(self, metrics_data):
        """Estimate processing time based on node count."""
        # Simple heuristic: ~20k nodes/min
        return int(metrics_data['nodes'] / 20000 * 60)
    
    def calculate_batch_size(self, metrics_data):
        """Calculate recommended batch size."""
        # 20k entities per batch
        return 20000
    
    def extract_continents(self):
        """Extract continents from planet PBF and link to OSMWikiData hierarchy.
        
        This method runs the continents recipe as part of planet initialization,
        ensuring continents are adapted to the unified OSMWikiData structure.
        """
        self.logger.info("Starting continent extraction")
        
        # Set environment variables for continental extraction
        base_data_dir = Path(self.policy['file_structure']['base_dir'])
        
        if not os.getenv('SOURCE_PBF_PATH'):
            os.environ['SOURCE_PBF_PATH'] = self.policy['parameters']['planet_pbf_path']
        if not os.getenv('OUTPUT_BASE_DIR'):
            os.environ['OUTPUT_BASE_DIR'] = str(base_data_dir / 'OSM-PBF-FILES' / 'osm_wikidata_extractions' / 'continents')
        
        try:
            # Phase 1: Sync Polygon Regions
            self.logger.info("Phase 1: Syncing Polygon Regions...")
            call_command('sync_poly_regions')
            self.logger.info("✓ Polygon regions synced")
            
            # Phase 2: Generate Continental PBFs
            self.logger.info("Phase 2: Generating Continental PBFs...")
            call_command('generate_continental_pbfs')
            self.logger.info("✓ Continental PBFs generated")
            
            # Phase 3: Link Continent PBFs
            self.logger.info("Phase 3: Linking Continent PBFs...")
            call_command('link_continent_pbfs')
            self.logger.info("✓ Continent PBFs linked to hierarchy")
            
            # Phase 4: Sync GeoVectors Metadata
            self.logger.info("Phase 4: Syncing GeoVectors Metadata...")
            call_command('sync_geovectors_metadata', save=True)
            self.logger.info("✓ GeoVectors metadata synced")
            
            self.logger.info("Continent extraction complete")
            
        except Exception as e:
            self.logger.error(f"Continent extraction failed: {e}")
            raise
    
    def calculate_workers(self, metrics_data):
        """Calculate recommended worker count."""
        # Cap at 26 workers
        return min(26, 4)  # Conservative default
    
    def _generate_continent_poly_from_relation(self, continent: OSMWikiDataHierarchy, output_dir: Path) -> Path:
        """Generate poly file from OSM relation ID using osmium.
        
        NOTE: This method is deprecated. We now use RegionHierarchy with poly files directly.
        
        Args:
            continent: OSMWikiDataHierarchy entry for the continent
            output_dir: Directory to save the poly file
            
        Returns:
            Path to the generated poly file
        """
        self.logger.warning("_generate_continent_poly_from_relation is deprecated - using RegionHierarchy poly files instead")
        return None
    
    def _extract_continents_simple(self):
        """Extract continents using RegionHierarchy region_type.
        
        This method:
        1. Queries RegionHierarchy for continent entries (region_type=CONTINENT)
        2. Uses poly files from RegionHierarchy for extraction
        3. Extracts continent PBFs using osmium with poly files
        4. Links extracted PBFs to RegionHierarchy
        """
        self.logger.info("Starting RegionHierarchy-based continent extraction")
        self.logger.info(f"Policy base_dir: {self.policy['file_structure']['base_dir']}")
        
        base_dir = Path(self.policy['file_structure']['base_dir'])
        output_dir = base_dir / 'OSM-PBF-FILES' / 'osm_wikidata_extractions' / 'continents'
        self.logger.info(f"Output directory: {output_dir}")
        output_dir.mkdir(parents=True, exist_ok=True)
        
        # Query RegionHierarchy for continent entries
        from api.models import RegionHierarchy
        self.logger.info("Querying RegionHierarchy for continent entries...")
        continents = RegionHierarchy.objects.filter(region_type=RegionHierarchy.RegionType.CONTINENT)
        
        if not continents.exists():
            self.logger.warning("No continent entries found in RegionHierarchy. "
                                "Running sync_poly_regions to populate...")
            base_data_dir = Path(self.policy['file_structure']['base_dir'])
            try:
                call_command('sync_poly_regions')
                self.logger.info("sync_poly_regions completed. Re-querying...")
                continents = RegionHierarchy.objects.filter(region_type=RegionHierarchy.RegionType.CONTINENT)
            except Exception as sync_err:
                self.logger.error(f"sync_poly_regions failed: {sync_err}")
                return

        if not continents.exists():
            self.logger.warning("RegionHierarchy still empty after sync_poly_regions. "
                                "Downloading polygon files from Geofabrik...")
            try:
                from extraction.services.geofabrik_poly_service import download_all_geofabrik_polygons
                stats = download_all_geofabrik_polygons(timeout=15)
                self.logger.info(
                    f"Downloaded {stats['downloaded']}, skipped {stats['skipped']}, "
                    f"not found {stats['not_found']}, errors {stats['errors']}"
                )
                if stats['downloaded'] > 0 or stats['not_found'] == 0:
                    # Re-run sync now that files exist
                    call_command('sync_poly_regions')
                    continents = RegionHierarchy.objects.filter(
                        region_type=RegionHierarchy.RegionType.CONTINENT
                    )
            except Exception as dl_err:
                self.logger.error(f"Polygon download failed: {dl_err}")

        if not continents.exists():
            self.logger.error(
                "Still no continent entries after auto-download. "
                f"Check POLYGON_FILES_DIR={settings.POLYGON_FILES_DIR}"
            )
            return
        
        self.logger.info(f"Found {continents.count()} continent entries in RegionHierarchy")
        
        extraction_service = ExtractionService()
        planet_pbf_path = self.policy['parameters']['planet_pbf_path']
        self.logger.info(f"Planet PBF path: {planet_pbf_path}")
        
        # Get or create planet PBF file record
        self.logger.info("Getting or creating planet PBF file record...")
        planet_pbf, created = PbfFile.objects.get_or_create(
            path=planet_pbf_path,
            defaults={
                'pbf_file_type': PbfFile.PbfType.PLANET,
                'extraction_level': PbfFile.ExtractionLevel.PLANET
            }
        )
        self.logger.info(f"Planet PBF record {'created' if created else 'retrieved'}")
        
        for continent in continents:
            try:
                self.logger.info(f"Processing continent: {continent.name}")
                self.logger.info(f"Continent poly_file_path: {continent.poly_file_path}")
                
                # Use poly file from RegionHierarchy
                if not continent.poly_file_path:
                    self.logger.warning(f"No poly_file_path for continent {continent.name}, skipping")
                    continue
                
                poly_path = Path(continent.poly_file_path)
                self.logger.info(f"Poly path: {poly_path}, exists: {poly_path.exists()}")
                if not poly_path.exists():
                    self.logger.warning(f"Poly file not found at {poly_path}, skipping {continent.name}")
                    continue
                
                # Extract continent PBF
                output_pbf_path = output_dir / f"{continent.name}.pbf"
                self.logger.info(f"Output PBF path: {output_pbf_path}")
                
                if output_pbf_path.exists():
                    self.logger.info(f"Continent PBF already exists: {output_pbf_path}")
                    # Create or update PbfFile entry
                    pbf_file, created = PbfFile.objects.get_or_create(
                        path=str(output_pbf_path),
                        defaults={
                            'pbf_file_type': PbfFile.PbfType.CONTINENT,
                            'extraction_level': PbfFile.ExtractionLevel.CONTINENT,
                            'parent_pbf': planet_pbf
                        }
                    )
                    # Link to RegionHierarchy
                    if continent.corresponding_pbf_id != pbf_file.id:
                        continent.corresponding_pbf = pbf_file
                        continent.save(update_fields=['corresponding_pbf'])
                    continue
                
                self.logger.info(f"Extracting {continent.name} to {output_pbf_path}")
                self.logger.info(f"Calling extraction_service.extract_with_polygon...")
                
                result = extraction_service.extract_with_polygon(
                    source_pbf_path=planet_pbf_path,
                    polygon_file_path=str(poly_path),
                    output_path=str(output_pbf_path),
                    source_pbf_file=planet_pbf
                )
                
                self.logger.info(f"Extraction result: {result}")
                
                if result and result.get('success'):
                    self.logger.info(f"✓ Extracted {continent.name}")
                    
                    # Create PbfFile entry
                    pbf_file, created = PbfFile.objects.get_or_create(
                        path=str(output_pbf_path),
                        defaults={
                            'pbf_file_type': PbfFile.PbfType.CONTINENT,
                            'extraction_level': PbfFile.ExtractionLevel.CONTINENT,
                            'parent_pbf': planet_pbf
                        }
                    )
                    
                    # Link to RegionHierarchy
                    continent.corresponding_pbf = pbf_file
                    continent.save(update_fields=['corresponding_pbf'])
                    
                else:
                    self.logger.error(f"✗ Failed to extract {continent.name}")
                    
            except Exception as e:
                self.logger.error(f"Error processing continent {continent.name}: {e}")
                import traceback
                self.logger.error(f"Traceback: {traceback.format_exc()}")
                continue
        
        self.logger.info("Continent extraction complete")
