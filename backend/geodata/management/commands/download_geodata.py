from django.core.management.base import BaseCommand, CommandError

from geodata.models import DataSource
from geodata.services import IngestionService


class Command(BaseCommand):
    help = (
        "Download and ingest all resources for a geodata dataset. "
        "Replaces download_toronto_data."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--source',
            required=True,
            help='DataSource name (e.g. "Toronto Open Data")',
        )
        parser.add_argument(
            '--dataset',
            required=True,
            help='Dataset slug or source_dataset_id (e.g. ttc-routes-and-schedules)',
        )
        parser.add_argument(
            '--force',
            action='store_true',
            help='Force re-download even if not modified',
        )
        parser.add_argument(
            '--no-truncate',
            action='store_true',
            help='Append records instead of truncating existing ones',
        )
        parser.add_argument(
            '--incremental',
            action='store_true',
            help='Incremental mode: only ingest records newer than last ingestion',
        )

    def handle(self, *args, **options):
        source = DataSource.objects.filter(name=options['source']).first()
        if source is None:
            raise CommandError(f"Source not found: {options['source']}")

        incremental = options['incremental']
        truncate = not options['no_truncate']
        if incremental:
            truncate = False
            self.stdout.write(self.style.SUCCESS("Incremental mode enabled"))

        service = IngestionService(source)
        self.stdout.write(self.style.SUCCESS(
            f"Downloading dataset '{options['dataset']}' from {source.name}..."
        ))

        result = service.download_and_ingest(
            options['dataset'],
            force=options['force'],
            incremental=incremental,
            truncate=truncate,
        )

        self.stdout.write("\n" + "=" * 60)
        self.stdout.write(self.style.SUCCESS(f"✓ Downloaded: {result.downloaded}"))
        self.stdout.write(f"⊘ Skipped (up to date): {result.skipped}")
        self.stdout.write(self.style.SUCCESS(f"✓ Records ingested: {result.records_ingested}"))
        if result.errors > 0:
            self.stdout.write(self.style.WARNING(f"✗ Errors: {result.errors}"))
            for error in result.errors_list:
                self.stdout.write(f"  - {error}")
        self.stdout.write("=" * 60)
