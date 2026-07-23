from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from api.models import PbfFile, OsmiumDatasetMetrics
import logging

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Analyze temporal coverage of PBF files and populate database metrics'

    def add_arguments(self, parser):
        parser.add_argument(
            '--pbf-id',
            type=str,
            help='Analyze specific PBF file by ID',
        )
        parser.add_argument(
            '--pbf-type',
            choices=['HISTORICAL', 'LATEST'],
            help='Analyze only files of specific type',
        )
        parser.add_argument(
            '--force-refresh',
            action='store_true',
            help='Recompute temporal metrics even if they already exist',
        )
        parser.add_argument(
            '--summary-only',
            action='store_true',
            help='Show temporal coverage summary without analyzing files',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be analyzed without processing',
        )

    def handle(self, *args, **options):
        self.pbf_id = options.get('pbf_id')
        self.pbf_type = options.get('pbf_type')
        self.force_refresh = options['force_refresh']
        self.summary_only = options['summary_only']
        self.dry_run = options['dry_run']
        
        from orchestration.services.pbf_temporal_analyzer import PbfTemporalAnalyzer
        analyzer = PbfTemporalAnalyzer()
        
        self.stdout.write("=== PBF Temporal Coverage Analyzer ===")
        
        # Show summary if requested
        if self.summary_only:
            self.show_temporal_summary(analyzer)
            return
        
        # Analyze specific PBF file
        if self.pbf_id:
            self.analyze_single_pbf(analyzer, self.pbf_id)
            return
        
        # Analyze all PBF files
        self.analyze_all_pbf_files(analyzer)

    def show_temporal_summary(self, analyzer):
        """Display temporal coverage summary for existing metrics"""
        self.stdout.write("Temporal Coverage Summary:")
        self.stdout.write("=" * 50)
        
        try:
            summary = analyzer.get_pbf_temporal_summary()
            
            if not summary or summary['total_files_with_temporal_data'] == 0:
                self.stdout.write(self.style.WARNING("No PBF files with temporal data found"))
                self.stdout.write("Run without --summary-only to analyze files")
                return
            
            self.stdout.write(f"Files with temporal data: {summary['total_files_with_temporal_data']}")
            
            if summary['global_start_date'] and summary['global_end_date']:
                self.stdout.write(f"Global temporal range:")
                self.stdout.write(f"  Start: {summary['global_start_date']}")
                self.stdout.write(f"  End: {summary['global_end_date']}")
                
                duration = summary['global_end_date'] - summary['global_start_date']
                self.stdout.write(f"  Duration: {duration.days} days")
            
            self.stdout.write("\nBy file type:")
            for file_type, info in summary['files_by_type'].items():
                self.stdout.write(f"  {file_type}: {info['count']} files")
                if info['start_date'] and info['end_date']:
                    self.stdout.write(f"    Range: {info['start_date']} to {info['end_date']}")
            
            # Show files that need analysis
            total_pbf_files = PbfFile.objects.filter(status=PbfFile.PbfStatus.COMPLETED).count()
            files_without_temporal = total_pbf_files - summary['total_files_with_temporal_data']
            
            if files_without_temporal > 0:
                self.stdout.write(f"\n{files_without_temporal} files need temporal analysis")
                self.stdout.write("Run without --summary-only to analyze them")
                
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Error getting summary: {e}"))

    def analyze_single_pbf(self, analyzer, pbf_id):
        """Analyze temporal coverage for a specific PBF file"""
        try:
            pbf_file = PbfFile.objects.get(id=pbf_id)
        except PbfFile.DoesNotExist:
            raise CommandError(f"PBF file with ID '{pbf_id}' not found")
        
        self.stdout.write(f"Analyzing PBF file: {pbf_file.path}")
        
        if self.dry_run:
            existing_metrics = OsmiumDatasetMetrics.objects.filter(pbf_file=pbf_file).first()
            if existing_metrics and existing_metrics.temporal_coverage_start:
                self.stdout.write("  Has existing temporal metrics")
                if not self.force_refresh:
                    self.stdout.write("  Would skip (use --force-refresh to recompute)")
                else:
                    self.stdout.write("  Would recompute due to --force-refresh")
            else:
                self.stdout.write("  Would analyze temporal coverage")
            return
        
        try:
            result = analyzer.analyze_pbf_temporal_coverage(pbf_file, self.force_refresh)
            
            if result:
                self.stdout.write(self.style.SUCCESS("✓ Analysis completed"))
                self.stdout.write(f"  Start date: {result['start_date']}")
                self.stdout.write(f"  End date: {result['end_date']}")
                if result.get('resolution_days'):
                    self.stdout.write(f"  Resolution: {result['resolution_days']:.1f} days")
                self.stdout.write(f"  Source: {result['source']}")
            else:
                self.stdout.write(self.style.ERROR("✗ Analysis failed"))
                
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"✗ Error: {e}"))

    def analyze_all_pbf_files(self, analyzer):
        """Analyze temporal coverage for all PBF files"""
        # Get files to analyze
        pbf_files = PbfFile.objects.filter(status=PbfFile.PbfStatus.COMPLETED)
        
        if self.pbf_type:
            pbf_files = pbf_files.filter(pbf_file_type=self.pbf_type)
        
        total_files = pbf_files.count()
        
        if total_files == 0:
            self.stdout.write(self.style.WARNING("No PBF files found to analyze"))
            return
        
        self.stdout.write(f"Found {total_files} PBF files to analyze")
        
        if self.pbf_type:
            self.stdout.write(f"Filter: {self.pbf_type} files only")
        
        if self.force_refresh:
            self.stdout.write("Mode: Force refresh (will recompute existing metrics)")
        
        # Show what would be processed in dry run
        if self.dry_run:
            self.show_dry_run_analysis(pbf_files)
            return
        
        # Run analysis
        self.stdout.write("\nStarting temporal analysis...")
        
        try:
            results = analyzer.analyze_all_pbf_files(
                force_refresh=self.force_refresh,
                pbf_type_filter=self.pbf_type
            )
            
            # Display results
            self.stdout.write("\n" + "=" * 50)
            self.stdout.write("Analysis Results:")
            self.stdout.write(f"  Total files: {results['total_files']}")
            self.stdout.write(f"  Successfully analyzed: {results['analyzed']}")
            self.stdout.write(f"  Failed: {results['failed']}")
            self.stdout.write(f"  Skipped: {results['skipped']}")
            
            if results['analyzed'] > 0:
                self.stdout.write(self.style.SUCCESS(f"\n✓ {results['analyzed']} files analyzed successfully"))
                
                # Show sample of temporal ranges
                sample_ranges = list(results['temporal_ranges'].items())[:5]
                if sample_ranges:
                    self.stdout.write("\nSample temporal ranges:")
                    for pbf_id, info in sample_ranges:
                        if info['start_date'] and info['end_date']:
                            self.stdout.write(f"  {info['path']}")
                            self.stdout.write(f"    {info['start_date']} to {info['end_date']}")
                        else:
                            self.stdout.write(f"  {info['path']}: No temporal data")
            
            if results['failed'] > 0:
                self.stdout.write(self.style.ERROR(f"\n✗ {results['failed']} files failed analysis"))
                self.stdout.write("Check logs for detailed error information")
                
        except Exception as e:
            raise CommandError(f"Batch analysis failed: {e}")

    def show_dry_run_analysis(self, pbf_files):
        """Show what would be analyzed in dry run mode"""
        self.stdout.write("\nDry Run Analysis:")
        
        analyze_count = 0
        skip_count = 0
        
        for pbf_file in pbf_files:
            existing_metrics = OsmiumDatasetMetrics.objects.filter(pbf_file=pbf_file).first()
            
            has_temporal = (existing_metrics and 
                          existing_metrics.temporal_coverage_start and 
                          existing_metrics.temporal_coverage_end)
            
            if has_temporal and not self.force_refresh:
                skip_count += 1
                status = "SKIP (has metrics)"
            else:
                analyze_count += 1
                if has_temporal:
                    status = "ANALYZE (force refresh)"
                else:
                    status = "ANALYZE (no metrics)"
            
            self.stdout.write(f"  {pbf_file.path}: {status}")
        
        self.stdout.write(f"\nSummary:")
        self.stdout.write(f"  Would analyze: {analyze_count}")
        self.stdout.write(f"  Would skip: {skip_count}")
        self.stdout.write(f"  Total: {analyze_count + skip_count}")
        
        if analyze_count > 0:
            self.stdout.write(f"\nRun without --dry-run to process {analyze_count} files")
