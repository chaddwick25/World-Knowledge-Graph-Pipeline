"""
Management command: prebuild_subgraphs

Generates SubgraphProfile rows from the local Geofabrik index.
Maps country_relations.json (Wikidata-aligned) -> Geofabrik hierarchy
to discover sub-regions (admin regions, provinces, states).

No API calls — purely local JSON processing.

Usage:
    python manage.py prebuild_subgraphs --countries belize,india
    python manage.py prebuild_subgraphs --all
    python manage.py prebuild_subgraphs --dry-run
"""

import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Pre-compute SubgraphProfile rows from Geofabrik index"

    def add_arguments(self, parser):
        parser.add_argument(
            '--countries',
            default=None,
            help='Comma-separated slugs (e.g., "belize,india")',
        )
        parser.add_argument(
            '--all',
            action='store_true',
            help='Process all countries with sub-regions in Geofabrik',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
        )

    def handle(self, *args, **options):
        from orchestration.models import CountryPipelineProfile, SubgraphProfile

        dry_run = options['dry_run']

        # ── Load Geofabrik index ──────────────────────────────────────
        geofabrik_path = Path(settings.BASE_DIR) / 'data' / 'geofabrik_index.json'
        if not geofabrik_path.exists():
            self.stdout.write(self.style.ERROR("geofabrik_index.json not found"))
            return

        with open(geofabrik_path) as f:
            geofabrik = json.load(f)

        # Build parent -> {id: props} map
        geo_children = {}
        geo_names = {}
        for feature in geofabrik.get('features', []):
            props = feature['properties']
            gid = props['id']
            parent = props.get('parent', 'root')
            geo_children.setdefault(parent, {})[gid] = props
            geo_names[gid] = props['name']

        # ── Load country_relations for slug -> Geofabrik mapping ──────
        cr_path = Path(settings.BASE_DIR) / 'data' / 'country_relations.json'
        if not cr_path.exists():
            self.stdout.write(self.style.ERROR("country_relations.json not found"))
            return

        with open(cr_path) as f:
            country_relations = json.load(f)

        # Build slug -> relation data
        cr_by_slug = {}
        for iso, cdata in country_relations.items():
            if isinstance(cdata, dict) and 'slug' in cdata:
                cr_by_slug[cdata['slug']] = {
                    **cdata,
                    '_iso': iso,
                }

        # ── Resolve countries ─────────────────────────────────────────
        if options['countries']:
            slugs = [s.strip() for s in options['countries'].split(',')]
            countries = CountryPipelineProfile.objects.filter(
                canonical_slug__in=slugs
            )
        elif options['all']:
            countries = CountryPipelineProfile.objects.exclude(
                osm_relation_id__isnull=True
            ).order_by('canonical_name')
        else:
            countries = CountryPipelineProfile.objects.filter(
                has_subgraphs=True
            ).order_by('canonical_name')

        if not countries:
            self.stdout.write(self.style.WARNING("No countries matched."))
            return

        total_created = 0
        total_skipped = 0

        for country in countries:
            # Find Geofabrik entry via parent_slug + slug
            cr_entry = cr_by_slug.get(country.canonical_slug)
            if not cr_entry:
                total_skipped += 1
                continue

            parent_slug = cr_entry.get('parent_slug', '')
            geofabrik_id = cr_entry.get('slug', '')

            # Navigate Geofabrik tree: root -> parent_slug -> slug
            if parent_slug and geo_children.get(parent_slug, {}).get(geofabrik_id):
                sub_regions = list(geo_children.get(geofabrik_id, {}).values())
            else:
                sub_regions = list(geo_children.get(geofabrik_id, {}).values())

            if not sub_regions:
                total_skipped += 1
                continue

            self.stdout.write(
                f"  {country.canonical_name}: {len(sub_regions)} sub-regions"
            )

            for region in sub_regions:
                raw_id = region['id']
                slug = raw_id.replace('/', '_')

                defaults = {
                    'name': region['name'],
                    'slug': slug,
                    'continent_name': country.continent_name,
                }

                if not dry_run:
                    SubgraphProfile.objects.update_or_create(
                        country_profile=country,
                        slug=slug,
                        defaults=defaults,
                    )
                total_created += 1

        self.stdout.write(self.style.SUCCESS(
            f"\nDone: {total_created} subgraphs, {total_skipped} skipped"
        ))
        if dry_run:
            self.stdout.write(self.style.WARNING("(dry run — no DB writes)"))
