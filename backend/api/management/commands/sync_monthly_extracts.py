"""
Management command to sync monthly extract metadata in database with actual files on disk.
This fixes cases where monthly extracts were generated but the parent PBF wasn't updated.
"""

from django.core.management.base import BaseCommand
from django.utils import timezone
from api.models import PbfFile
import logging

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Sync monthly extract metadata for parent PBF files based on existing monthly extracts'

    def add_arguments(self, parser):
        parser.add_argument(
            '--region',
            type=str,
            help='Specific region to sync (e.g., ukraine). If not provided, syncs all regions.',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be updated without making changes',
        )

    def handle(self, *args, **options):
        region_filter = options.get('region')
        dry_run = options.get('dry_run', False)
        
        if dry_run:
            self.stdout.write(self.style.WARNING('DRY RUN MODE - No changes will be made'))
        
        # Find all parent PBF files (region extracts) that have yearly extracts
        parent_pbfs = PbfFile.objects.filter(
            extraction_level__isnull=True,  # Original region extracts have null extraction_level
            pbf_file_type='REGION'
        )
        
        if region_filter:
            # Filter by region name in path
            parent_pbfs = parent_pbfs.filter(path__icontains=region_filter)
        
        updated_count = 0
        
        for parent_pbf in parent_pbfs:
            # Count monthly extracts for this parent (through yearly extracts)
            monthly_extracts = PbfFile.objects.filter(
                parent_pbf__parent_pbf=parent_pbf,
                extraction_level='REGION_MONTHLY'
            ).order_by('min_timestamp')
            
            monthly_count = monthly_extracts.count()
            
            if monthly_count > 0:
                # Get year range
                first_extract = monthly_extracts.first()
                last_extract = monthly_extracts.last()
                
                if first_extract.min_timestamp and last_extract.max_timestamp:
                    min_year = first_extract.min_timestamp.year
                    max_year = last_extract.max_timestamp.year
                    year_range = f"{min_year}-{max_year}"
                    
                    # Check if update is needed
                    needs_update = (
                        not parent_pbf.monthly_extracts_generated or
                        parent_pbf.monthly_extracts_count != monthly_count or
                        parent_pbf.monthly_extracts_year_range != year_range
                    )
                    
                    if needs_update:
                        self.stdout.write(
                            self.style.SUCCESS(
                                f'\nUpdating {parent_pbf.path}:'
                            )
                        )
                        self.stdout.write(f'  Current state:')
                        self.stdout.write(f'    - monthly_extracts_generated: {parent_pbf.monthly_extracts_generated}')
                        self.stdout.write(f'    - monthly_extracts_count: {parent_pbf.monthly_extracts_count}')
                        self.stdout.write(f'    - monthly_extracts_year_range: {parent_pbf.monthly_extracts_year_range}')
                        
                        self.stdout.write(f'  New state:')
                        self.stdout.write(f'    - monthly_extracts_generated: True')
                        self.stdout.write(f'    - monthly_extracts_count: {monthly_count}')
                        self.stdout.write(f'    - monthly_extracts_year_range: {year_range}')
                        
                        if not dry_run:
                            parent_pbf.monthly_extracts_generated = True
                            parent_pbf.monthly_extracts_completed_at = timezone.now()
                            parent_pbf.monthly_extracts_count = monthly_count
                            parent_pbf.monthly_extracts_year_range = year_range
                            parent_pbf.save()
                            
                            self.stdout.write(
                                self.style.SUCCESS(f'  ✓ Updated successfully')
                            )
                        else:
                            self.stdout.write(
                                self.style.WARNING(f'  (Would update in non-dry-run mode)')
                            )
                        
                        updated_count += 1
                    else:
                        self.stdout.write(
                            self.style.SUCCESS(
                                f'✓ {parent_pbf.path} - Already up to date ({monthly_count} monthly extracts)'
                            )
                        )
            else:
                self.stdout.write(
                    f'- {parent_pbf.path} - No monthly extracts found'
                )
        
        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    f'\nDRY RUN COMPLETE: Would have updated {updated_count} parent PBF file(s)'
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f'\n✓ Successfully updated {updated_count} parent PBF file(s)'
                )
            )
