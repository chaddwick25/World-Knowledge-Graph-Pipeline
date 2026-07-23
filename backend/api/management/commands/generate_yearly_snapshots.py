import os
import sys
from pathlib import Path
from datetime import datetime, timedelta
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.conf import settings
from api.models import PbfFile, PbfExtract, TemporalSnapshot, ProcessingSession, Task, OsmiumDatasetMetrics
import logging

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Generate yearly continental snapshots from continental PBF files'

    def add_arguments(self, parser):
        parser.add_argument(
            '--start-year',
            type=int,
            help='Starting year for snapshot generation (auto-detected if not specified)',
        )
        parser.add_argument(
            '--end-year',
            type=int,
            help='Ending year for snapshot generation (auto-detected if not specified)',
        )
        parser.add_argument(
            '--auto-detect-range',
            action='store_true',
            help='Automatically detect year range from PBF temporal coverage',
        )
        parser.add_argument(
            '--continent',
            type=str,
            help='Process only specific continent (e.g., africa, asia, europe)',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be processed without actually creating snapshots',
        )
        parser.add_argument(
            '--force',
            action='store_true',
            help='Force regeneration even if yearly snapshots already exist',
        )
        parser.add_argument(
            '--snapshot-date',
            type=str,
            default='12-31',
            help='Date within year for snapshot (MM-DD format, default: 12-31)',
        )

    def handle(self, *args, **options):
        self.start_year = options.get('start_year')
        self.end_year = options.get('end_year')
        self.auto_detect_range = options['auto_detect_range']
        self.continent_filter = options.get('continent')
        self.dry_run = options['dry_run']
        self.force = options['force']
        self.snapshot_date = options['snapshot_date']
        
        # Validate environment variables
        self.validate_environment()
        
        # Validate date format
        try:
            datetime.strptime(self.snapshot_date, '%m-%d')
        except ValueError:
            raise CommandError(f"Invalid snapshot date format: {self.snapshot_date}. Use MM-DD format.")
        
        # Get configuration from environment
        continental_pbf_dir = os.getenv('OUTPUT_BASE_DIR')
        
        # Auto-detect or validate year range
        if self.auto_detect_range or not self.start_year or not self.end_year:
            detected_range = self.detect_temporal_range(continental_pbf_dir)
            if detected_range:
                if not self.start_year:
                    self.start_year = detected_range['start_year']
                if not self.end_year:
                    self.end_year = detected_range['end_year']
                self.stdout.write(f"Auto-detected temporal range: {self.start_year} - {self.end_year}")
            else:
                if not self.start_year or not self.end_year:
                    raise CommandError(
                        "Could not auto-detect temporal range. Please specify --start-year and --end-year, "
                        "or run 'python manage.py analyze_pbf_temporal_coverage' first."
                    )
        
        self.stdout.write("=== Yearly Continental Snapshots Generator ===")
        self.stdout.write(f"Configuration:")
        self.stdout.write(f"  Continental PBF directory: {continental_pbf_dir}")
        self.stdout.write(f"  Year range: {self.start_year} - {self.end_year}")
        self.stdout.write(f"  Snapshot date: {self.snapshot_date}")
        self.stdout.write(f"  Dry run: {self.dry_run}")
        self.stdout.write(f"  Force regeneration: {self.force}")
        if self.continent_filter:
            self.stdout.write(f"  Continent filter: {self.continent_filter}")
        self.stdout.write()
        
        # Validate year range
        if self.start_year > self.end_year:
            raise CommandError("Start year cannot be greater than end year")
        
        if self.end_year > datetime.now().year:
            raise CommandError(f"End year cannot be greater than current year ({datetime.now().year})")
        
        # Create processing session
        session = self.create_processing_session()
        
        try:
            # Find continental PBF files
            continental_pbfs = self.find_continental_pbfs(continental_pbf_dir)
            
            if self.continent_filter:
                continental_pbfs = {
                    k: v for k, v in continental_pbfs.items() 
                    if k.lower() == self.continent_filter.lower()
                }
                if not continental_pbfs:
                    raise CommandError(f"No continental PBF found for '{self.continent_filter}'")
            
            if not continental_pbfs:
                raise CommandError(f"No continental PBF files found in {continental_pbf_dir}")
            
            self.stdout.write(f"Found {len(continental_pbfs)} continental PBF files:")
            for continent, pbf_files in continental_pbfs.items():
                self.stdout.write(f"  {continent}: {len(pbf_files)} PBF files")
            self.stdout.write()
            
            # Check existing snapshots
            if not self.force:
                existing_snapshots = self.check_existing_snapshots()
                if existing_snapshots:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Existing snapshots found for {len(existing_snapshots)} continent-year combinations"
                        )
                    )
                    if not self.dry_run:
                        self.stdout.write("Use --force to regenerate existing snapshots")
            
            if self.dry_run:
                self.show_dry_run_summary(continental_pbfs)
                return
            
            # Process each continent and year
            total_snapshots = 0
            total_failed = 0
            
            for continent, pbf_files in continental_pbfs.items():
                self.stdout.write(f"Processing continent: {continent}")
                
                for year in range(self.start_year, self.end_year + 1):
                    snapshot_timestamp = datetime(year, 12, 31, 23, 59, 59)
                    
                    try:
                        result = self.process_yearly_snapshots(
                            continent=continent,
                            pbf_files=pbf_files,
                            year=year,
                            snapshot_timestamp=snapshot_timestamp,
                            session=session
                        )
                        
                        if result['success']:
                            total_snapshots += result['snapshots_created']
                            self.stdout.write(
                                self.style.SUCCESS(
                                    f"  ✓ {year}: {result['snapshots_created']} snapshots created"
                                )
                            )
                        else:
                            total_failed += 1
                            self.stdout.write(
                                self.style.ERROR(f"  ✗ {year}: {result['error']}")
                            )
                            
                    except Exception as e:
                        total_failed += 1
                        logger.error(f"Error processing {continent} {year}: {e}")
                        self.stdout.write(
                            self.style.ERROR(f"  ✗ {year}: Unexpected error - {e}")
                        )
            
            # Update session results
            session.results = {
                'total_snapshots_created': total_snapshots,
                'total_failed': total_failed,
                'year_range': f"{self.start_year}-{self.end_year}",
                'continents_processed': list(continental_pbfs.keys()),
                'completed_at': timezone.now().isoformat()
            }
            session.mark_completed()
            
            # Final summary
            self.stdout.write()
            self.stdout.write("=== Processing Summary ===")
            self.stdout.write(f"Snapshots created: {total_snapshots}")
            self.stdout.write(f"Failed operations: {total_failed}")
            self.stdout.write(f"Session ID: {session.id}")
            
            if total_failed == 0:
                self.stdout.write(self.style.SUCCESS("All yearly snapshots generated successfully!"))
            else:
                self.stdout.write(self.style.WARNING("Some operations failed - check logs for details"))
                
        except Exception as e:
            session.status = ProcessingSession.SessionStatus.FAILED
            session.results = {'error': str(e)}
            session.save()
            raise CommandError(f"Yearly snapshot generation failed: {e}")

    def validate_environment(self):
        """Validate required environment variables exist"""
        required_vars = ['OUTPUT_BASE_DIR']
        missing_vars = []
        
        for var in required_vars:
            if not os.getenv(var):
                missing_vars.append(var)
        
        if missing_vars:
            raise CommandError(
                f"Missing required environment variables: {', '.join(missing_vars)}\n"
                "Please set OUTPUT_BASE_DIR to the continental PBF directory."
            )
        
        # Validate path exists
        continental_pbf_dir = os.getenv('OUTPUT_BASE_DIR')
        if not os.path.exists(continental_pbf_dir):
            raise CommandError(f"OUTPUT_BASE_DIR does not exist: {continental_pbf_dir}")

    def create_processing_session(self):
        """Create a processing session to track this snapshot generation run"""
        session = ProcessingSession.objects.create(
            session_name=f"Yearly Snapshots {self.start_year}-{self.end_year} - {timezone.now().strftime('%Y%m%d_%H%M%S')}",
            session_type=ProcessingSession.SessionType.TEMPORAL_CORPUS,
            configuration={
                'start_year': self.start_year,
                'end_year': self.end_year,
                'snapshot_date': self.snapshot_date,
                'continent_filter': self.continent_filter,
                'force_regeneration': self.force,
                'command': 'generate_yearly_snapshots'
            }
        )
        return session

    def find_continental_pbfs(self, continental_pbf_dir):
        """Find all continental PBF files organized by continent"""
        continental_pbfs = {}
        base_path = Path(continental_pbf_dir)
        
        # Look for continental directories
        for continent_dir in base_path.iterdir():
            if continent_dir.is_dir():
                continent_name = continent_dir.name
                pbf_files = []
                
                # Find all PBF files in this continent directory
                for pbf_file in continent_dir.rglob('*.pbf'):
                    # Get corresponding database record
                    pbf_record = PbfFile.objects.filter(path__icontains=str(pbf_file)).first()
                    if not pbf_record:
                        # Try to find by PbfExtract output path
                        extract_record = PbfExtract.objects.filter(output_pbf_path=str(pbf_file)).first()
                        if extract_record:
                            pbf_record = extract_record.source_pbf
                    
                    if pbf_record:
                        pbf_files.append({
                            'file_path': pbf_file,
                            'pbf_record': pbf_record,
                            'region_name': pbf_file.stem
                        })
                
                if pbf_files:
                    continental_pbfs[continent_name] = pbf_files
        
        return continental_pbfs

    def detect_temporal_range(self, continental_pbf_dir):
        """Detect temporal range from PBF files with metrics"""
        try:
            # Find continental PBF files
            continental_pbfs = self.find_continental_pbfs(continental_pbf_dir)
            
            if self.continent_filter:
                continental_pbfs = {
                    k: v for k, v in continental_pbfs.items() 
                    if k.lower() == self.continent_filter.lower()
                }
            
            if not continental_pbfs:
                self.stdout.write(self.style.WARNING("No continental PBF files found for temporal analysis"))
                return None
            
            # Collect all PBF files with temporal metrics
            pbf_files_with_metrics = []
            for continent, pbf_files in continental_pbfs.items():
                for pbf_info in pbf_files:
                    pbf_record = pbf_info['pbf_record']
                    
                    # Get temporal metrics for this PBF
                    metrics = OsmiumDatasetMetrics.objects.filter(
                        pbf_file=pbf_record,
                        temporal_coverage_start__isnull=False,
                        temporal_coverage_end__isnull=False
                    ).first()
                    
                    if metrics:
                        pbf_files_with_metrics.append({
                            'pbf_file': pbf_record,
                            'continent': continent,
                            'start_date': metrics.temporal_coverage_start,
                            'end_date': metrics.temporal_coverage_end
                        })
            
            if not pbf_files_with_metrics:
                self.stdout.write(
                    self.style.WARNING(
                        "No PBF files with temporal metrics found. "
                        "Run 'python manage.py analyze_pbf_temporal_coverage' first."
                    )
                )
                return None
            
            # Calculate global temporal range
            all_start_dates = [info['start_date'] for info in pbf_files_with_metrics]
            all_end_dates = [info['end_date'] for info in pbf_files_with_metrics]
            
            global_start = min(all_start_dates)
            global_end = max(all_end_dates)
            
            start_year = global_start.year
            end_year = min(global_end.year, datetime.now().year)
            
            self.stdout.write(f"Detected temporal coverage:")
            self.stdout.write(f"  Global range: {global_start} to {global_end}")
            self.stdout.write(f"  Year range: {start_year} - {end_year}")
            self.stdout.write(f"  Files with metrics: {len(pbf_files_with_metrics)}")
            
            # Show per-continent breakdown
            continent_ranges = {}
            for info in pbf_files_with_metrics:
                continent = info['continent']
                if continent not in continent_ranges:
                    continent_ranges[continent] = {
                        'start_dates': [],
                        'end_dates': [],
                        'count': 0
                    }
                continent_ranges[continent]['start_dates'].append(info['start_date'])
                continent_ranges[continent]['end_dates'].append(info['end_date'])
                continent_ranges[continent]['count'] += 1
            
            self.stdout.write("  Per-continent coverage:")
            for continent, ranges in continent_ranges.items():
                cont_start = min(ranges['start_dates'])
                cont_end = max(ranges['end_dates'])
                self.stdout.write(f"    {continent}: {cont_start} to {cont_end} ({ranges['count']} files)")
            
            return {
                'start_year': start_year,
                'end_year': end_year,
                'global_start_date': global_start,
                'global_end_date': global_end,
                'files_with_metrics': len(pbf_files_with_metrics),
                'continent_ranges': continent_ranges
            }
            
        except Exception as e:
            logger.error(f"Error detecting temporal range: {e}")
            self.stdout.write(self.style.ERROR(f"Error detecting temporal range: {e}"))
            return None

    def check_existing_snapshots(self):
        """Check for existing temporal snapshots in the specified range"""
        existing_snapshots = []
        
        for year in range(self.start_year, self.end_year + 1):
            year_start = datetime(year, 1, 1)
            year_end = datetime(year, 12, 31, 23, 59, 59)
            
            snapshots = TemporalSnapshot.objects.filter(
                timestamp__range=(year_start, year_end)
            )
            
            if self.continent_filter:
                snapshots = snapshots.filter(region__icontains=self.continent_filter)
            
            for snapshot in snapshots:
                existing_snapshots.append({
                    'region': snapshot.region,
                    'year': year,
                    'timestamp': snapshot.timestamp
                })
        
        return existing_snapshots

    def show_dry_run_summary(self, continental_pbfs):
        """Show what would be processed in dry run mode"""
        total_operations = 0
        
        self.stdout.write("=== Dry Run Summary ===")
        
        for continent, pbf_files in continental_pbfs.items():
            continent_operations = 0
            
            for year in range(self.start_year, self.end_year + 1):
                for pbf_info in pbf_files:
                    region_name = pbf_info['region_name']
                    
                    # Check if snapshot already exists
                    snapshot_timestamp = datetime(year, 12, 31, 23, 59, 59)
                    existing = TemporalSnapshot.objects.filter(
                        region=region_name,
                        timestamp=snapshot_timestamp
                    ).exists()
                    
                    if not existing or self.force:
                        continent_operations += 1
                        total_operations += 1
            
            self.stdout.write(f"  {continent}: {continent_operations} snapshots would be created")
        
        self.stdout.write(f"Total operations: {total_operations}")
        self.stdout.write(self.style.SUCCESS("Dry run completed - no snapshots created"))

    def process_yearly_snapshots(self, continent, pbf_files, year, snapshot_timestamp, session):
        """Process yearly snapshots for a continent"""
        try:
            snapshots_created = 0
            failed_count = 0
            
            # Create output directory for this year
            year_output_dir = Path(os.getenv('OUTPUT_BASE_DIR')) / continent / 'snapshots' / str(year)
            year_output_dir.mkdir(parents=True, exist_ok=True)
            
            from extraction.services.osmium_facade import OsmiumFacade
            osmium_facade = OsmiumFacade()
            
            for pbf_info in pbf_files:
                pbf_file_path = pbf_info['file_path']
                pbf_record = pbf_info['pbf_record']
                region_name = pbf_info['region_name']
                
                try:
                    # Check if snapshot already exists
                    existing_snapshot = TemporalSnapshot.objects.filter(
                        region=region_name,
                        timestamp=snapshot_timestamp
                    ).first()
                    
                    if existing_snapshot and not self.force:
                        self.stdout.write(f"    Skipping {region_name} (snapshot exists)")
                        continue
                    
                    # Create output filename
                    output_filename = f"{region_name}_{year}_{self.snapshot_date.replace('-', '')}.pbf"
                    output_path = year_output_dir / output_filename
                    
                    self.stdout.write(f"    Creating snapshot: {region_name} @ {year}")
                    
                    # Use osmium time-filter to create temporal snapshot
                    time_filter_date = f"{year}-{self.snapshot_date}T23:59:59Z"
                    result = osmium_facade.time_filter(
                        str(pbf_file_path),
                        time_filter_date,
                        str(output_path)
                    )
                    
                    if result['exit_code'] == 0:
                        # Get file metrics
                        file_stats = output_path.stat() if output_path.exists() else None
                        
                        # Create or update TemporalSnapshot record
                        if existing_snapshot:
                            existing_snapshot.pbf_file = pbf_record
                            existing_snapshot.save()
                            snapshot = existing_snapshot
                        else:
                            snapshot = TemporalSnapshot.objects.create(
                                region=region_name,
                                timestamp=snapshot_timestamp,
                                pbf_file=pbf_record,
                                node_count=0,  # Will be updated by metrics analysis
                                way_count=0,
                                relation_count=0
                            )
                        
                        snapshots_created += 1
                        self.stdout.write(f"      ✓ Created: {output_filename}")
                        
                    else:
                        failed_count += 1
                        error_msg = result.get('error', 'Unknown osmium error')
                        self.stdout.write(f"      ✗ Failed: {error_msg}")
                        logger.error(f"Osmium time-filter failed for {region_name} {year}: {error_msg}")
                        
                except Exception as e:
                    failed_count += 1
                    logger.error(f"Error creating snapshot for {region_name} {year}: {e}")
                    self.stdout.write(f"      ✗ Error: {e}")
            
            return {
                'success': failed_count == 0 or snapshots_created > 0,
                'snapshots_created': snapshots_created,
                'failed_count': failed_count,
                'total_files': len(pbf_files)
            }
            
        except Exception as e:
            logger.error(f"Error processing yearly snapshots for {continent} {year}: {e}")
            return {
                'success': False,
                'error': str(e),
                'snapshots_created': 0
            }
