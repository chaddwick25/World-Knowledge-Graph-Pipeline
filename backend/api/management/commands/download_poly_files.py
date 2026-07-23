import requests
from pathlib import Path
from typing import Optional
from django.core.management.base import BaseCommand
from django.conf import settings
from extraction.services.geofabrik_index_service import geofabrik_index_service


class Command(BaseCommand):
    help = (
        'Downloads .poly files from Geofabrik for all countries that have '
        'embeddings (GeoVectors TSVs on disk). Uses the Geofabrik index to '
        'resolve the correct URL for each country, ensuring every pipeline-ready '
        'country has a corresponding .poly boundary file.'
    )

    GEOFABRIK_POLY_BASE = 'https://download.geofabrik.de'
    TIMEOUT_PER_FILE = 15

    def add_arguments(self, parser):
        parser.add_argument(
            '--force',
            action='store_true',
            help='Re-download all .poly files even if they already exist',
        )
        parser.add_argument(
            '--index-url',
            type=str,
            default=None,
            help='Override the Geofabrik index URL',
        )

    def handle(self, *args, **options):
        force = options['force']
        index_url = options.get('index_url')

        poly_dir = Path(settings.POLYGON_FILES_DIR)
        poly_dir.mkdir(parents=True, exist_ok=True)

        # ---- 1. Load the Geofabrik index ----
        self.stdout.write('Loading Geofabrik index...')
        data = geofabrik_index_service.fetch_index(force_refresh=False)
        features = data.get('features', [])
        regions_dict: dict = {}
        for feat in features:
            props = feat.get('properties') or {}
            nid = props.get('id')
            if nid:
                regions_dict[nid] = props

        self.stdout.write(f'Geofabrik index contains {len(regions_dict)} regions.')

        # ---- 2. Get all country slugs that need .poly files ----
        from extraction.services.regional_path_service import normalize_country_slug
        from extraction.models import OSMWikiDataHierarchy

        db_entries = list(
            OSMWikiDataHierarchy.objects
            .filter(admin_level=2)
            .values('name', 'slug', 'parent_slug', 'osm_relation_id')
        )

        self.stdout.write(
            f'OSMWikiDataHierarchy has {len(db_entries)} countries that need .poly files.'
        )

        # ---- 3. Build URL from full Geofabrik path ----
        def _build_full_id(node_id: str) -> Optional[str]:
            """Builds the slash-joined Geofabrik path like 'central-america/belize'."""
            parts = []
            current = node_id
            while current:
                node = regions_dict.get(current)
                if not node:
                    break
                parent = node.get('parent')
                parts.insert(0, current)
                if not parent:
                    break
                current = parent
            return '/'.join(parts) if parts else None

        def _find_geofabrik_id(slug: str) -> Optional[str]:
            """Find the Geofabrik node ID for a normalized country slug."""
            normalized = normalize_country_slug(slug)
            for nid, props in regions_dict.items():
                nid_norm = normalize_country_slug(nid)
                name_norm = normalize_country_slug(props.get('name', ''))
                if nid_norm == normalized or name_norm == normalized:
                    return nid
            return None

        # ---- 4. Download missing .poly files ----
        downloaded = 0
        skipped = 0
        not_found = 0
        errors = 0

        for entry in db_entries:
            slug = entry['slug'] or entry['name']
            normalized_slug = normalize_country_slug(slug)

            # Determine expected local path
            parent_slug = entry.get('parent_slug', '')
            if parent_slug:
                cont = normalize_country_slug(parent_slug.split('/')[0])
                local_path = poly_dir / cont / f'{normalized_slug}.poly'
            else:
                local_path = poly_dir / f'{normalized_slug}.poly'

            if local_path.exists() and not force:
                skipped += 1
                continue

            # Find Geofabrik node ID and build URL
            geofabrik_id = _find_geofabrik_id(slug)
            if not geofabrik_id:
                not_found += 1
                self.stdout.write(
                    self.style.WARNING(
                        f'  ✗ {entry["name"]}: not found in Geofabrik index'
                    )
                )
                continue

            full_id = _build_full_id(geofabrik_id)
            if not full_id:
                errors += 1
                self.stdout.write(
                    self.style.ERROR(f'  ✗ {entry["name"]}: could not build Geofabrik path')
                )
                continue

            poly_url = f'{self.GEOFABRIK_POLY_BASE}/{full_id}.poly'

            try:
                local_path.parent.mkdir(parents=True, exist_ok=True)
                resp = requests.get(poly_url, timeout=self.TIMEOUT_PER_FILE)

                if resp.status_code == 200:
                    with local_path.open('wb') as f:
                        f.write(resp.content)
                    downloaded += 1
                    self.stdout.write(
                        f'  ✓ {entry["name"]} ({normalized_slug}.poly)'
                    )
                elif resp.status_code == 404:
                    not_found += 1
                    self.stdout.write(
                        self.style.WARNING(
                            f'  ✗ {entry["name"]}: 404 at {poly_url}'
                        )
                    )
                else:
                    errors += 1
                    self.stdout.write(
                        self.style.WARNING(
                            f'  ✗ {entry["name"]}: HTTP {resp.status_code}'
                        )
                    )
            except requests.exceptions.Timeout:
                errors += 1
                self.stdout.write(
                    self.style.ERROR(f'  ✗ {entry["name"]}: timeout')
                )
            except Exception as e:
                errors += 1
                self.stdout.write(
                    self.style.ERROR(f'  ✗ {entry["name"]}: {e}')
                )

        # ---- 5. Summary ----
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS('=' * 60))
        self.stdout.write(self.style.SUCCESS('Download complete'))
        self.stdout.write(self.style.SUCCESS(f'  Downloaded: {downloaded}'))
        self.stdout.write(self.style.SUCCESS(f'  Skipped (already exist): {skipped}'))
        self.stdout.write(self.style.WARNING(f'  Not found: {not_found}'))
        self.stdout.write(self.style.ERROR(f'  Errors: {errors}'))
        self.stdout.write(self.style.SUCCESS('=' * 60))
