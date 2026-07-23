import json
from pathlib import Path
from django.core.management.base import BaseCommand
from django.conf import settings
from extraction.models import OSMWikiDataHierarchy, RegionHierarchy

class Command(BaseCommand):
    help = 'Import country relations into OSMWikiDataHierarchy model (direct from SPARQL+Geofabrik, no JSON intermediary)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be done without making changes'
        )
        parser.add_argument(
            '--update',
            action='store_true',
            help='Update existing records instead of skipping'
        )
        parser.add_argument(
            '--json',
            type=str,
            default=None,
            help='Path to country_relations.json (legacy mode; default: resolve directly)',
        )
        parser.add_argument(
            '--save-json',
            action='store_true',
            help='Save resolved data to country_relations.json for debugging',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        update = options['update']
        json_path_override = options.get('json')
        save_json = options.get('save_json', False)

        if json_path_override:
            # Legacy mode: read from a JSON file
            json_path = Path(json_path_override)
            if not json_path.exists():
                self.stdout.write(self.style.ERROR(f'File not found: {json_path}'))
                return
            with open(json_path) as f:
                data = json.load(f)
            self.stdout.write(f'Loaded {len(data)} countries from {json_path}')
        else:
            # Direct mode: resolve from SPARQL + Geofabrik, no JSON file hop
            self.stdout.write('Resolving country relations directly from SPARQL + Geofabrik...')
            from extraction.services.country_relation_resolver import country_relation_resolver

            data = country_relation_resolver.sync(force_refresh=False)
            self.stdout.write(f'Resolved {len(data)} countries')

            if save_json:
                self.stdout.write(
                    f'  Data saved to {country_relation_resolver.output_path}'
                )

        for iso_code, country_data in data.items():
            self.import_country(iso_code, country_data, dry_run, update)

        self.stdout.write(self.style.SUCCESS('✓ Country relations import complete'))
    
    def import_country(self, iso_code, country_data, dry_run, update):
        name = country_data.get('name')
        slug = country_data.get('slug')
        relation_id = country_data.get('relation_id')
        parent_slug = country_data.get('parent_slug')
        continent_name = country_data.get('continent_name')
        continent_id = country_data.get('continent_id')
        pbf_url = country_data.get('pbf_url')
        wkg_uri = country_data.get('wkg_uri')
        
        # Extract wikidata_id from URI
        wikidata_id = wkg_uri.split('/')[-1] if wkg_uri else None
        
        # Check if exists
        hierarchy = OSMWikiDataHierarchy.objects.filter(slug=slug).first()
        
        if hierarchy:
            if update and not dry_run:
                hierarchy.osm_relation_id = relation_id
                hierarchy.wikidata_uri = wkg_uri
                hierarchy.wikidata_id = wikidata_id
                hierarchy.parent_slug = parent_slug
                hierarchy.continent_name = continent_name
                hierarchy.continent_id = continent_id
                hierarchy.pbf_url = pbf_url
                hierarchy.save()
                self.stdout.write(f'  ✓ Updated: {name} ({iso_code})')
            else:
                self.stdout.write(f'  ⊘ Skipped: {name} ({iso_code})')
        else:
            if not dry_run:
                hierarchy = OSMWikiDataHierarchy.objects.create(
                    name=name,
                    slug=slug,
                    osm_relation_id=relation_id,
                    wikidata_uri=wkg_uri,
                    wikidata_id=wikidata_id,
                    parent_slug=parent_slug,
                    continent_name=continent_name,
                    continent_id=continent_id,
                    pbf_url=pbf_url,
                    admin_level=2  # Countries are admin_level 2
                )
                
                # Link to RegionHierarchy if exists
                region = RegionHierarchy.objects.filter(name__iexact=name).first()
                if region:
                    hierarchy.region_hierarchy = region
                    hierarchy.save()
                
                self.stdout.write(f'  + Created: {name} ({iso_code})')
