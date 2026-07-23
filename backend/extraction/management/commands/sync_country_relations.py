from django.core.management.base import BaseCommand
from extraction.services.country_relation_resolver import country_relation_resolver

class Command(BaseCommand):
    help = 'Synchronizes Country Relation IDs from WorldKG SPARQL and aligns them with Geofabrik Index.'

    def add_arguments(self, parser):
        parser.add_argument('--force', action='store_true', help='Force refresh of Geofabrik index')

    def handle(self, *args, **options):
        self.stdout.write("Starting Country Relation synchronization...")
        
        results = country_relation_resolver.sync(force_refresh=options['force'])
        
        self.stdout.write(self.style.SUCCESS(
            f"Successfully synchronized {len(results)} countries."
        ))
        for iso, data in list(results.items())[:5]:
            self.stdout.write(f"  [{iso}] {data['name']} (Relation: {data['relation_id']}, Slug: {data['slug']})")
        
        if len(results) > 5:
            self.stdout.write(f"  ... and {len(results) - 5} more.")
