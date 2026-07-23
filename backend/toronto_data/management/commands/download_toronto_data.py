from django.core.management.base import BaseCommand
from toronto_data.models import CkanDataset, CkanResource
from toronto_data.services.dataset_download_service import DatasetDownloadService


class Command(BaseCommand):
    help = "Download and parse Toronto Open Data CSV/JSON files into database tables"

    def add_arguments(self, parser):
        parser.add_argument(
            '--dataset',
            help='Specific dataset ID to download (default: all)',
        )
        parser.add_argument(
            '--force',
            action='store_true',
            help='Force re-download even if not modified',
        )
        parser.add_argument(
            '--no-truncate',
            action='store_true',
            help='Append data instead of truncating existing records',
        )
        parser.add_argument(
            '--incremental',
            action='store_true',
            help='Use incremental mode: only process records newer than last ingestion',
        )

    def handle(self, *args, **options):
        download_service = DatasetDownloadService()
        
        dataset_id = options.get('dataset')
        force = options.get('force', False)
        truncate = not options.get('no_truncate', False)
        incremental = options.get('incremental', False)
        
        # Incremental mode implies no truncate
        if incremental:
            truncate = False
            self.stdout.write(self.style.SUCCESS("🔄 Incremental mode enabled"))
        
        # Get datasets to process
        if dataset_id:
            datasets = CkanDataset.objects.filter(ckan_id=dataset_id)
            if not datasets.exists():
                self.stdout.write(self.style.ERROR(f"Dataset not found: {dataset_id}"))
                return
        else:
            datasets = CkanDataset.objects.all()
        
        self.stdout.write(self.style.SUCCESS(
            f"Starting data download for {datasets.count()} datasets..."
        ))
        
        total_downloaded = 0
        total_skipped = 0
        total_errors = 0
        
        for dataset in datasets:
            self.stdout.write(f"\n{'='*60}")
            self.stdout.write(f"Dataset: {dataset.title}")
            self.stdout.write(f"{'='*60}")
            
            # Get CSV resources for this dataset
            resources = dataset.resources.filter(format__in=['CSV', 'JSON'])
            
            if not resources.exists():
                self.stdout.write(self.style.WARNING("  No CSV/JSON resources found"))
                continue
            
            for resource in resources:
                try:
                    self.stdout.write(f"\nResource: {resource.name} ({resource.format})")
                    
                    # Download file
                    file_path = download_service.download_resource(resource, force=force)
                    
                    if file_path is None:
                        self.stdout.write(self.style.WARNING("  ⊘ Skipped (up to date)"))
                        total_skipped += 1
                        continue
                    
                    self.stdout.write(self.style.SUCCESS(f"  ✓ Downloaded to {file_path}"))
                    total_downloaded += 1
                    
                    # Parse and import
                    mode_str = "incremental" if incremental else "full"
                    self.stdout.write(f"  Parsing and importing data ({mode_str} mode)...")
                    download_service.parse_and_import(resource, file_path, truncate=truncate, incremental=incremental)
                    self.stdout.write(self.style.SUCCESS("  ✓ Import complete"))
                    
                except Exception as e:
                    self.stdout.write(self.style.ERROR(f"  ✗ Error: {e}"))
                    total_errors += 1
                    import traceback
                    self.stdout.write(traceback.format_exc())
        
        # Summary
        self.stdout.write("\n" + "="*60)
        self.stdout.write(self.style.SUCCESS(f"✓ Downloaded: {total_downloaded}"))
        self.stdout.write(f"⊘ Skipped: {total_skipped}")
        if total_errors > 0:
            self.stdout.write(self.style.WARNING(f"✗ Errors: {total_errors}"))
        self.stdout.write("="*60)
