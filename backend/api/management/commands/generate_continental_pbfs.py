import os
import sys
import json
from pathlib import Path
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.conf import settings
from django.db.models import Count
from api.models import PbfFile, PbfExtract, ProcessingSession, Task, RegionHierarchy
import logging

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Generate continental PBF files using recursive polygon extraction'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be processed without actually running extractions',
        )
        parser.add_argument(
            '--continent',
            type=str,
            help='Process only specific continent (e.g., africa, asia, europe)',
        )
        parser.add_argument(
            '--max-workers',
            type=int,
            help='Maximum number of worker processes (defaults to E-cores)',
        )
        parser.add_argument(
            '--force',
            action='store_true',
            help='Force regeneration even if continental PBFs already exist',
        )
        parser.add_argument(
            '--sync-only',
            action='store_true',
            help='Only sync the hierarchy and initialize state, do not run extractions',
        )
        parser.add_argument(
            '--country',
            type=str,
            help='Filter by specific country',
        )
        parser.add_argument(
            '--phases',
            type=str,
            default='1,2,3',
            help='Comma-separated list of phases to run (1: Regional, 2: Preprocess, 3: Snapshot)',
        )
        parser.add_argument(
            '--start-year',
            type=int,
            default=2021,
            help='Start year for temporal extracts (defaults to 2021)',
        )

    def handle(self, *args, **options):
        self.dry_run = options['dry_run']
        self.continent_filter = options.get('continent')
        self.max_workers = options.get('max_workers')
        self.force = options['force']
        
        # Validate required environment variables
        self.validate_environment()
        
        # Get configuration from environment
        folder_path = os.path.abspath(os.getenv('FOLDER_PATH') or settings.POLYGON_FILES_DIR)
        source_pbf_path = os.getenv('SOURCE_PBF_PATH')
        output_base_dir = os.getenv('OUTPUT_BASE_DIR')
        
        self.stdout.write("=== WorldKG Temporal Pipeline Orchestrator ===")
        self.stdout.write(f"Configuration:")
        self.stdout.write(f"  Folder path: {folder_path}")
        self.stdout.write(f"  Source PBF: {source_pbf_path}")
        self.stdout.write(f"  Output directory: {output_base_dir}")
        self.stdout.write(f"  Start Year: {options.get('start_year') or 2021}")
        self.stdout.write(f"  Phases: {options.get('phases')}")
        self.stdout.write()

        # Step 1: Sync Hierarchy and Initialize RegionalExtractionState
        self.stdout.write(self.style.SUCCESS("Step 1: Syncing polygon file hierarchy..."))
        from core.services.snapshot.regional_path_service import normalize_continent_slug
        self.sync_hierarchy(Path(folder_path), source_pbf_path)
        
        if options.get('sync_only'):
            self.stdout.write(self.style.SUCCESS("✓ Sync completed. (Sync-only mode)"))
            return

        # Create processing session
        session = self.create_processing_session(source_pbf_path, output_base_dir)
        
        try:
            # Step 2: Determine Phase Configuration
            phases = [int(p) for p in options.get('phases').split(',')]
            start_year = options.get('start_year')
            
            # Get target regions
            from core.models import RegionalExtractionState
            if options.get('country'):
                country_name = options.get('country')
                target_states = RegionalExtractionState.objects.filter(
                    region_name__iexact=country_name, 
                    region_type=RegionalExtractionState.RegionType.COUNTRY
                )
                if not target_states.exists():
                    raise CommandError(f"Country state not found: {country_name}")
            elif self.continent_filter:
                target_states = RegionalExtractionState.objects.filter(
                    region_name__iexact=self.continent_filter,
                    region_type=RegionalExtractionState.RegionType.CONTINENT
                )
                if not target_states.exists():
                    raise CommandError(f"Continent state not found: {self.continent_filter}")
            else:
                target_states = RegionalExtractionState.objects.filter(
                    region_type=RegionalExtractionState.RegionType.CONTINENT,
                    source_planet_path=source_pbf_path
                )

            self.stdout.write(f"Targeting {target_states.count()} regions for temporal expansion.")
            
            # Step 3: Run Pipeline (Continental Only)
            for state in target_states:
                if state.region_type == RegionalExtractionState.RegionType.CONTINENT:
                    self.stdout.write(self.style.SUCCESS(f"Extracting Continent: {state.region_name}"))

                    try:
                        # Direct continent extraction via osmium extract --polygon.
                        # Replaces the legacy temporal_orchestrator.run_pipeline(phases=[1])
                        # call. Uses the continent's .poly file to extract from the planet PBF.
                        from core.services.snapshot.regional_path_service import (
                            regional_path_service,
                            normalize_continent_slug,
                        )
                        cont_slug = normalize_continent_slug(state.region_name)
                        output_pbf = regional_path_service.get_continent_pbf_path(cont_slug)

                        # Continent .poly files live in POLYGON_FILES_DIR
                        poly_dir = Path(getattr(settings, 'POLYGON_FILES_DIR', ''))
                        poly_path = poly_dir / f"{cont_slug}.poly"
                        if not poly_path.exists():
                            # Try alternate naming (e.g., underscores vs hyphens)
                            alt_poly = poly_dir / f"{cont_slug.replace('-', '_')}.poly"
                            if alt_poly.exists():
                                poly_path = alt_poly

                        if not poly_path.exists():
                            self.stdout.write(self.style.ERROR(
                                f"✗ Poly file not found for {cont_slug}: {poly_path}"
                            ))
                            continue

                        Path(output_pbf).parent.mkdir(parents=True, exist_ok=True)
                        if Path(output_pbf).exists() and not self.force:
                            self.stdout.write(f"  ✓ {cont_slug} PBF already exists — skipping")
                            continue

                        import subprocess
                        osmium_path = getattr(settings, 'OSMIUM_BINARY_PATH', 'osmium')
                        cmd = [
                            osmium_path, 'extract',
                            '--with-history',
                            '--strategy=complete_ways',
                            '--overwrite',
                            '-p', str(poly_path),
                            '-o', str(output_pbf),
                            source_pbf_path,
                        ]
                        self.stdout.write(f"  Running: {' '.join(cmd)}")
                        subprocess.run(cmd, check=True)
                        self.stdout.write(self.style.SUCCESS(
                            f"  ✓ Extracted {cont_slug} → {output_pbf}"
                        ))
                    except Exception as e:
                        self.stdout.write(self.style.ERROR(f"✗ Failed to extract {state.region_name}: {e}"))
                        # Continue to next continent
                        continue
                else:
                    self.stdout.write(self.style.WARNING(f"Skipping non-continent region: {state.region_name}. Use GUI for countries."))

            session.status = ProcessingSession.SessionStatus.COMPLETED
            session.save()
            self.stdout.write(self.style.SUCCESS("✓ All requested temporal tasks completed."))

        except Exception as e:
            session.status = ProcessingSession.SessionStatus.FAILED
            session.results = {'error': str(e)}
            session.save()
            logger.error(f"Command execution failed: {e}", exc_info=True)
            raise CommandError(f"Command execution failed: {e}")

    def validate_environment(self):
        """Validate required environment variables exist"""
        required_vars = ['FOLDER_PATH', 'SOURCE_PBF_PATH', 'OUTPUT_BASE_DIR']
        missing_vars = []
        
        for var in required_vars:
            if not os.getenv(var):
                missing_vars.append(var)
        
        if missing_vars:
            raise CommandError(
                f"Missing required environment variables: {', '.join(missing_vars)}\n"
                "Please set these variables before running the command."
            )
        
        # Validate paths exist
        folder_path = os.getenv('FOLDER_PATH')
        source_pbf_path = os.getenv('SOURCE_PBF_PATH')
        
        if not os.path.exists(folder_path):
            raise CommandError(f"FOLDER_PATH does not exist: {folder_path}")
        
        if not os.path.exists(source_pbf_path):
            raise CommandError(f"SOURCE_PBF_PATH does not exist: {source_pbf_path}")

    def check_existing_continental_pbfs(self):
        """Check if continental PBFs already exist in database"""
        continental_names = ['africa', 'asia', 'europe', 'north-america', 'south-america', 'australia', 'antarctica']
        existing_continents = []
        
        for continent in continental_names:
            # Check if PBF extracts exist for this continent
            extracts = PbfExtract.objects.filter(
                output_pbf_path__icontains=continent
            ).exists()
            
            if extracts:
                existing_continents.append(continent)
        
        return existing_continents

    def create_processing_session(self, source_pbf_path, output_base_dir):
        """Create a processing session to track this continental generation run"""
        session = ProcessingSession.objects.create(
            session_name=f"Continental PBF Generation - {timezone.now().strftime('%Y%m%d_%H%M%S')}",
            session_type=ProcessingSession.SessionType.GEOGRAPHIC_EXTRACT,
            configuration={
                'source_pbf_path': source_pbf_path,
                'output_base_dir': output_base_dir,
                'max_workers': self.max_workers,
                'continent_filter': self.continent_filter,
                'force_regeneration': self.force,
                'command': 'generate_continental_pbfs'
            }
        )
        return session

    def sync_hierarchy(self, folder_path, source_planet_path):
        """Modified sync_poly_regions logic integrated into this command"""
        EXCLUDED_DIRS = {'merge'}
        CONTINENT_PRIORITY = [
            'europe', 'africa', 'asia', 'north-america', 'south-america',
            'central-america', 'oceania', 'russia', 'antarctica',
        ]

        def sync_directory(current_path, parent_obj=None):
            from core.models import PolygonFile, RegionalExtractionState
            for item in sorted(current_path.iterdir()):
                if item.is_dir():
                    if item.name in EXCLUDED_DIRS:
                        continue
                    # Normalize continent/region names to use underscores (database convention)
                    normalized_name = normalize_continent_slug(item.name) if parent_obj is None else item.name
                    region_obj, created = RegionHierarchy.objects.get_or_create(
                        name=normalized_name,
                        parent=parent_obj,
                        defaults={'poly_file_path': None}
                    )
                    sync_directory(item, parent_obj=region_obj)

                elif item.is_file() and item.suffix == '.poly':
                    resolved_path = str(item.resolve())
                    region_obj, created = RegionHierarchy.objects.get_or_create(
                        name=item.stem,
                        parent=parent_obj,
                        defaults={'poly_file_path': resolved_path}
                    )
                    
                    # Update path if mismatched
                    if not created and region_obj.poly_file_path != resolved_path:
                        region_obj.poly_file_path = resolved_path
                        region_obj.save(update_fields=['poly_file_path'])

                    # Link PolygonFile
                    poly_file_obj, p_created = PolygonFile.objects.get_or_create(
                        file_path=resolved_path,
                        defaults={'name': item.stem, 'region_name': item.stem}
                    )
                    if region_obj.polygon_file_id != poly_file_obj.id:
                        region_obj.polygon_file = poly_file_obj
                        region_obj.save(update_fields=['polygon_file'])
                    
                    # Initialize RegionalExtractionState
                    # A continent is a top-level dir, a country is a sub-dir or a file in a continent dir.
                    r_type = RegionalExtractionState.RegionType.CONTINENT if parent_obj is None else RegionalExtractionState.RegionType.COUNTRY
                    
                    RegionalExtractionState.objects.get_or_create(
                        source_planet_path=source_planet_path,
                        poly_file=poly_file_obj,
                        defaults={
                            'region_name': item.stem,
                            'region_type': r_type,
                            'status': RegionalExtractionState.StateStatus.PENDING
                        }
                    )

        sync_directory(folder_path)
        
        # Deduplication logic
        continents = RegionHierarchy.objects.filter(parent__isnull=True)
        duplicates = (
            RegionHierarchy.objects
            .filter(parent__in=continents)
            .values('name')
            .annotate(count=Count('id'))
            .filter(count__gt=1)
        )

        for dup in duplicates:
            name = dup['name']
            entries = list(
                RegionHierarchy.objects
                .filter(name=name, parent__in=continents)
                .select_related('parent')
            )

            def sort_key(entry):
                has_path = 0 if entry.poly_file_path else 1
                continent_rank = (
                    CONTINENT_PRIORITY.index(entry.parent.name)
                    if entry.parent and entry.parent.name in CONTINENT_PRIORITY
                    else len(CONTINENT_PRIORITY)
                )
                return (has_path, continent_rank)

            entries.sort(key=sort_key)
            for entry in entries[1:]:
                entry.delete()

    def count_polygon_files(self, directory_path):
        """Count .poly files in a directory recursively"""
        return len(list(Path(directory_path).rglob('*.poly')))

    def process_continent_from_state(self, state, source_pbf_path, output_base_dir, session):
        """Process a single continent using its RegionalExtractionState"""
        from core.models import RegionalExtractionState
        try:
            source_pbf_file = self.get_or_create_source_pbf(source_pbf_path)
            from core.services.snapshot.extraction_service import ExtractionService
            extraction_service = ExtractionService()
            
            continent_output_dir = Path(output_base_dir) / state.region_name
            continent_output_dir.mkdir(parents=True, exist_ok=True)
            
            # Find all country states under this continent by looking at poly_file directory structure
            # Actually, we can just look for countries whose poly_file is in the continent's folder.
            continent_path = Path(state.poly_file.file_path).parent
            country_states = RegionalExtractionState.objects.filter(
                source_planet_path=source_pbf_path,
                region_type=RegionalExtractionState.RegionType.COUNTRY,
                poly_file__file_path__startswith=str(continent_path)
            )
            
            self.stdout.write(f"  Found {country_states.count()} country states in registry.")
            
            processed_count = 0
            failed_count = 0
            
            for country_state in country_states:
                try:
                    poly_file = Path(country_state.poly_file.file_path)
                    relative_path = poly_file.relative_to(continent_path)
                    output_file = continent_output_dir / relative_path.with_suffix('.pbf')
                    output_file.parent.mkdir(parents=True, exist_ok=True)
                    
                    # Check if already completed
                    if not self.force and country_state.status == RegionalExtractionState.StateStatus.COMPLETED:
                        self.stdout.write(f"    Skipping {relative_path} (already completed)")
                        continue
                    
                    self.stdout.write(f"    Processing {relative_path}...")
                    
                    # Update state to EXTRACTING
                    country_state.status = RegionalExtractionState.StateStatus.EXTRACTING
                    country_state.save()
                    
                    # Run extraction
                    result = extraction_service.extract_with_polygon(
                        source_pbf_path=source_pbf_path,
                        polygon_file_path=str(poly_file),
                        output_path=str(output_file),
                        source_pbf_file=source_pbf_file
                    )
                    
                    if result and result.get('success'):
                        processed_count += 1
                        country_state.status = RegionalExtractionState.StateStatus.COMPLETED
                        country_state.pbf_file = PbfFile.objects.filter(path=str(output_file)).first()
                        country_state.metrics = result
                        country_state.last_processed_at = timezone.now()
                        country_state.save()
                        self.stdout.write(f"      ✓ Completed: {output_file.name}")
                    else:
                        failed_count += 1
                        country_state.status = RegionalExtractionState.StateStatus.FAILED
                        country_state.error_message = result.get('error', 'Unknown error') if result else 'No result'
                        country_state.save()
                        self.stdout.write(f"      ✗ Failed: {country_state.error_message}")
                        
                except Exception as e:
                    failed_count += 1
                    logger.error(f"Error processing {country_state.region_name}: {e}")
                    country_state.status = RegionalExtractionState.StateStatus.FAILED
                    country_state.error_message = str(e)
                    country_state.save()
                    self.stdout.write(f"      ✗ Error: {e}")
            
            # Update continent state
            if failed_count == 0:
                state.status = RegionalExtractionState.StateStatus.COMPLETED
            elif processed_count > 0:
                state.status = RegionalExtractionState.StateStatus.EXTRACTING # Partially done
            else:
                state.status = RegionalExtractionState.StateStatus.FAILED
            
            state.last_processed_at = timezone.now()
            state.save()

            return {
                'success': failed_count == 0 or processed_count > 0,
                'processed_count': processed_count,
                'failed_count': failed_count
            }
            
        except Exception as e:
            logger.error(f"Error processing continent state {state.region_name}: {e}")
            return {
                'success': False,
                'error': str(e),
                'processed_count': 0
            }

    def get_or_create_source_pbf(self, source_pbf_path):
        """Get or create PbfFile record for the source PBF"""
        try:
            # Try to find existing PBF file by path
            pbf_file = PbfFile.objects.filter(path=source_pbf_path).first()
            
            if not pbf_file:
                # Create new PBF file record
                pbf_file = PbfFile.objects.create(
                    path=source_pbf_path,
                    pbf_file_type=PbfFile.PbfType.HISTORICAL,
                    status=PbfFile.PbfStatus.COMPLETED,
                    has_history=True,
                    size_bytes=Path(source_pbf_path).stat().st_size if Path(source_pbf_path).exists() else None
                )
                self.stdout.write(f"Created PBF file record: {pbf_file.id}")
            
            return pbf_file
            
        except Exception as e:
            logger.error(f"Error creating PBF file record: {e}")
            raise CommandError(f"Failed to create PBF file record: {e}")
