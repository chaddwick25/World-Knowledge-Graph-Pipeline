"""
Scan EMBEDDINGS_ROOT for the specific countries/regions we care about.

For each snapshot, checks whether embeddings exist for the countries we
want to show on the map and process through the init pipeline.

Currently targeted:
  - Scotland, England, Wales (split from great-britain-location TSV)
  - United States (needs merging from shards: us-midwest, us-northeast, etc.)

Usage:
    python manage.py scan_embeddings
    python manage.py scan_embeddings --dry-run
    python manage.py scan_embeddings --clear
"""

import logging
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

logger = logging.getLogger(__name__)


# ── The targets we care about ─────────────────────────────────────────

SPLIT_TARGETS = {
    # Great Britain split
    'Scotland': {'slug': 'scotland', 'continent': 'europe', 'source_tsv': 'great-britain-location',
                 'source_dir': 'great-britain-location'},
    'England':  {'slug': 'england',  'continent': 'europe', 'source_tsv': 'great-britain-location',
                 'source_dir': 'great-britain-location'},
    'Wales':    {'slug': 'wales',    'continent': 'europe', 'source_tsv': 'great-britain-location',
                 'source_dir': 'great-britain-location'},
    # Malaysia/Singapore/Brunei split (multi-country TSV inside asia-location/)
    'Malaysia': {'slug': 'malaysia', 'continent': 'asia', 'source_tsv': 'malaysia-singapore-brunei-location',
                 'source_dir': 'asia-location'},
    'Singapore': {'slug': 'singapore', 'continent': 'asia', 'source_tsv': 'malaysia-singapore-brunei-location',
                  'source_dir': 'asia-location'},
    'Brunei':   {'slug': 'brunei',   'continent': 'asia', 'source_tsv': 'malaysia-singapore-brunei-location',
                 'source_dir': 'asia-location'},
}

MERGE_TARGETS = {
    # Deferred: United States (needs DB sharding & pipeline test first)
}


class Command(BaseCommand):
    help = 'Scan EMBEDDINGS_ROOT for target country embedding availability'

    def add_arguments(self, parser):
        parser.add_argument(
            '--snapshot-date',
            default='2025_12_31',
            help='Snapshot date in YYYY_MM_DD format (default: 2025_12_31)',
        )
        parser.add_argument(
            '--embeddings-root',
            type=str,
            default=None,
            help='Override EMBEDDINGS_ROOT path',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Print what would be done without writing to DB',
        )
        parser.add_argument(
            '--clear',
            action='store_true',
            help='Clear existing EligibleCountry rows for this snapshot before scanning',
        )

    def handle(self, *args, **options):
        snapshot_date = options['snapshot_date']
        dry_run = options['dry_run']
        clear = options['clear']

        if options['embeddings_root']:
            emb_root = Path(options['embeddings_root'])
        else:
            emb_root = Path(settings.EMBEDDINGS_ROOT)

        if not emb_root.exists():
            raise CommandError(f'EMBEDDINGS_ROOT does not exist: {emb_root}')

        self.stdout.write('=' * 60)
        self.stdout.write(f'Scanning embeddings in: {emb_root}')
        self.stdout.write(f'Snapshot: {snapshot_date}  Dry run: {dry_run}')
        self.stdout.write('=' * 60)

        from orchestration.models import EligibleCountry

        if clear and not dry_run:
            deleted, _ = EligibleCountry.objects.filter(
                snapshot_date=snapshot_date,
            ).delete()
            self.stdout.write(f'Cleared {deleted} existing row(s) for {snapshot_date}')

        # ── Check each target country ──────────────────────────────
        results = []

        for name, info in SPLIT_TARGETS.items():
            country_dir = emb_root / info['continent'] / info['slug']
            location_path = country_dir / f"{info['slug']}-location.tsv.gz"
            tags_path = country_dir / f"{info['slug']}-tags.tsv.gz"
            canonical_location = country_dir / 'location.tsv.gz'
            canonical_tags = country_dir / 'tags.tsv.gz'

            has_location = any(p.exists() for p in [canonical_location, location_path])
            has_tags = any(p.exists() for p in [canonical_tags, tags_path])

            if has_location or has_tags:
                status = EligibleCountry.EmbeddingStatus.READY
            else:
                source_dir = emb_root / info['continent'] / info['source_dir']
                source_tsv = source_dir / f'{info["source_tsv"]}.tsv.gz'
                if source_tsv.exists():
                    status = EligibleCountry.EmbeddingStatus.NEEDS_SPLIT
                else:
                    status = EligibleCountry.EmbeddingStatus.NO_EMBEDDINGS

            results.append({
                'country_name': name,
                'continent': info['continent'],
                'embedding_status': status,
                'location_tsv_path': (
                    str(canonical_location if canonical_location.exists() else location_path)
                    if has_location else None
                ),
                'tags_tsv_path': (
                    str(canonical_tags if canonical_tags.exists() else tags_path)
                    if has_tags else None
                ),
            })

        for name, info in MERGE_TARGETS.items():
            continent_dir = emb_root / info['continent']
            has_shards = any((continent_dir / s).is_dir() for s in info['shards'])

            target_dir = continent_dir / info['slug']
            canonical_location = target_dir / 'location.tsv.gz'
            canonical_tags = target_dir / 'tags.tsv.gz'
            location_path = target_dir / f"{info['slug']}-location.tsv.gz"
            tags_path = target_dir / f"{info['slug']}-tags.tsv.gz"

            has_location = any(p.exists() for p in [canonical_location, location_path])
            has_tags = any(p.exists() for p in [canonical_tags, tags_path])

            if has_location or has_tags:
                status = EligibleCountry.EmbeddingStatus.READY
            elif has_shards:
                status = EligibleCountry.EmbeddingStatus.NEEDS_MERGE
            else:
                status = EligibleCountry.EmbeddingStatus.NO_EMBEDDINGS

            results.append({
                'country_name': name,
                'continent': info['continent'],
                'embedding_status': status,
                'location_tsv_path': (
                    str(canonical_location if canonical_location.exists() else location_path)
                    if has_location else None
                ),
                'tags_tsv_path': (
                    str(canonical_tags if canonical_tags.exists() else tags_path)
                    if has_tags else None
                ),
            })

        # ── Report ─────────────────────────────────────────────────
        self.stdout.write(f'\n{"─" * 50}')
        status_counts = {}
        for r in results:
            s = r['embedding_status']
            status_counts[s] = status_counts.get(s, 0) + 1
            detail = ''
            if r.get('location_tsv_path'):
                detail += ' ✓ location'
            if r.get('tags_tsv_path'):
                detail += ' ✓ tags'
            self.stdout.write(f'  {r["country_name"]:20s} → {s:20s}{detail}')

        self.stdout.write(f'\n{"─" * 50}')
        for s, c in sorted(status_counts.items()):
            self.stdout.write(f'  {s}: {c}')
        self.stdout.write(f'  Total: {len(results)}')

        if dry_run:
            self.stdout.write('\n[DRY-RUN] No rows written.')
            return

        # ── Persist ────────────────────────────────────────────────
        created = 0
        updated = 0
        for r in results:
            # Resolve ISO code: try non-sovereign synthetic ISO first,
            # then fall back to existing iso_code from DB (if any).
            from extraction.services.non_sovereign_territories import (
                resolve_non_sovereign_iso,
            )
            existing_entry = EligibleCountry.objects.filter(
                country_name__iexact=r['country_name'],
                snapshot_date=snapshot_date,
            ).first()
            existing_iso = existing_entry.iso_code if existing_entry else None
            iso_code = (
                resolve_non_sovereign_iso(r['country_name'])
                or existing_iso
                or None
            )

            defaults = {
                'country_name': r['country_name'],
                'iso_code': iso_code,
                'continent': r['continent'],
                'snapshot_date': snapshot_date,
                'embedding_status': r['embedding_status'],
                'location_tsv_path': r.get('location_tsv_path'),
                'tags_tsv_path': r.get('tags_tsv_path'),
            }

            if r['country_name'] in SPLIT_TARGETS and r['embedding_status'] == EligibleCountry.EmbeddingStatus.NEEDS_SPLIT:
                defaults['source_tsv_name'] = SPLIT_TARGETS[r['country_name']]['source_tsv']
                defaults['source_continent'] = SPLIT_TARGETS[r['country_name']]['continent']

            obj, is_new = EligibleCountry.objects.update_or_create(
                country_name__iexact=r['country_name'],
                snapshot_date=snapshot_date,
                defaults=defaults,
            )
            if is_new:
                created += 1
            else:
                updated += 1

        self.stdout.write(f'\nDB results: {created} created, {updated} updated')
        self.stdout.write('Done.')
