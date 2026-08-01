"""
Populate OsmBoundary table with country geometries from .poly files.

This command reads every country-level .poly file from the RegionHierarchy /
PolygonFile tables, parses the geometry with the multi-ring-aware
parse_poly_file(), and stores the result in OsmBoundary for cartographic display.

Usage:
    python manage.py generate_osm_boundaries
    python manage.py generate_osm_boundaries --country scotland
    python manage.py generate_osm_boundaries --country scotland --country england
    python manage.py generate_osm_boundaries --clear

NOTE on maritime areas:
  .poly files from Geofabrik include territorial waters (the 12 NM EEZ).
  For purely cartographic display you may want to intersect with the OSM
  coastline.  That step is currently opt-in (--with-coastline-clip) because
  building the land polygon from a 145 GB planet PBF is expensive.
"""

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional, Union
from itertools import chain

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.contrib.gis.geos import GEOSGeometry, MultiPolygon, Polygon

from extraction.models import OsmBoundary, PolygonFile, RegionHierarchy
from extraction.utils.poly_parser import parse_poly_file
from extraction.services.osm_wikidata_resolver import get_country_relations_dict

logger = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────

def _build_slug_to_iso() -> Dict[str, str]:
    """Build a dict: lowercase slug → ISO code from country_relations.json.

    Resolves QID keys to real ISO 3166-1 alpha-2 codes when possible
    by cross-referencing CountryPipelineProfile.iso2 via OSMWikiDataHierarchy.
    """
    lookup = {}
    relations = get_country_relations_dict()

    # Pre-build a QID→ISO mapping from CountryPipelineProfile
    qid_to_iso = {}
    try:
        from orchestration.models import CountryPipelineProfile
        from extraction.models import OSMWikiDataHierarchy

        profiles = CountryPipelineProfile.objects.filter(
            iso2__isnull=False
        ).exclude(iso2='')
        for profile in profiles:
            if profile.iso2:
                qid = None
                if profile.wikidata_id:
                    qid = profile.wikidata_id.upper()
                elif profile.canonical_slug:
                    hierarchy = OSMWikiDataHierarchy.objects.filter(
                        slug=profile.canonical_slug, admin_level=2
                    ).first()
                    if hierarchy and hierarchy.wikidata_id:
                        qid = hierarchy.wikidata_id.upper()
                if qid:
                    qid_to_iso[qid] = profile.iso2.upper()
    except Exception:
        logger.warning("Could not build QID→ISO mapping from CountryPipelineProfile", exc_info=True)

    def _resolve_iso(iso_key: str, data: dict) -> str:
        """Resolve an ISO key to a real 2-letter ISO code."""
        key_upper = iso_key.upper()
        # Already looks like a real ISO code (2 letters)
        if len(key_upper) == 2 and key_upper.isalpha():
            return key_upper
        # Try to resolve QID via our mapping
        wkg_uri = data.get('wkg_uri', '')
        qid_from_uri = wkg_uri.rsplit('/', 1)[-1].upper() if wkg_uri else ''
        resolved = (
            qid_to_iso.get(key_upper)
            or qid_to_iso.get(qid_from_uri)
            or key_upper  # fallback to original (QID)
        )
        return resolved

    for iso_key, data in relations.items():
        slug = (data.get('slug') or '').lower()
        if slug:
            lookup[slug] = _resolve_iso(iso_key, data)
        name = (data.get('name') or '').lower().replace(' ', '_')
        if name:
            lookup[name] = _resolve_iso(iso_key, data)

    return lookup


def _compute_bbox_from_geometry(geometry: dict) -> List[float]:
    """Compute [minLon, minLat, maxLon, maxLat] from a GeoJSON geometry dict."""
    coords = []

    def extract_coords(geom):
        if geom['type'] == 'Polygon':
            for ring in geom['coordinates']:
                for pt in ring:
                    coords.append(pt)
        elif geom['type'] == 'MultiPolygon':
            for polygon in geom['coordinates']:
                for ring in polygon:
                    for pt in ring:
                        coords.append(pt)

    extract_coords(geometry)
    if not coords:
        return [0.0, 0.0, 0.0, 0.0]
    lons = [c[0] for c in coords]
    lats = [c[1] for c in coords]
    return [min(lons), min(lats), max(lons), max(lats)]


def _build_land_polygon(osmium_bin: str, planet_pbf: str, cache_dir: Path):
    """Build a unified land MultiPolygon by extracting natural=coastline."""
    import subprocess

    if not Path(planet_pbf).exists():
        logger.warning(f"Planet PBF not found at {planet_pbf} — skipping coastline clip")
        return None

    cache_path = cache_dir / 'coastline.geojson'
    if cache_path.exists():
        logger.info("Loading cached land polygon from %s", cache_path)
        with open(cache_path) as f:
            data = json.load(f)
        return GEOSGeometry(json.dumps(data))

    logger.info("Extracting coastline from planet PBF (this may take 10-20 minutes)...")

    coast_pbf = cache_dir / 'coastline.osm.pbf'
    try:
        subprocess.run([
            osmium_bin, 'tags-filter', planet_pbf, 'w/natural=coastline',
            '-o', str(coast_pbf),
        ], check=True, capture_output=True, timeout=7200)
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as e:
        logger.error(f"osmium tags-filter failed: {e}")
        return None

    try:
        result = subprocess.run([
            osmium_bin, 'export', str(coast_pbf), '-f', 'geojson', '-o', '-',
        ], check=True, capture_output=True, text=True, timeout=3600)
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as e:
        logger.error(f"osmium export failed: {e}")
        return None

    if not result.stdout.strip():
        logger.warning("No coastline features found")
        return None

    coast_geojson = json.loads(result.stdout)
    features = coast_geojson.get('features', [])
    if not features:
        logger.warning("Empty coastline GeoJSON")
        return None

    land_polys = []
    for feat in features:
        geom = feat.get('geometry')
        if geom is None:
            continue
        try:
            g = GEOSGeometry(json.dumps(geom))
            if isinstance(g, Polygon):
                land_polys.append(g)
            elif isinstance(g, MultiPolygon):
                land_polys.extend(g)
        except Exception:
            continue

    if not land_polys:
        logger.warning("No valid land polygons from coastline")
        return None

    merged = MultiPolygon(land_polys) if len(land_polys) > 1 else land_polys[0]
    merged.srid = 4326
    if merged.num_coords > 10000:
        merged = merged.simplify(0.01, preserve_topology=True)

    with open(cache_path, 'w') as f:
        f.write(merged.geojson)
    coast_pbf.unlink(missing_ok=True)

    logger.info(f"Land polygon built: {len(land_polys)} features, ~{merged.num_coords} points")
    return merged


def _clip_to_land(boundary_geometry: dict, land_polygon) -> dict:
    """Intersect boundary GeoJSON with global land polygon."""
    if land_polygon is None:
        return boundary_geometry
    try:
        boundary_geom = GEOSGeometry(json.dumps(boundary_geometry))
        boundary_geom.srid = 4326
        clipped = boundary_geom.intersection(land_polygon)
        if clipped.empty:
            logger.warning("Clip produced empty geometry — using original")
            return boundary_geometry
        if clipped.num_coords > 1000:
            clipped = clipped.simplify(0.01, preserve_topology=True)
        return json.loads(clipped.geojson)
    except Exception as e:
        logger.warning(f"Coastline clipping failed: {e} — using original")
        return boundary_geometry


class Command(BaseCommand):
    help = 'Populate OsmBoundary table with country geometries from .poly files'

    def add_arguments(self, parser):
        parser.add_argument(
            '--country', action='append', type=str, default=None,
            help='Process only the specified country (can be used multiple times)',
        )
        parser.add_argument(
            '--clear', action='store_true',
            help='Clear existing OsmBoundary records before generating',
        )
        parser.add_argument(
            '--with-coastline-clip', action='store_true',
            help='Enable coastline clipping (requires planet PBF, may take hours)',
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Print what would be done without writing to DB',
        )

    def handle(self, *args, **options):
        country_filter = options.get('country')
        clear = options['clear']
        with_clip = options['with_coastline_clip']
        dry_run = options['dry_run']

        self.stdout.write('=' * 60)
        self.stdout.write('Generate OSM Boundaries')
        self.stdout.write('=' * 60)

        if clear and not dry_run:
            deleted, _ = OsmBoundary.objects.all().delete()
            self.stdout.write(f'Cleared {deleted} existing OsmBoundary records')

        # ── ISO lookup ──
        slug_to_iso = _build_slug_to_iso()
        self.stdout.write(f'Loaded {len(slug_to_iso)} country relations for ISO lookup')

        # ── Land polygon (optional) ──
        land_polygon = None
        if with_clip:
            osmium_bin = settings.OSMIUM_EXECUTABLE
            planet_pbf = settings.PLANET_OSM_FILE_PATH
            cache_dir = Path(settings.OSM_WIKIDATA_EXTRACTIONS_DIR) / 'temp'
            cache_dir.mkdir(parents=True, exist_ok=True)
            self.stdout.write('Building land polygon from coastline...')
            land_polygon = _build_land_polygon(osmium_bin, planet_pbf, cache_dir)
            if land_polygon:
                self.stdout.write(self.style.SUCCESS('✓ Land polygon built'))
            else:
                self.stdout.write(self.style.WARNING('Land polygon could not be built'))

        # ── Query countries ──
        # Exclude synthetic parents used for osmium extraction only
        EXCLUDED_PARENTS = ['overrides', 'merge']

        # Get all real continent IDs
        continent_ids = list(RegionHierarchy.objects.filter(
            region_type=RegionHierarchy.RegionType.CONTINENT
        ).exclude(name__in=EXCLUDED_PARENTS).values_list('id', flat=True))

        # Direct children of continents (most countries)
        direct_ids = list(RegionHierarchy.objects.filter(
            parent_id__in=continent_ids,
            polygon_file__isnull=False,
        ).values_list('id', flat=True))

        # Grandchildren of continents (e.g. scotland → united_kingdom → europe)
        sub_country_parent_ids = list(RegionHierarchy.objects.filter(
            parent_id__in=continent_ids
        ).values_list('id', flat=True))
        indirect_ids = list(RegionHierarchy.objects.filter(
            parent_id__in=sub_country_parent_ids,
            polygon_file__isnull=False,
        ).values_list('id', flat=True))

        all_valid_ids = set(direct_ids) | set(indirect_ids)

        if country_filter:
            normalized = [c.strip().lower().replace(' ', '_') for c in country_filter]
            countries_qs = RegionHierarchy.objects.filter(
                name__in=normalized,
                id__in=list(all_valid_ids),
            ).select_related('polygon_file', 'parent')
        else:
            countries_qs = RegionHierarchy.objects.filter(
                id__in=list(all_valid_ids),
            ).select_related('polygon_file', 'parent')

        # Deduplicate by name
        seen = {}
        for c in countries_qs:
            key = c.name.lower()
            if key not in seen:
                seen[key] = c
        countries = list(seen.values())

        total = len(countries)
        self.stdout.write(f'Processing {total} countries...\n')

        created = 0
        skipped = 0
        failed = 0

        for country in countries:
            poly_file = country.polygon_file
            if not poly_file or not poly_file.file_path:
                self.stdout.write(f'  SKIP {country.name}: no polygon file path')
                skipped += 1
                continue

            poly_path = Path(poly_file.file_path)
            if not poly_path.exists():
                self.stdout.write(f'  SKIP {country.name}: not found at {poly_path}')
                skipped += 1
                continue

            # Parse geometry
            try:
                geometry = parse_poly_file(str(poly_path))
                geojson_geom = json.loads(geometry.geojson)
            except Exception as e:
                self.stdout.write(f'  FAIL {country.name}: parse error — {e}')
                failed += 1
                continue

            # Optional coastline clip
            if with_clip and land_polygon:
                geojson_geom = _clip_to_land(geojson_geom, land_polygon)

            bbox = _compute_bbox_from_geometry(geojson_geom)
            iso_code = slug_to_iso.get(country.name.lower())
            human_name = country.name.replace('_', ' ').title()

            if dry_run:
                self.stdout.write(
                    f'  [DRY-RUN] {human_name:25s} → iso={iso_code or "??":4s} '
                    f'bbox=({bbox[0]:.2f}, {bbox[1]:.2f}, {bbox[2]:.2f}, {bbox[3]:.2f})'
                )
                created += 1
                continue

            try:
                OsmBoundary.objects.filter(polygon_file=poly_file).delete()
                # Use a hash of the file path as a unique numeric ID
                # since we don't have the real OSM relation ID from .poly files
                from hashlib import md5
                unique_id = int(md5(str(poly_path).encode()).hexdigest()[:12], 16)
                OsmBoundary.objects.create(
                    osm_id=unique_id,
                    osm_type='relation',
                    admin_level=2,
                    name=human_name,
                    name_en=human_name,
                    iso_code=iso_code,
                    geometry=geojson_geom,
                    bbox=bbox,
                    polygon_file=poly_file,
                    simplification_tolerance=0.01,
                )
                created += 1
                self.stdout.write(f'  ✓ {human_name:25s} → OsmBoundary created')
            except Exception as e:
                self.stdout.write(f'  FAIL {human_name:25s}: {e}')
                failed += 1

        self.stdout.write('')
        self.stdout.write('─' * 40)
        self.stdout.write(f'  Created: {created}')
        self.stdout.write(f'  Skipped: {skipped}')
        self.stdout.write(f'  Failed:  {failed}')
        self.stdout.write(f'  Total:   {total}')
        self.stdout.write('─' * 40)

        if dry_run:
            self.stdout.write(self.style.WARNING('\n[DRY-RUN] No rows written to DB'))
        else:
            total_in_db = OsmBoundary.objects.count()
            self.stdout.write(f'\nOsmBoundary table now has {total_in_db} records')
            count_admin2 = OsmBoundary.objects.filter(admin_level=2).count()
            self.stdout.write(f'  admin_level=2: {count_admin2}')
