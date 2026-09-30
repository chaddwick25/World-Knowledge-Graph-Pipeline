from django.core.management.base import BaseCommand, CommandError

from geodata.models import DataSource
from geodata.services import IngestionService, QualityService


class Command(BaseCommand):
    help = (
        "Sync dataset/resource metadata (and quality scores) for a geodata "
        "source. Replaces ingest_toronto_metadata."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--source',
            required=True,
            help='DataSource name (e.g. "Toronto Open Data")',
        )
        parser.add_argument(
            '--datasets',
            nargs='+',
            help='Specific dataset IDs to sync (default: source config target_datasets)',
        )
        parser.add_argument(
            '--skip-qa',
            action='store_true',
            help='Skip quality score sync',
        )

    def handle(self, *args, **options):
        source = DataSource.objects.filter(name=options['source']).first()
        if source is None:
            raise CommandError(f"Source not found: {options['source']}")

        dataset_ids = options.get('datasets') or list(source.config.get('target_datasets', []))
        self.stdout.write(self.style.SUCCESS(
            f"Syncing metadata for {len(dataset_ids)} datasets from {source.name}..."
        ))

        service = IngestionService(source)
        synced = service.sync_metadata(dataset_ids)
        self.stdout.write(self.style.SUCCESS(f"✓ Synced metadata: {synced} datasets"))

        if options['skip_qa']:
            return

        self.stdout.write("Fetching quality scores...")
        quality_service = QualityService(source, service.adapter)
        snapshots = quality_service.sync_quality_snapshots(dataset_ids)
        self.stdout.write(self.style.SUCCESS(f"✓ Synced {len(snapshots)} quality snapshots"))
        for snapshot in snapshots:
            self.stdout.write(
                f"  {snapshot.dataset.title}: {snapshot.grade} "
                f"({snapshot.quality_score_pct:.1f}%)"
            )
