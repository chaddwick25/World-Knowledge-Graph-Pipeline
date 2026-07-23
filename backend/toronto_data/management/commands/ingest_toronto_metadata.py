from django.core.management.base import BaseCommand
from toronto_data.services.ckan_metadata_service import CkanMetadataService
from toronto_data.services.ckan_qa_service import CkanQaService


class Command(BaseCommand):
    help = "Ingest Toronto Open Data metadata and quality scores (no data download)"

    def add_arguments(self, parser):
        parser.add_argument(
            '--datasets',
            nargs='+',
            help='Specific dataset IDs to ingest (default: all TARGET_DATASETS)',
        )

    def handle(self, *args, **options):
        metadata_service = CkanMetadataService()
        qa_service = CkanQaService()
        
        # Determine which datasets to process
        dataset_ids = options.get('datasets') or metadata_service.TARGET_DATASETS
        
        self.stdout.write(self.style.SUCCESS(
            f"Starting metadata ingestion for {len(dataset_ids)} datasets..."
        ))
        
        # Fetch QA scores first (QA data uses dataset names, not IDs)
        self.stdout.write("Fetching quality scores...")
        qa_records = qa_service.fetch_qa_scores(dataset_ids)
        
        # Process each dataset
        success_count = 0
        error_count = 0
        
        for dataset_id in dataset_ids:
            try:
                self.stdout.write(f"\nProcessing: {dataset_id}")
                
                # Fetch and sync metadata
                metadata = metadata_service.fetch_metadata(dataset_id)
                if not metadata:
                    self.stdout.write(self.style.WARNING(f"  ✗ No data returned for {dataset_id}"))
                    error_count += 1
                    continue
                
                dataset = metadata_service.sync_to_database(metadata)
                self.stdout.write(self.style.SUCCESS(f"  ✓ Synced metadata: {dataset.title}"))
                self.stdout.write(f"    Refresh rate: {dataset.refresh_rate}")
                self.stdout.write(f"    Resources: {dataset.resources.count()}")
                
                success_count += 1
                
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"  ✗ Error processing {dataset_id}: {e}"))
                error_count += 1
        
        # Create QA snapshots
        if qa_records:
            self.stdout.write("\nCreating quality snapshots...")
            snapshots = qa_service.create_snapshots(qa_records)
            self.stdout.write(self.style.SUCCESS(f"Created {len(snapshots)} quality snapshots"))
            
            for snapshot in snapshots:
                self.stdout.write(
                    f"  {snapshot.dataset.title}: {snapshot.grade} "
                    f"({snapshot.quality_score_pct:.1f}%)"
                )
        
        # Summary
        self.stdout.write("\n" + "="*60)
        self.stdout.write(self.style.SUCCESS(f"✓ Successfully processed: {success_count}"))
        if error_count > 0:
            self.stdout.write(self.style.WARNING(f"✗ Errors: {error_count}"))
        self.stdout.write("="*60)
