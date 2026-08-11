from django.core.management.base import BaseCommand
from django.core.management import call_command
from geovectors_encoder.services.geovectors_service import GeoVectorsEncoderService

class Command(BaseCommand):
    help = 'Run GeoVectors Encoder on monthly snapshots for a country'

    def add_arguments(self, parser):
        parser.add_argument('country_name', type=str, help='Name of the country')
        parser.add_argument('--continent', type=str, help='Continent name (optional)')
        parser.add_argument('--jobs', type=int, default=10, help='Number of parallel jobs')
        parser.add_argument(
            '--force',
            action='store_true',
            help='Force re-encoding even if entities with this gv_tags_version already exist in the DB.',
        )
        parser.add_argument(
            '--drop-indexes-during-load',
            action='store_true',
            help='Drop pgvector indexes on semantic_search_osmentity before encoding and recreate them after.',
        )

    def handle(self, *args, **options):
        country_name = options['country_name']
        continent = options.get('continent')
        jobs = options['jobs']
        force = options['force']
        drop_indexes = options['drop_indexes_during_load']

        self.stdout.write(self.style.SUCCESS(f'Starting GeoVectors Encoder for {country_name}...'))

        if drop_indexes:
            self.stdout.write(
                self.style.WARNING(
                    'Dropping osmentity vector indexes before encoding...'
                )
            )
            call_command('drop_osmentity_vector_indexes')

        try:
            service = GeoVectorsEncoderService(n_jobs=jobs)
            session = service.run_for_country(country_name, continent, force=force)
        finally:
            if drop_indexes:
                self.stdout.write(
                    self.style.WARNING(
                        'Recreating osmentity vector indexes after encoding...'
                    )
                )
                call_command('create_osmentity_vector_indexes')

        if session:
            self.stdout.write(self.style.SUCCESS(f'Finished! Session ID: {session.id}'))
        else:
            self.stdout.write(self.style.WARNING('No snapshots found or processing failed.'))
