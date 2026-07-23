"""
Management command to register continent PBF files from disk into the database.

This command scans a directory for continent PBF files and creates PbfFile
records for them with pbf_file_type='CONTINENT'.

Usage:
    python manage.py register_continent_pbfs --directory /path/to/continents [--dry-run]
"""
from django.core.management.base import BaseCommand
from pathlib import Path
from datetime import datetime
from django.utils import timezone


class Command(BaseCommand):
    help = 'Register continent PBF files from disk into the database'

    def add_arguments(self, parser):
        parser.add_argument(
            '--directory',
            type=str,
            required=True,
            help='Directory containing continent PBF files'
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be registered without making changes'
        )
        parser.add_argument(
            '--planet-id',
            type=str,
            help='UUID of planet PBF file to set as parent (optional)'
        )

    def handle(self, *args, **options):
        directory = Path(options['directory'])
        dry_run = options['dry_run']
        planet_id = options.get('planet_id')
        
        self.stdout.write(self.style.SUCCESS('=' * 80))
        self.stdout.write(self.style.SUCCESS('Registering Continent PBF Files'))
        self.stdout.write(self.style.SUCCESS('=' * 80))
        self.stdout.write('')
        
        if dry_run:
            self.stdout.write(self.style.WARNING('DRY RUN MODE - No changes will be made'))
            self.stdout.write('')
        
        if not directory.exists():
            self.stdout.write(self.style.ERROR(f'Directory not found: {directory}'))
            return
        
        # Get planet PBF if ID provided
        from extraction.models import PbfFile
        planet_pbf = None
        if planet_id:
            try:
                planet_pbf = PbfFile.objects.get(id=planet_id)
                self.stdout.write(f'Using planet PBF as parent: {planet_pbf.path}')
                self.stdout.write('')
            except PbfFile.DoesNotExist:
                self.stdout.write(self.style.WARNING(f'Planet PBF not found: {planet_id}'))
                self.stdout.write('Continents will be registered without parent')
                self.stdout.write('')
        
        # Known continent names
        continent_names = [
            'africa', 'asia', 'europe', 'north-america', 'south-america',
            'central-america', 'oceania', 'russia', 'antarctica'
        ]
        
        registered_count = 0
        already_exists_count = 0
        skipped_count = 0
        
        # Scan directory for continent subdirectories
        for item in sorted(directory.iterdir()):
            if not item.is_dir():
                continue
            
            continent_name = item.name.lower()
            
            # Check if this is a known continent
            if continent_name not in continent_names:
                continue
            
            # Look for .pbf or .osm.pbf file in the subdirectory
            pbf_files = list(item.glob('*.pbf')) + list(item.glob('*.osm.pbf'))
            
            if not pbf_files:
                self.stdout.write(
                    self.style.WARNING(
                        f'⚠ {continent_name:20s} → No PBF file found in {item}'
                    )
                )
                skipped_count += 1
                continue
            
            pbf_file = pbf_files[0]  # Use first match
            pbf_path = str(pbf_file.resolve())
            
            # Check if already registered
            existing = PbfFile.objects.filter(path=pbf_path).first()
            if existing:
                self.stdout.write(
                    self.style.SUCCESS(
                        f'✓ {continent_name:20s} → Already registered: {pbf_path}'
                    )
                )
                already_exists_count += 1
                continue
            
            # Get file size
            file_size = pbf_file.stat().st_size
            file_size_gb = file_size / (1024**3)
            
            if dry_run:
                self.stdout.write(
                    self.style.WARNING(
                        f'[DRY RUN] Would register {continent_name:20s} → {pbf_path} ({file_size_gb:.2f} GB)'
                    )
                )
            else:
                # Create PbfFile record
                pbf_record = PbfFile.objects.create(
                    path=pbf_path,
                    pbf_file_type='CONTINENT',
                    extraction_level='CONTINENT',
                    parent_pbf=planet_pbf,
                    status='COMPLETED',
                    has_history=True,
                    size_bytes=file_size,
                    temporal_metadata_source='INHERITED'
                )
                
                self.stdout.write(
                    self.style.SUCCESS(
                        f'✓ Registered {continent_name:20s} → {pbf_path} ({file_size_gb:.2f} GB)'
                    )
                )
            
            registered_count += 1
        
        # Summary
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS('=' * 80))
        self.stdout.write(self.style.SUCCESS('Summary'))
        self.stdout.write(self.style.SUCCESS('=' * 80))
        self.stdout.write(f'Already registered: {already_exists_count}')
        self.stdout.write(f'Newly registered: {registered_count}')
        self.stdout.write(f'Skipped (no PBF): {skipped_count}')
        
        if dry_run:
            self.stdout.write('')
            self.stdout.write(self.style.WARNING('DRY RUN - No changes were made'))
            self.stdout.write('Run without --dry-run to register files')
        elif registered_count > 0:
            self.stdout.write('')
            self.stdout.write(self.style.SUCCESS(f'Successfully registered {registered_count} continents!'))
            self.stdout.write('')
            self.stdout.write('Next step: Run "python manage.py link_continent_pbfs" to link them to RegionHierarchy')
