from django.core.management.base import BaseCommand

class Command(BaseCommand):
    help = 'Initialize planet file structure, Wikidata alignment, and OSMWikiData primitives'
    
    def handle(self, *args, **options):
        from extraction.models import OSMWikiDataHierarchy
        # Get or create planet-level OSMWikiDataHierarchy
        planet_hierarchy, _ = OSMWikiDataHierarchy.objects.get_or_create(
            slug='planet',
            defaults={
                'name': 'Planet',
                'admin_level': None,
                'processing_policy': OSMWikiDataHierarchy(admin_level=None).get_default_policy()
            }
        )
        
        # Execute planet initialization
        from extraction.services.planet_initialization_service import PlanetInitializationService
        service = PlanetInitializationService(planet_hierarchy.processing_policy)
        results = service.execute()
        
        self.stdout.write(self.style.SUCCESS('✓ Planet initialization complete'))
        self.stdout.write(f'  - Planetary metrics: {results["planetary_metrics"].id if results["planetary_metrics"] else "N/A"}')
        self.stdout.write(f'  - OSMWikiData hierarchy: {results["osm_wikidata_hierarchy"].id}')
