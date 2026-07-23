import json
import os
from pathlib import Path
from django.core.management.base import BaseCommand
from django.conf import settings

class Command(BaseCommand):
    help = 'Procedurally scan embeddings directory and update country_relations.json with local TSV paths.'

    def add_arguments(self, parser):
        from django.conf import settings
        default_dir = getattr(settings, 'EMBEDDINGS_ROOT', None)
        if not default_dir:
            default_dir = os.path.join(settings.BASE_DATA_DIR, 'embeddings')
        parser.add_argument('--dir', type=str, default=default_dir,
                            help='Base directory of the embeddings')
        parser.add_argument('--save', action='store_true', help='Actually save changes to JSON')

    def handle(self, *args, **options):
        base_dir = Path(options['dir'])
        # Canonical location (host: backend/data, container: /app/data).
        # BASE_DIR is /app inside the container, so BASE_DIR / 'data' is /app/data.
        json_path = Path(settings.BASE_DIR) / 'data' / 'country_relations.json'
        
        if not json_path.exists():
            self.stdout.write(self.style.ERROR(f"JSON not found at {json_path}"))
            return

        with open(json_path, 'r') as f:
            data = json.load(f)

        count = 0
        self.stdout.write(f"Scanning {base_dir} for embeddings...")

        # 1. Map slugs to multiple codes (Handles shared slugs like Haiti/DomRep)
        slug_to_codes = {}
        for k, v in data.items():
            slug = v.get('slug')
            if slug:
                if slug not in slug_to_codes:
                    slug_to_codes[slug] = []
                slug_to_codes[slug].append(k)

        # 2. Walk the embeddings directory for strict matching
        for root, dirs, files in os.walk(base_dir):
            for file in files:
                if not file.endswith('.tsv.gz'):
                    continue
                
                full_path = os.path.join(root, file)
                
                # Determine type and slug
                if '-location.tsv.gz' in file:
                    slug = file.replace('-location.tsv.gz', '')
                    field = 'geovectors_location_tsv'
                elif '-tags.tsv.gz' in file:
                    slug = file.replace('-tags.tsv.gz', '')
                    field = 'geovectors_tags_tsv'
                else:
                    continue

                # Normalize slug - TSVs use hyphens, JSON slugs now use hyphens too
                codes = slug_to_codes.get(slug)
                if not codes:
                    # Try replacing hyphens with underscores as fallback
                    codes = slug_to_codes.get(slug.replace('-', '_'))
                
                if codes:
                    for code in codes:
                        data[code][field] = full_path
                        self.stdout.write(self.style.SUCCESS(f"Mapped {slug} -> {code} ({field})"))
                        count += 1

        # 3. Apply Procedural Location Overrides (Aliases)
        # Format: { 'target_slug': 'source_slug' }
        LOCATION_OVERRIDES = {
            "poland": "europe-east",
            "us-other": "us-west",
            "haiti-and-domrep": "central-america"
        }

        self.stdout.write("\nApplying Location Overrides...")
        for target_slug, source_slug in LOCATION_OVERRIDES.items():
            target_codes = slug_to_codes.get(target_slug)
            source_codes = slug_to_codes.get(source_slug)
            
            if target_codes and source_codes:
                # Use the first available source code's location
                source_code = source_codes[0]
                source_path = data[source_code].get('geovectors_location_tsv')
                
                if source_path:
                    for t_code in target_codes:
                        # Only override if target doesn't already have its own location
                        if not data[t_code].get('geovectors_location_tsv'):
                            data[t_code]['geovectors_location_tsv'] = source_path
                            self.stdout.write(self.style.SUCCESS(f"Override: {target_slug} -> {source_slug} (location)"))
                            count += 1

        if options['save']:
            with open(json_path, 'w') as f:
                json.dump(data, f, indent=4)
            self.stdout.write(self.style.SUCCESS(f"\nSaved {count} updates to {json_path}"))
        else:
            self.stdout.write(self.style.NOTICE(f"\nDRY RUN: Found {count} mappings/overrides. Run with --save to update JSON."))
