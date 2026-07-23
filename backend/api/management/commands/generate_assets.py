from django.core.management.base import BaseCommand, CommandError

class Command(BaseCommand):
    help = 'Generates a full asset bundle (nodes, edges, tags) from a source PBF file.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--pbf-id', type=str, required=True,
            help='The database ID of the PbfFile to process.'
        )

    def handle(self, *args, **options):
        pbf_id = options['pbf_id']

        self.stdout.write(self.style.SUCCESS(f'Starting asset generation for PBF file ID: {pbf_id}'))

        try:
            from extraction.services.asset_generation_service import AssetGenerationService
            service = AssetGenerationService(pbf_file_id=pbf_id)
            asset_bundle = service.generate_assets()
            self.stdout.write(self.style.SUCCESS(
                f'Successfully completed asset generation!\n'
                f'Bundle ID: {asset_bundle.id}\n'
                f'Directory: {asset_bundle.asset_directory}'
            ))
        except (ValueError, RuntimeError) as e:
            raise CommandError(f'Asset generation failed: {e}')
