from django.core.management.base import BaseCommand

from geodata.models import DataSource, GeoDataset


class Command(BaseCommand):
    help = "List registered geodata sources (optionally with their datasets)."

    def add_arguments(self, parser):
        parser.add_argument(
            '--source',
            help='Only list a specific source',
        )
        parser.add_argument(
            '--list-datasets',
            action='store_true',
            help='List datasets for each source',
        )

    def handle(self, *args, **options):
        sources = DataSource.objects.all()
        if options['source']:
            sources = sources.filter(name=options['source'])

        if not sources.exists():
            self.stdout.write(self.style.WARNING("No geodata sources registered."))
            return

        for source in sources:
            status = 'active' if source.is_active else 'inactive'
            datasets = source.datasets.count()
            self.stdout.write(
                f"{source.name} [{source.adapter_type}] {status} "
                f"({source.country_code or 'global'}) — {datasets} datasets"
            )
            if options['list_datasets']:
                for dataset in GeoDataset.objects.filter(source=source)[:50]:
                    latest = dataset.quality_snapshots.order_by('-ingested_at').first()
                    grade = latest.grade if latest else '-'
                    retired = ' [retired]' if dataset.is_retired else ''
                    self.stdout.write(
                        f"  - {dataset.name}: {dataset.title} ({grade}){retired}"
                    )
