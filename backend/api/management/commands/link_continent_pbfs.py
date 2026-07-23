"""
Management command to link continent PBF files to RegionHierarchy nodes.

This command finds all continent-level RegionHierarchy nodes and links them
to their corresponding PbfFile records via the corresponding_pbf field.

Usage:
    python manage.py link_continent_pbfs [--dry-run]
"""
from django.core.management.base import BaseCommand
from django.db.models import Q
from pathlib import Path
from api.models import RegionHierarchy


class Command(BaseCommand):
    help = 'Link continent PBF files to RegionHierarchy nodes'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be linked without making changes'
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        
        self.stdout.write(self.style.SUCCESS('=' * 80))
        self.stdout.write(self.style.SUCCESS('Linking Continent PBF Files to RegionHierarchy'))
        self.stdout.write(self.style.SUCCESS('=' * 80))
        self.stdout.write('')
        
        if dry_run:
            self.stdout.write(self.style.WARNING('DRY RUN MODE - No changes will be made'))
            self.stdout.write('')
        
        # Get all continent-level nodes (region_type=CONTINENT)
        continents = RegionHierarchy.objects.filter(region_type=RegionHierarchy.RegionType.CONTINENT)
        
        if not continents.exists():
            self.stdout.write(self.style.ERROR('No continent nodes found in RegionHierarchy'))
            self.stdout.write('Run "python manage.py sync_poly_regions" first')
            return
        
        self.stdout.write(f'Found {continents.count()} continent nodes:')
        for continent in continents:
            self.stdout.write(f'  - {continent.name}')
        self.stdout.write('')
        
        # Strategy 0: Proactive Disk Scan & Auto-Registration
        from extraction.models import PbfFile
        from extraction.services.regional_path_service import regional_path_service, normalize_continent_slug
        for continent in continents:
            pbf_path = regional_path_service.get_continent_pbf_path(continent.name)
            if pbf_path.exists():
                pbf_record = PbfFile.objects.filter(path=str(pbf_path)).first()
                if not pbf_record:
                    if not dry_run:
                        self.stdout.write(self.style.WARNING(f'Discovered unregistered file: {pbf_path}. Registering...'))
                        pbf_record = PbfFile.objects.create(
                            path=str(pbf_path),
                            pbf_file_type='REGION',  # Logic expects REGION for standard extracts
                            status='COMPLETED',
                            has_history=True,
                            size_bytes=pbf_path.stat().st_size
                        )
                    else:
                        self.stdout.write(self.style.NOTICE(f'[DRY RUN] Would register: {pbf_path}'))
        
        # Refresh PBF list after potential discovery
        continent_pbfs = PbfFile.objects.filter(
            status='COMPLETED'
        ).filter(Q(pbf_file_type='CONTINENT') | Q(pbf_file_type='REGION'))
        
        self.stdout.write(f'Found {continent_pbfs.count()} registered continent PBF files:')
        for pbf in continent_pbfs:
            self.stdout.write(f'  - {pbf.path}')
        self.stdout.write('')
        
        # Link continents to PBFs
        linked_count = 0
        already_linked_count = 0
        not_found_count = 0
        
        for continent in continents:
            continent_name = normalize_continent_slug(continent.name)  # Standardize to underscores (DB convention)
            
            # Check if already linked
            if continent.corresponding_pbf:
                self.stdout.write(
                    self.style.SUCCESS(
                        f'✓ {continent.name} already linked to {continent.corresponding_pbf.path}'
                    )
                )
                already_linked_count += 1
                continue
            
            # Try to find matching PBF by name
            matching_pbf = None
            
            # Strategy 1: Strict Exact Filename Match (Best)
            for pbf in continent_pbfs:
                pbf_stem = Path(pbf.path).stem.lower()
                if pbf_stem == continent_name or pbf_stem == continent.name.lower():
                    # Ensure this PBF isn't already assigned to someone else
                    if not RegionHierarchy.objects.filter(corresponding_pbf=pbf).exclude(id=continent.id).exists():
                        matching_pbf = pbf
                        break
            
            # Strategy 2: Contained Name Match (Fallback)
            if not matching_pbf:
                for pbf in continent_pbfs:
                    pbf_path_lower = pbf.path.lower()
                    if f"/{continent_name}/" in pbf_path_lower or f"/{continent.name.lower()}/" in pbf_path_lower:
                        # Ensure this PBF isn't already assigned to someone else
                        if not RegionHierarchy.objects.filter(corresponding_pbf=pbf).exclude(id=continent.id).exists():
                            matching_pbf = pbf
                            break
            
            if matching_pbf:
                if dry_run:
                    self.stdout.write(
                        self.style.WARNING(
                            f'[DRY RUN] Would link {continent.name} → {matching_pbf.path}'
                        )
                    )
                else:
                    try:
                        continent.corresponding_pbf = matching_pbf
                        continent.save()
                        self.stdout.write(
                            self.style.SUCCESS(
                                f'✓ Linked {continent.name} → {matching_pbf.path}'
                            )
                        )
                    except Exception as e:
                        self.stdout.write(self.style.ERROR(f'✗ Failed to link {continent.name}: {e}'))
                        continue
                linked_count += 1
            else:
                self.stdout.write(
                    self.style.ERROR(
                        f'✗ No matching PBF found for continent: {continent.name}'
                    )
                )
                self.stdout.write(f'  Tried matching: {continent_name}, {continent.name.lower()}')
                not_found_count += 1
        
        # Summary
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS('=' * 80))
        self.stdout.write(self.style.SUCCESS('Summary'))
        self.stdout.write(self.style.SUCCESS('=' * 80))
        self.stdout.write(f'Already linked: {already_linked_count}')
        self.stdout.write(f'Newly linked: {linked_count}')
        self.stdout.write(f'Not found: {not_found_count}')
        
        if dry_run:
            self.stdout.write('')
            self.stdout.write(self.style.WARNING('DRY RUN - No changes were made'))
            self.stdout.write('Run without --dry-run to apply changes')
        elif linked_count > 0:
            self.stdout.write('')
            self.stdout.write(self.style.SUCCESS(f'Successfully linked {linked_count} continents!'))
        
        if not_found_count > 0:
            self.stdout.write('')
            self.stdout.write(self.style.WARNING('Some continents could not be linked.'))
            self.stdout.write('Make sure continent PBF files are extracted and registered.')
