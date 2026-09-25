from django.core.management.base import BaseCommand
from django.core.management import call_command
from django.conf import settings
from pathlib import Path

class Command(BaseCommand):
    help = 'Runs the continents recipe: sync polygon regions, generate continent PBFs, and link them to the hierarchy.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be done without making changes'
        )
        parser.add_argument(
            '--force',
            action='store_true',
            help='Force regeneration of PBFs even if they exist'
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        force = options['force']

        self.stdout.write(self.style.SUCCESS('= ' * 40))
        self.stdout.write(self.style.SUCCESS('RUNNING CONTINENTS RECIPE'))
        self.stdout.write(self.style.SUCCESS('= ' * 40))
        self.stdout.write('')

        # Phase 1: Sync Polygon Regions
        self.stdout.write(self.style.MIGRATE_HEADING('Phase 1: Syncing Polygon Regions...'))
        call_command('sync_poly_regions')
        self.stdout.write('')

        # Phase 2: Generate Continental PBFs (using RegionHierarchy-based extraction)
        self.stdout.write(self.style.MIGRATE_HEADING('Phase 2: Generating Continental PBFs...'))

        if not getattr(settings, 'FOLDER_PATH', None):
            settings.FOLDER_PATH = settings.POLYGON_FILES_DIR
        if not getattr(settings, 'SOURCE_PBF_PATH', None):
            settings.SOURCE_PBF_PATH = settings.PLANET_OSM_FILE_PATH
        if not getattr(settings, 'OUTPUT_BASE_DIR', None):
            settings.OUTPUT_BASE_DIR = getattr(
                settings,
                'OSM_CONTINENTS_OUTPUT_DIR',
                str(Path(settings.OSM_WIKIDATA_EXTRACTIONS_DIR) / 'continents'),
            )

        # Use planet initialization service for continent extraction
        from core.services.planet_init.planet_initialization_service import PlanetInitializationService
        from core.models import OSMWikiDataHierarchy
        
        # Get default policy
        planet_hierarchy = OSMWikiDataHierarchy(admin_level=None)
        policy = planet_hierarchy.get_default_policy()

        # Debug policy structure and extract_continents flag safely
        self.stdout.write(f"Policy top-level keys: {list(policy.keys())}")
        planet_stage = policy.get('stages', {}).get('planet_initialization')
        if planet_stage:
            extract_flag = planet_stage.get('parameters', {}).get('extract_continents', False)
            self.stdout.write(f"Policy extract_continents parameter: {extract_flag}")
        else:
            self.stdout.write("No 'planet_initialization' stage found in policy; using defaults")
        
        # Create planet initialization service
        planet_service = PlanetInitializationService(policy)
        
        try:
            # Call the new simple continent extraction method
            self.stdout.write(self.style.NOTICE('Calling _extract_continents_simple()...'))
            planet_service._extract_continents_simple()
            self.stdout.write(self.style.SUCCESS('✓ Continental PBFs generated'))
        except Exception as e:
            import traceback
            self.stdout.write(self.style.ERROR(f'Continental PBF generation failed: {e}'))
            self.stdout.write(self.style.ERROR(f'Traceback: {traceback.format_exc()}'))
            if not dry_run:
                raise

        self.stdout.write('')

        # Phase 3: Sync GeoVectors Metadata (Procedural)
        self.stdout.write(self.style.MIGRATE_HEADING('Phase 3: Syncing GeoVectors Metadata...'))
        call_command('sync_geovectors_metadata', save=True)
        self.stdout.write('')

        self.stdout.write(self.style.SUCCESS('= ' * 40))
        self.stdout.write(self.style.SUCCESS('CONTINENTS RECIPE COMPLETE ✓'))
        self.stdout.write(self.style.SUCCESS('= ' * 40))
