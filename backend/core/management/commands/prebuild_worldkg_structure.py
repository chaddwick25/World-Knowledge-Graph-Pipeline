"""
Management command: prebuild_worldkg_structure

Computes the WorldKG structural index — the single source of truth
that Celery (the control plane) reads to know what to process.

Phases:
    1. Planet level: osmium fileinfo on planet PBF  → PlanetSnapshot
    2. Continent level: osmium fileinfo on continent PBFs → ContinentProfile
    3. Country level: from country_relations.json + osmium fileinfo on country PBFs → CountryPipelineProfile
    4. Subgraph level: osmium fileinfo on subgraph PBFs → SubgraphProfile

Run once after planet init. Re-runnable (idempotent).
"""

import json
import subprocess
from datetime import date
from pathlib import Path
from typing import Optional

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone


class Command(BaseCommand):
    help = "Pre-compute WorldKG structural index (osmium fileinfo at all levels)"

    def add_arguments(self, parser):
        parser.add_argument(
            '--planet-pbf',
            default=None,
            help='Path to planet .osm.pbf (default: settings.PLANET_OSM_FILE_PATH)',
        )
        parser.add_argument(
            '--skip-osmium',
            action='store_true',
            help='Skip osmium fileinfo calls (just sync DB structure)',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be done without writing to DB',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        skip_osmium = options['skip_osmium']
        planet_pbf = options['planet_pbf'] or getattr(settings, 'PLANET_OSM_FILE_PATH', None)

        if not planet_pbf:
            raise CommandError(
                "Planet PBF path required: pass --planet-pbf or set PLANET_OSM_FILE_PATH"
            )

        osmium = self._resolve_osmium()
        if not skip_osmium and not osmium:
            self.stdout.write(self.style.WARNING(
                "osmium-tool not found. Use --skip-osmium or set OSMIUM_EXECUTABLE."
            ))
            skip_osmium = True

        self.stdout.write(self.style.SUCCESS("=" * 60))
        self.stdout.write(self.style.SUCCESS("WorldKG Structure Pre-Build"))
        self.stdout.write(self.style.SUCCESS("=" * 60))

        # ── Phase 1: Planet ──────────────────────────────────────────────
        self.stdout.write(self.style.MIGRATE_HEADING("\n[1/4] Planet Level"))
        planet_snapshot = self._prebuild_planet(osmium, planet_pbf, skip_osmium, dry_run)

        if not planet_snapshot:
            self.stdout.write(self.style.ERROR("Planet pre-build failed — aborting."))
            return

        # ── Phase 2: Continents ──────────────────────────────────────────
        self.stdout.write(self.style.MIGRATE_HEADING("\n[2/4] Continent Level"))
        continent_count = self._prebuild_continents(
            osmium, planet_snapshot, skip_osmium, dry_run
        )
        self.stdout.write(f"  ✓ {continent_count} continents processed")

        # ── Phase 3: Countries ───────────────────────────────────────────
        self.stdout.write(self.style.MIGRATE_HEADING("\n[3/4] Country Level"))
        country_count = self._prebuild_countries(
            osmium, planet_snapshot, skip_osmium, dry_run
        )
        self.stdout.write(f"  ✓ {country_count} countries processed")

        # ── Phase 4: Subgraphs ───────────────────────────────────────────
        self.stdout.write(self.style.MIGRATE_HEADING("\n[4/4] Subgraph Level"))
        subgraph_count = self._prebuild_subgraphs(osmium, skip_osmium, dry_run)
        self.stdout.write(f"  ✓ {subgraph_count} subgraphs processed")

        self.stdout.write(self.style.SUCCESS("\n" + "=" * 60))
        self.stdout.write(self.style.SUCCESS("WorldKG Structure Pre-Build Complete"))
        self.stdout.write(self.style.SUCCESS("=" * 60))

    # ══════════════════════════════════════════════════════════════════════
    # Phase 1: Planet
    # ══════════════════════════════════════════════════════════════════════

    def _prebuild_planet(self, osmium, planet_pbf, skip_osmium, dry_run):
        """Create/update PlanetSnapshot with osmium fileinfo metrics."""
        from core.models import PlanetSnapshot

        metrics = None
        if not skip_osmium:
            metrics = self._fileinfo(osmium, planet_pbf)
            if metrics:
                self.stdout.write(
                    f"  Planet: {metrics['nodes']:,} nodes, "
                    f"{metrics['ways']:,} ways, "
                    f"{metrics['relations']:,} relations"
                )

        snapshot, created = PlanetSnapshot.objects.get_or_create(
            snapshot_date=date.today(),
            planet_osm_path=planet_pbf,
        )

        if metrics and not dry_run:
            snapshot.node_count = metrics['nodes']
            snapshot.way_count = metrics['ways']
            snapshot.relation_count = metrics['relations']
            snapshot.file_size_bytes = metrics['file_size_bytes']
            snapshot.status = PlanetSnapshot.SnapshotStatus.COMPLETED
            snapshot.completed_at = timezone.now()
            snapshot.save()

        self.stdout.write(f"  {'+' if created else '✓'} PlanetSnapshot: {snapshot.id}")
        return snapshot

    # ══════════════════════════════════════════════════════════════════════
    # Phase 2: Continents
    # ══════════════════════════════════════════════════════════════════════

    def _prebuild_continents(self, osmium, planet_snapshot, skip_osmium, dry_run):
        """Scan continent PBF directory and create/update ContinentProfile rows."""
        from core.models import ContinentProfile

        continent_pbf_dir = Path(settings.BASE_DATA_DIR) / 'OSM-PBF-FILES' / \
            'osm_wikidata_extractions' / 'continents'

        count = 0
        if not continent_pbf_dir.exists():
            self.stdout.write(self.style.WARNING(f"  ⚠ No continent directory: {continent_pbf_dir}"))
            return 0

        for pbf_path in sorted(continent_pbf_dir.glob('*.osm.pbf')):
            slug = pbf_path.stem.replace('.osm', '')

            metrics = None
            if not skip_osmium:
                metrics = self._fileinfo(osmium, str(pbf_path))

            defaults = {
                'name': slug.replace('_', ' ').title(),
                'file_size_bytes': Path(pbf_path).stat().st_size,
            }
            if metrics:
                defaults.update({
                    'node_count': metrics['nodes'],
                    'way_count': metrics['ways'],
                    'relation_count': metrics['relations'],
                    'file_size_bytes': metrics['file_size_bytes'],
                })

            if not dry_run:
                ContinentProfile.objects.update_or_create(
                    planet_snapshot=planet_snapshot,
                    slug=slug,
                    defaults=defaults,
                )

            node_str = f"{metrics['nodes']:,}" if metrics else "?"
            self.stdout.write(f"  + {slug}: {node_str} nodes, {pbf_path.name}")
            count += 1

        return count

    # ══════════════════════════════════════════════════════════════════════
    # Phase 3: Countries
    # ══════════════════════════════════════════════════════════════════════

    def _prebuild_countries(self, osmium, planet_snapshot, skip_osmium, dry_run):
        """
        Sync CountryPipelineProfile from country_relations.json.

        The country_relations.json is the Wikidata-aligned ground truth for
        which countries exist, their ISO codes, slugs, and Wikidata QIDs.
        Osmium fileinfo is run on each country's PBF (if on disk).
        """
        from core.models import (
            ContinentProfile,
            CountryPipelineProfile,
        )

        relations_path = Path(settings.BASE_DATA_DIR) / 'country_relations.json'
        if not relations_path.exists():
            self.stdout.write(self.style.WARNING(
                f"  ⚠ country_relations.json not found at {relations_path}"
            ))
            return 0

        with open(relations_path) as f:
            country_relations = json.load(f)

        # Determine country PBF directory
        country_pbf_dir = Path(settings.BASE_DATA_DIR) / 'OSM-PBF-FILES' / \
            'osm_wikidata_extractions'

        count = 0
        for iso_code, country_data in country_relations.items():
            if not isinstance(country_data, dict):
                continue

            slug = country_data.get('slug', iso_code.lower())
            name = country_data.get('name', slug)
            continent_name = country_data.get('continent_name', '')

            # Resolve continent profile
            continent_profile = None
            if continent_name:
                cont_slug = continent_name.lower().replace(' ', '_')
                continent_profile = ContinentProfile.objects.filter(
                    planet_snapshot=planet_snapshot,
                    slug=cont_slug,
                ).first()

            # Find country PBF on disk
            country_pbf_path = None
            for candidate in [
                country_pbf_dir / slug / f"{slug}.osm.pbf",
                country_pbf_dir / slug / f"{slug}.pbf",
                country_pbf_dir / f"{slug}.osm.pbf",
                country_pbf_dir / f"{slug}.pbf",
            ]:
                if candidate.exists():
                    country_pbf_path = str(candidate)
                    break

            # Osmium fileinfo
            metrics = None
            if not skip_osmium and country_pbf_path:
                metrics = self._fileinfo(osmium, country_pbf_path)

            defaults = {
                'planet_snapshot': planet_snapshot,
                'continent_profile': continent_profile,
                'iso2': iso_code.upper() if len(iso_code) == 2 else None,
                'iso3': iso_code.upper() if len(iso_code) == 3 else None,
                'canonical_name': name,
                'embedding_slug': slug,
                'embedding_root_path': slug,
                'continent_name': continent_name,
                'snapshot_date': planet_snapshot.snapshot_date.strftime('%Y_%m_%d'),
                'osm_relation_id': country_data.get('relation_id'),
                'wikidata_id': country_data.get('wkg_uri', '').split('/')[-1] if country_data.get('wkg_uri') else None,
                'country_relations_payload': country_data,
                'file_size_bytes': Path(country_pbf_path).stat().st_size if country_pbf_path else None,
            }
            if metrics:
                defaults.update({
                    'node_count': metrics['nodes'],
                    'way_count': metrics['ways'],
                    'relation_count': metrics['relations'],
                    'file_size_bytes': metrics['file_size_bytes'],
                })

            if not dry_run:
                CountryPipelineProfile.objects.update_or_create(
                    canonical_slug=slug,
                    defaults=defaults,
                )

            node_str = f"{metrics['nodes']:,}" if metrics else "no-PBF"
            self.stdout.write(f"  {'+' if country_pbf_path else ' '} {iso_code}: {name} ({node_str} nodes)")
            count += 1

        return count

    # ══════════════════════════════════════════════════════════════════════
    # Phase 4: Subgraphs
    # ══════════════════════════════════════════════════════════════════════

    def _prebuild_subgraphs(self, osmium, skip_osmium, dry_run):
        """Scan existing SubgraphProfile rows and populate osmium metrics."""
        from core.models import SubgraphProfile

        count = 0
        for subgraph in SubgraphProfile.objects.filter(
            has_subgraph_pbf=True,
            subgraph_pbf_path__isnull=False,
        ):
            pbf_path = subgraph.subgraph_pbf_path
            if not pbf_path or not Path(pbf_path).exists():
                continue

            metrics = None
            if not skip_osmium:
                metrics = self._fileinfo(osmium, pbf_path)

            if metrics and not dry_run:
                subgraph.node_count = metrics['nodes']
                subgraph.way_count = metrics['ways']
                subgraph.relation_count = metrics['relations']
                subgraph.file_size_bytes = metrics['file_size_bytes']
                subgraph.bbox_min_lon = metrics.get('bbox_min_lon')
                subgraph.bbox_min_lat = metrics.get('bbox_min_lat')
                subgraph.bbox_max_lon = metrics.get('bbox_max_lon')
                subgraph.bbox_max_lat = metrics.get('bbox_max_lat')
                subgraph.save()
            count += 1

        return count

    # ══════════════════════════════════════════════════════════════════════
    # Utilities
    # ══════════════════════════════════════════════════════════════════════
    def _resolve_osmium(self):
        """Find osmium-tool executable."""
        candidates = [
            getattr(settings, 'OSMIUM_EXECUTABLE', None),
            '/usr/bin/osmium',
            '/usr/local/bin/osmium',
        ]
        for c in candidates:
            if c and Path(c).exists():
                return c
        try:
            result = subprocess.run(
                ['which', 'osmium-tool'], capture_output=True, text=True
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except FileNotFoundError:
            pass
        return None

    def _fileinfo(self, osmium: str, pbf_path: str) -> Optional[dict]:
        """Run osmium fileinfo --json and return parsed metrics."""
        if not pbf_path or not Path(pbf_path).exists():
            self.stdout.write(self.style.WARNING(f"  ⚠ PBF not found: {pbf_path}"))
            return None

        result = subprocess.run(
            [osmium, 'fileinfo', '--json', pbf_path],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            self.stdout.write(self.style.WARNING(
                f"  ⚠ osmium fileinfo failed: {result.stderr[:200]}"
            ))
            return None

        try:
            info = json.loads(result.stdout)
        except json.JSONDecodeError:
            return None

        header = info.get('header', info)
        file_info = info.get('file', {})
        
        # History PBFs don't have nested data — use top-level fields
        data = header.get('data', {})

        # Timestamps: history files put it in header.option
        timestamps = data.get('timestamp', {})
        if not timestamps and header.get('option', {}).get('timestamp'):
            t = header['option']['timestamp']
            timestamps = {'max': t, 'min': t}

        # Bbox: history files use header.boxes
        box = data.get('box', {})
        if not box and header.get('boxes'):
            b = header['boxes'][0]
            box = {'minlon': b[0], 'minlat': b[1], 'maxlon': b[2], 'maxlat': b[3]}

        # Entity counts: may be absent in history files
        nodes_data = data.get('nodes', {}) or {}
        ways_data = data.get('ways', {}) or {}
        rels_data = data.get('relations', {}) or {}

        return {
            'nodes': nodes_data.get('count', 0) if isinstance(nodes_data, dict) else 0,
            'ways': ways_data.get('count', 0) if isinstance(ways_data, dict) else 0,
            'relations': rels_data.get('count', 0) if isinstance(rels_data, dict) else 0,
            'file_size': file_info.get('size', Path(pbf_path).stat().st_size),
            'file_size_bytes': file_info.get('size', Path(pbf_path).stat().st_size),
            'min_timestamp': timestamps.get('min') if timestamps else None,
            'max_timestamp': timestamps.get('max') if timestamps else None,
            'bbox_min_lon': box.get('minlon'),
            'bbox_min_lat': box.get('minlat'),
            'bbox_max_lon': box.get('maxlon'),
            'bbox_max_lat': box.get('maxlat'),
        }
