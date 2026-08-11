"""
Helper functions for resolving country metadata and geometry.

Originally this module provided a unified interface for services that
previously read from country_relations.json to now query the
OSMWikiDataHierarchy model. It has been extended to also act as the
canonical resolver for ISO / country → bounding box lookups, used by
multiple apps (extraction, worldkg_nca, semantic_search, orchestration).
"""

import logging
import math
import os
from typing import Dict, Optional, Tuple, List

from django.db import models

logger = logging.getLogger(__name__)
from extraction.models import OSMWikiDataHierarchy
from extraction.services.country_override_service import get_country_slug as get_override_country_slug
# TODO: implement a better caching system

# Module-level cache for country relations to avoid repeated DB queries
_country_relations_cache: Dict[str, dict] = None

# Module-level cache for bbox resolution to avoid repeated lookups
_bbox_cache: Dict[str, Tuple[float, float, float, float]] = {}

# ----------------------------------------------------------------------
# Polygon helpers (shared across apps)
# ----------------------------------------------------------------------

def parse_poly_bbox(poly_path: str) -> Optional[Tuple[float, float, float, float]]:
    """Parse an osmium .poly boundary file and return its bounding box.

    Returns (min_lon, min_lat, max_lon, max_lat) or None on parse failure.
    """

    try:
        lons: List[float] = []
        lats: List[float] = []
        with open(poly_path, "r") as f:
            for line in f:
                parts = line.strip().split()
                if len(parts) == 2:
                    try:
                        lons.append(float(parts[0]))
                        lats.append(float(parts[1]))
                    except ValueError:
                        continue
        if lons and lats:
            return min(lons), min(lats), max(lons), max(lats)
    except Exception as exc:
        logger.warning(f"parse_poly_bbox({poly_path}): {exc}")
    return None


def parse_poly_to_wkt(poly_path: str) -> Optional[str]:
    """Parse an osmium .poly file into a WKT POLYGON string (outer ring only)."""

    try:
        coords: List[Tuple[float, float]] = []
        reading_outer = False

        with open(poly_path, "r") as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                if stripped == "END":
                    if reading_outer and coords:
                        break  # end of first (outer) ring — stop here
                    reading_outer = False
                    continue
                parts = stripped.split()
                if len(parts) == 1:
                    # Ring number line: positive = outer ring, '!' prefix = hole
                    reading_outer = not stripped.startswith("!")
                    continue
                if reading_outer and len(parts) == 2:
                    try:
                        coords.append((float(parts[0]), float(parts[1])))
                    except ValueError:
                        pass

        if len(coords) < 3:
            return None

        # Close the ring if open
        if coords[0] != coords[-1]:
            coords.append(coords[0])

        coord_str = ", ".join(f"{lon} {lat}" for lon, lat in coords)
        return f"POLYGON(({coord_str}))"

    except Exception as exc:
        logger.warning(f"parse_poly_to_wkt({poly_path}): {exc}")
        return None


def bbox_to_wkt(min_lon: float, min_lat: float, max_lon: float, max_lat: float) -> str:
    """Convert a bounding box to a WKT POLYGON string for PostGIS use."""

    return (
        f"POLYGON(({min_lon} {min_lat}, {max_lon} {min_lat}, "
        f"{max_lon} {max_lat}, {min_lon} {max_lat}, {min_lon} {min_lat}))"
    )


# ----------------------------------------------------------------------
# Bounding box expansion helpers
# ----------------------------------------------------------------------

# Minimum bounding box span in degrees (applied to both lon and lat).
# Countries with spans below this threshold (e.g. Monaco, Vatican City)
# will be symmetrically expanded outward to meet the minimum.
MIN_BBOX_SPAN = 0.5


def expand_bbox_to_min_span(
    bbox: Tuple[float, float, float, float],
    min_span: float = MIN_BBOX_SPAN,
) -> Tuple[float, float, float, float]:
    """Symmetrically expand a bounding box so both axes meet *min_span*.

    Accepts antimeridian-crossing bboxes (lon span > 180°) and preserves
    the geographic centre of the original bbox.  Longitude is clamped to
    [-180, 180] after expansion; latitude is clamped to [-90, 90].

    Args:
        bbox: (min_lon, min_lat, max_lon, max_lat) in degrees.
        min_span: Minimum span in degrees for both lon and lat (default 0.5).

    Returns:
        Expanded (min_lon, min_lat, max_lon, max_lat).

    Examples:
        >>> expand_bbox_to_min_span((7.40, 43.72, 7.44, 43.75), 0.5)
        (7.38, 43.695, 7.46, 43.775)
        >>> expand_bbox_to_min_span((2.0, 48.0, 2.0, 48.0), 0.5)
        (1.75, 47.75, 2.25, 48.25)
    """
    min_lon, min_lat, max_lon, max_lat = bbox

    lon_span = max_lon - min_lon
    lat_span = max_lat - min_lat

    # Handle antimeridian: if span > 180° the bbox already wraps around,
    # so we don't need to expand lon (it's already large enough).
    # However, a span < -180° would indicate invalid ordering; fix it.
    if lon_span < 0:
        min_lon, max_lon = max_lon, min_lon
        lon_span = max_lon - min_lon

    # Expand longitude axis
    if lon_span < min_span:
        delta_lon = (min_span - lon_span) / 2.0
        min_lon -= delta_lon
        max_lon += delta_lon

    # Expand latitude axis
    if lat_span < min_span:
        delta_lat = (min_span - lat_span) / 2.0
        min_lat -= delta_lat
        max_lat += delta_lat

    # Clamp latitude to [-90, 90]
    if min_lat < -90:
        # Shift the box so it stays within [-90, 90]
        max_lat += (-90 - min_lat)
        min_lat = -90.0
    if max_lat > 90:
        min_lat -= (max_lat - 90)
        max_lat = 90.0
    # Re-check min_lat after adjustment
    if min_lat < -90:
        min_lat = -90.0

    # Clamp longitude to [-180, 180]
    # For antimeridian-crossing bboxes, we allow the span to exceed 180,
    # but we still wrap individual values.
    if lon_span >= 0:
        if min_lon < -180:
            min_lon = -180.0
        if max_lon > 180:
            max_lon = 180.0

    return (min_lon, min_lat, max_lon, max_lat)


# ----------------------------------------------------------------------
# Country bbox resolution (DB-only, no fallbacks)
# ----------------------------------------------------------------------

def resolve_iso_code(input_code: str) -> str:
    """Resolve a QID or country name to a real ISO 3166-1 alpha-2 code.

    Handles three input forms:
      - QID (e.g. 'Q27')          -> looks up OSMWikiDataHierarchy.wikidata_id
      - Country name (e.g. 'Ireland', 'IRELAND') -> looks up via get_country_by_name
      - Synthetic ISO (e.g. 'WL') -> returns as-is (already a valid synthetic code)
      - Real ISO (e.g. 'IE')      -> returns as-is (no resolution needed)

    Resolution chain:
      1. OSMWikiDataHierarchy + CountryPipelineProfile.iso2
      2. Non-sovereign synthetic registry (for territories like Wales/Scotland)
      3. get_country_relations_dict() cross-reference

    Returns '' (empty string) if unresolvable, never None.
    """
    if not input_code:
        return ''

    code = input_code.strip().upper()

    # Already looks like a real ISO 3166-1 alpha-2 (2 letters) - return as-is
    if len(code) == 2 and code.isalpha():
        return code

    # Check non-sovereign synthetic registry first (fast, no DB hit)
    try:
        from extraction.services.non_sovereign_territories import is_non_sovereign_synthetic_iso
        if is_non_sovereign_synthetic_iso(code):
            return code
    except Exception:
        pass

    # Try OSMWikiDataHierarchy lookup by QID
    try:
        hierarchy = OSMWikiDataHierarchy.objects.filter(
            wikidata_id__iexact=code,
        ).first()
        if hierarchy:
            from orchestration.models import CountryPipelineProfile
            profile = CountryPipelineProfile.objects.filter(
                osm_relation_id=hierarchy.osm_relation_id,
            ).first()
            if profile and profile.iso2:
                return profile.iso2.upper()
            # Fallback: cross-reference via get_country_relations_dict()
            relations = get_country_relations_dict()
            for iso_k, data in relations.items():
                if (
                    (data.get("wkg_uri") or "").endswith(code)
                    or (data.get("wikidata_id") or "").upper() == code
                ):
                    return iso_k
    except Exception:
        pass

    # Try OSMWikiDataHierarchy lookup by name
    try:
        from extraction.services.regional_path_service import normalize_country_slug

        hierarchy = OSMWikiDataHierarchy.objects.filter(
            name__iexact=code,
        ).first()
        if not hierarchy:
            hierarchy = OSMWikiDataHierarchy.objects.filter(
                name__iexact=code.replace("_", " "),
            ).first()
        if not hierarchy:
            hierarchy = OSMWikiDataHierarchy.objects.filter(
                name__icontains=code,
            ).first()
        if hierarchy:
            from orchestration.models import CountryPipelineProfile
            profile = CountryPipelineProfile.objects.filter(
                osm_relation_id=hierarchy.osm_relation_id,
            ).first()
            if profile and profile.iso2:
                return profile.iso2.upper()
            relations = get_country_relations_dict()
            for iso_k, data in relations.items():
                norm_slug = normalize_country_slug(data.get("name", ""))
                if norm_slug == normalize_country_slug(code):
                    return iso_k
    except Exception:
        pass

    # Try via get_country_by_name as a final fallback
    try:
        country_meta = get_country_by_name(code)
        if country_meta:
            slug = country_meta.get("slug", "")
            from orchestration.models import CountryPipelineProfile
            profile = CountryPipelineProfile.objects.filter(
                canonical_slug__iexact=slug,
            ).first()
            if profile and profile.iso2:
                return profile.iso2.upper()
    except Exception:
        pass

    return ''


def resolve_country_bbox(
    iso_code: Optional[str],
    poly_file_path: Optional[str] = None,
) -> Optional[Tuple[float, float, float, float]]:
    """Resolve a country to (min_lon, min_lat, max_lon, max_lat) from DB ground truth (cached).

    Resolution order:
      0. Pre-resolve: QID / country name to real ISO code (see resolve_iso_code)
      1. Explicit poly_file_path (.poly file)
      2. extraction.OsmBoundary DB record (by iso_code)
      3. CountryPipelineProfile.country_relations_payload.bbox (authoritative from country_relations.json)
      4. Runtime-generated snapshot poly file (from temporal_snapshots)

    No fallback to hardcoded values. BBOX must exist in DB or on disk.
    """
    global _bbox_cache

    # Check cache first
    cache_key = f"{iso_code}:{poly_file_path}"
    if cache_key in _bbox_cache:
        return _bbox_cache[cache_key]

    # 1. Explicit poly file
    if poly_file_path:
        bbox = parse_poly_bbox(poly_file_path)
        if bbox:
            logger.info(
                "Resolved bbox for '%s' from explicit poly file: %s",
                iso_code, poly_file_path,
            )
            _bbox_cache[cache_key] = bbox
            return bbox

    if not iso_code:
        return None

    # -- Step 0: Pre-resolve QID / country name to a real ISO code ----------
    code = iso_code.strip().upper()
    resolved_iso = resolve_iso_code(iso_code)
    if resolved_iso:
        code = resolved_iso
        logger.info(
            "resolve_country_bbox: pre-resolved '%s' -> '%s'",
            iso_code, resolved_iso,
        )
    # If resolution failed, keep `code` as the original input and let the
    # downstream steps fail gracefully with a clear error log.

    # 2. OsmBoundary DB lookup (high precision from recipes)
    #    Try by iso_code, then by QID, then by name.
    #    If the bbox is too small, expand it symmetrically to meet the minimum span.
    try:
        from extraction.models import OsmBoundary

        # Multiple lookup strategies.
        # ⚠️  Do NOT use name__icontains with a 2-letter ISO code — it matches
        # substrings in unrelated country names (e.g. "IL" → "Brazil",
        # "CD" → "Heard Mcdonald", "SN" → "Bosnia Herzegovina").  Only use
        # name lookups when the input is a full country name (len > 3).
        ob = None
        strategies = [("iso_code__iexact", code)]
        if len(code) > 3:
            # Input looks like a country name, not an ISO code — safe to
            # do exact and contains name lookups.
            strategies.extend([
                ("name__iexact", iso_code),        # e.g. "Australia"
                ("name__iexact", iso_code.replace("_", " ")),
            ])
        for lookup, val in strategies:
            ob = OsmBoundary.objects.filter(**{lookup: val}).first()
            if ob and ob.bbox:
                break

        if ob is None and resolved_iso:
            # Also try with the resolved ISO code
            ob = OsmBoundary.objects.filter(iso_code__iexact=resolved_iso).first()

        if ob and ob.bbox:
            bbox = tuple(ob.bbox)
            lon_span = bbox[2] - bbox[0]
            lat_span = bbox[3] - bbox[1]
            if lon_span >= MIN_BBOX_SPAN and lat_span >= MIN_BBOX_SPAN:
                logger.info(f"Resolved bbox for '{code}' from OsmBoundary: {ob.name} iso={ob.iso_code}")
                _bbox_cache[cache_key] = bbox
                return bbox
            else:
                bbox = expand_bbox_to_min_span(bbox, MIN_BBOX_SPAN)
                logger.warning(
                    "Expanded OsmBoundary bbox for '%s': "
                    "original span (%.2f° x %.2f°) < %.1f°; "
                    "expanded to %s",
                    code, lon_span, lat_span, MIN_BBOX_SPAN, bbox,
                )
                _bbox_cache[cache_key] = bbox
                return bbox
    except Exception:
        pass

    # 3. CountryPipelineProfile.country_relations_payload.bbox (authoritative from country_relations.json)
    #    If the bbox is too small, expand it symmetrically to meet the minimum span.
    try:
        from orchestration.models import CountryPipelineProfile

        profile = CountryPipelineProfile.objects.filter(
            models.Q(iso2__iexact=code) | models.Q(iso3__iexact=code)
        ).first()
        if profile and hasattr(profile, "country_relations_payload") and profile.country_relations_payload:
            bb = profile.country_relations_payload.get("bbox")
            if bb and all(k in bb for k in ["min_lon", "min_lat", "max_lon", "max_lat"]):
                bbox = (bb["min_lon"], bb["min_lat"], bb["max_lon"], bb["max_lat"])
                lon_span = bb["max_lon"] - bb["min_lon"]
                lat_span = bb["max_lat"] - bb["min_lat"]
                if lon_span >= MIN_BBOX_SPAN and lat_span >= MIN_BBOX_SPAN:
                    logger.info(f"Resolved bbox for '{code}' from CountryPipelineProfile: {profile.canonical_slug}")
                    _bbox_cache[cache_key] = bbox
                    return bbox
                else:
                    # Expand small bbox to meet minimum span
                    bbox = expand_bbox_to_min_span(bbox, MIN_BBOX_SPAN)
                    logger.warning(
                        f"Expanded CountryPipelineProfile bbox for '{code}': "
                        f"original span ({lon_span:.2f}° x {lat_span:.2f}°) < {MIN_BBOX_SPAN}°; "
                        f"expanded to {bbox}"
                    )
                    _bbox_cache[cache_key] = bbox
                    return bbox
    except Exception:
        pass

    # 4. Runtime-generated snapshot poly file (from _preprocess_snapshot Phase 3).
    #    The .poly file is generated during Step 1 preprocessing and lives alongside
    #    the snapshot PBF in the temporal_snapshots directory:
    #      {osm_wikidata_extractions}/{continent}/{country}/temporal_snapshots/{country}_{date}.osm.poly
    #
    #    Temporal preprocessing honours overrides.json for special-case slugs
    #    (via get_country_slug). To avoid slug mismatches (e.g. 'bahamas'
    #    vs 'the_bahamas'), try both canonical and override-based slugs.
    try:
        from django.conf import settings as dj_settings
        from extraction.services.regional_path_service import (
            regional_path_service,
            normalize_country_slug,
        )
        from orchestration.models import CountryPipelineProfile

        profile = CountryPipelineProfile.objects.filter(
            models.Q(iso2__iexact=code) | models.Q(iso3__iexact=code)
        ).first()

        candidate_slugs: List[str] = []

        if not profile:
            # Fall back to OSMWikiDataHierarchy for countries not yet in profiles
            hierarchy_data = get_country_by_iso(code)
            if hierarchy_data:
                profile_h = OSMWikiDataHierarchy.objects.filter(
                    admin_level=2, slug=hierarchy_data["slug"]
                ).first()
                if profile_h:
                    cont_norm = normalize_country_slug(
                        profile_h.parent_slug
                        or profile_h.continent_name
                        or "unknown"
                    )
                    base_slug = profile_h.slug or code.lower()
                else:
                    cont_norm = normalize_country_slug(code)
                    base_slug = code.lower()
            else:
                cont_norm = normalize_country_slug(code)
                base_slug = code.lower()

            candidate_slugs.append(base_slug)
            # Even without a profile, let overrides.json remap BS→the_bahamas
            override_slug = get_override_country_slug(code, base_slug)
            if override_slug and override_slug not in candidate_slugs:
                candidate_slugs.append(override_slug)
        else:
            base_slug = profile.canonical_slug or profile.embedding_slug or code.lower()
            cont_slug = profile.continent_name or ""
            if not cont_slug:
                # Try to derive continent from hierarchy
                hierarchy_profile = OSMWikiDataHierarchy.objects.filter(
                    slug=base_slug, admin_level=2
                ).first()
                cont_slug = hierarchy_profile.parent_slug if hierarchy_profile else "unknown"
            cont_norm = normalize_country_slug(cont_slug)

            # Build candidate slugs: canonical, override (from overrides.json), and
            # normalized variants, de-duplicated while preserving order.
            candidate_slugs.append(base_slug)
            override_slug = get_override_country_slug(code, base_slug)
            if override_slug and override_slug not in candidate_slugs:
                candidate_slugs.append(override_slug)
            normed = normalize_country_slug(base_slug)
            if normed not in candidate_slugs:
                candidate_slugs.append(normed)

        snap_date = getattr(dj_settings, "SINGLE_SNAPSHOT_DATE", "2025_12_31")

        # Try each candidate slug when resolving the snapshot PBF/poly path.
        for slug_candidate in candidate_slugs:
            try:
                pbf_path = regional_path_service.get_single_snapshot_pbf_path(
                    cont_norm, slug_candidate, snap_date,
                )
            except Exception:
                continue

            snap_dir = pbf_path.parent
            possible_paths = [
                snap_dir / pbf_path.name.replace(".pbf", ".poly"),
                snap_dir
                / f"{normalize_country_slug(slug_candidate)}_{snap_date.replace('_', '-')}.osm.poly",
            ]

            for poly_path in possible_paths:
                if poly_path.exists():
                    bbox = parse_poly_bbox(str(poly_path))
                    if bbox:
                        logger.info(
                            "Resolved bbox for '%s' from runtime snapshot poly: %s",
                            code,
                            poly_path,
                        )
                        _bbox_cache[cache_key] = bbox
                        return bbox
    except Exception:
        pass

    # 5. Vectors DB extent fallback — compute bbox from actual entity geometries.
    #    This is a last resort when no other source has the bbox.
    #    It uses the addr:country tag or the OSM admin boundary relation ID
    #    to filter entities, then computes the spatial extent via ST_Extent().
    try:
        from django.db import connections

        with connections["vectors"].cursor() as cursor:
            # Try addr:country tag first (most reliable)
            cursor.execute(
                "SELECT ST_Extent(geom) FROM semantic_search_osmentity "
                "WHERE tags->>'addr:country' = %s",
                [code],
            )
            row = cursor.fetchone()
            if row and row[0]:
                extent_str = row[0]  # "BOX(min_lon min_lat, max_lon max_lat)"
                parts = extent_str.replace("BOX(", "").replace(")", "").split(",")
                if len(parts) == 2:
                    min_pt = parts[0].strip().split()
                    max_pt = parts[1].strip().split()
                    if len(min_pt) == 2 and len(max_pt) == 2:
                        bbox = (
                            float(min_pt[0]), float(min_pt[1]),
                            float(max_pt[0]), float(max_pt[1]),
                        )
                        logger.info(
                            "Resolved bbox for '%s' from vectors DB extent (addr:country)",
                            iso_code,
                        )
                        _bbox_cache[cache_key] = bbox
                        return bbox
    except Exception:
        pass

    logger.error(
        f"Cannot resolve bbox for country '{iso_code}' from DB or runtime snapshot poly files. "
        f"Run the pipeline Step 1 first to generate the snapshot poly file, or manually "
        f"add an OsmBoundary/PolygonFile DB record for this country."
    )
    return None


def populate_bbox_for_profile(profile) -> Optional[Tuple[float, float, float, float]]:
    """Resolve and persist a bbox on a CountryPipelineProfile.

    This is the canonical bbox population function. It resolves the bbox
    using the existing chain, stores it in profile.country_relations_payload.bbox,
    and logs which source was used. All consumers (USLP, Wikidata harvest, IGEA)
    should read from the profile's stored bbox after this runs.

    Resolution order (logged per source):
      1. profile.country_poly → parse_poly_bbox(file_path)
      2. OsmiumDatasetMetrics.bounding_box for profile.country_pbf
      3. resolve_country_bbox(profile.iso2 or profile.iso3)
         a. Explicit poly_file_path
         b. OsmBoundary DB record
         c. CountryPipelineProfile.country_relations_payload.bbox
         d. Runtime snapshot poly file

    Returns the bbox tuple or None if unresolvable.
    """
    iso_code = (getattr(profile, "iso2", None) or getattr(profile, "iso3", None) or "??").upper()

    # 1. Direct country_poly polygon
    country_poly = getattr(profile, "country_poly", None)
    if country_poly and getattr(country_poly, "file_path", None):
        poly_path = country_poly.file_path
        if poly_path and os.path.exists(poly_path):
            bbox = parse_poly_bbox(poly_path)
            if bbox:
                _store_bbox_on_profile(profile, bbox)
                logger.info(
                    "populate_bbox_for_profile: resolved bbox for '%s' from country_poly (%s)",
                    iso_code, poly_path,
                )
                return bbox

    # 2. PBF-level metrics
    country_pbf = getattr(profile, "country_pbf", None)
    if country_pbf is not None:
        try:
            from orchestration.models import OsmiumDatasetMetrics

            metrics = (
                OsmiumDatasetMetrics.objects.filter(pbf_file=country_pbf)
                .order_by("-metrics_generated_at")
                .first()
            )
            if metrics and metrics.bounding_box:
                bb = metrics.bounding_box
                try:
                    bbox = (bb["min_lon"], bb["min_lat"], bb["max_lon"], bb["max_lat"])
                    _store_bbox_on_profile(profile, bbox)
                    logger.info(
                        "populate_bbox_for_profile: resolved bbox for '%s' from OsmiumDatasetMetrics",
                        iso_code,
                    )
                    return bbox
                except (KeyError, TypeError):
                    logger.warning(
                        "populate_bbox_for_profile: OsmiumDatasetMetrics.bounding_box has unexpected format: %r",
                        bb,
                    )
        except Exception as exc:
            logger.warning("populate_bbox_for_profile: metrics lookup failed for '%s': %s", iso_code, exc)

    # 3. ISO-based resolution (calls resolve_country_bbox which logs its own source)
    code = iso_code
    if code:
        bbox = resolve_country_bbox(code)
        if bbox:
            _store_bbox_on_profile(profile, bbox)
            logger.info(
                "populate_bbox_for_profile: resolved bbox for '%s' via resolve_country_bbox",
                iso_code,
            )
            return bbox

    logger.error(
        "populate_bbox_for_profile: cannot resolve bbox for '%s' from any source",
        iso_code,
    )
    return None


def _store_bbox_on_profile(profile, bbox: Tuple[float, float, float, float]) -> None:
    """Persist a resolved bbox onto a CountryPipelineProfile's country_relations_payload."""
    try:
        payload = getattr(profile, "country_relations_payload", None) or {}
        payload["bbox"] = {
            "min_lon": bbox[0],
            "min_lat": bbox[1],
            "max_lon": bbox[2],
            "max_lat": bbox[3],
        }
        profile.country_relations_payload = payload
        profile.save(update_fields=["country_relations_payload", "updated_at"])
    except Exception as exc:
        logger.warning(
            "_store_bbox_on_profile: failed to persist bbox on profile: %s", exc,
        )


def resolve_bbox_for_profile(profile) -> Optional[Tuple[float, float, float, float]]:
    """Resolve a bounding box for a CountryPipelineProfile-like object.

    This expects an object with attributes similar to
    orchestration.models.CountryPipelineProfile, but is kept loosely
    coupled (no hard type requirements) so it can be reused in tests or
    utility scripts.

    Resolution order:
      1. profile.country_poly → parse_poly_bbox(file_path)
      2. OsmiumDatasetMetrics.bounding_box for profile.country_pbf
      3. resolve_country_bbox(profile.iso2 or profile.iso3)
    """

    # 1. Direct country_poly polygon
    country_poly = getattr(profile, "country_poly", None)
    if country_poly and getattr(country_poly, "file_path", None):
        poly_path = country_poly.file_path
        if poly_path and os.path.exists(poly_path):
            bbox = parse_poly_bbox(poly_path)
            if bbox:
                return bbox

    # 2. PBF-level metrics (bounding_box JSON)
    country_pbf = getattr(profile, "country_pbf", None)
    if country_pbf is not None:
        try:
            from orchestration.models import OsmiumDatasetMetrics

            metrics = (
                OsmiumDatasetMetrics.objects.filter(pbf_file=country_pbf)
                .order_by("-metrics_generated_at")
                .first()
            )
            if metrics and metrics.bounding_box:
                bb = metrics.bounding_box
                try:
                    return (
                        bb["min_lon"],
                        bb["min_lat"],
                        bb["max_lon"],
                        bb["max_lat"],
                    )
                except (KeyError, TypeError):
                    logger.warning(
                        "OsmiumDatasetMetrics.bounding_box has unexpected format: %r",
                        bb,
                    )
        except Exception as exc:
            logger.warning("resolve_bbox_for_profile metrics lookup failed: %s", exc)

    # 3. ISO-based resolution
    iso2 = getattr(profile, "iso2", None)
    iso3 = getattr(profile, "iso3", None)
    code = (iso2 or "").upper() or (iso3 or "").upper()
    if code:
        return resolve_country_bbox(code)

    return None


def get_country_relations_dict() -> Dict[str, dict]:
    """
    Load country relations from OSMWikiDataHierarchy model (cached).

    Returns a dict compatible with the old country_relations.json format:
    {
        'ISO_CODE': {
            'name': 'Country Name',
            'relation_id': 12345,
            'slug': 'country_slug',
            'parent_slug': 'continent_slug',
            'continent_name': 'continent_name',
            'continent_id': 'uuid',
            'pbf_url': 'https://...',
            'wkg_uri': 'http://www.wikidata.org/entity/Q781'
        },
        ...
    }
    """
    global _country_relations_cache
    if _country_relations_cache is not None:
        return _country_relations_cache
    try:
        import json
        from pathlib import Path
        from django.conf import settings

        # 1. Load data from the legacy JSON file if it exists. We only use this
        #    as an optional metadata overlay (ISO codes, Geovectors TSV paths),
        #    not as the primary source of truth.
        json_relations_by_uri: Dict[str, dict] = {}
        json_path = Path(settings.BASE_DIR) / "data" / "country_relations.json"
        if json_path.exists():
            try:
                with open(json_path, "r") as f:
                    json_data = json.load(f)
                    for iso_code, v in json_data.items():
                        wkg_uri = v.get("wkg_uri")
                        if not wkg_uri:
                            continue
                        v["iso_code"] = iso_code.upper()
                        json_relations_by_uri[wkg_uri] = v
            except Exception as e:
                logger.warning(f"Failed to load country_relations.json: {e}")

        # 2. Query all countries (admin_level=2) from OSMWikiDataHierarchy
        hierarchies = list(OSMWikiDataHierarchy.objects.filter(admin_level=2))

        # 3. Convert to dict format compatible with existing code, keyed
        #    primarily by ISO 3166-1 alpha-2 codes. When no ISO is known for a
        #    row, we fall back to using the Wikidata ID (QID) as the key.
        relations: Dict[str, dict] = {}
        for hierarchy in hierarchies:
            json_entry = json_relations_by_uri.get(hierarchy.wikidata_uri or "")

            # Prefer ISO code from JSON overlay when available
            iso_code = None
            if json_entry is not None:
                iso_code = json_entry.get("iso_code")

            # Fallback: use Wikidata ID / last URI segment as synthetic key
            if not iso_code:
                iso_code = hierarchy.wikidata_id or (
                    (hierarchy.wikidata_uri or "").rsplit("/", 1)[-1]
                    if hierarchy.wikidata_uri
                    else None
                )

            if not iso_code:
                continue

            key = iso_code.upper()

            data = {
                "name": hierarchy.name,
                "relation_id": hierarchy.osm_relation_id,
                "slug": hierarchy.slug,
                "parent_slug": hierarchy.parent_slug,
                "continent_name": hierarchy.continent_name,
                "continent_id": str(hierarchy.continent_id)
                if hierarchy.continent_id
                else None,
                "pbf_url": hierarchy.pbf_url,
                "wkg_uri": hierarchy.wikidata_uri,
                "iso_code": key,
            }

            # Merge TSV paths and legacy metadata from JSON when available
            if json_entry is not None:
                if json_entry.get("geovectors_location_tsv"):
                    data["geovectors_location_tsv"] = json_entry[
                        "geovectors_location_tsv"
                    ]
                if json_entry.get("geovectors_tags_tsv"):
                    data["geovectors_tags_tsv"] = json_entry["geovectors_tags_tsv"]
                # Prefer JSON pbf_url if DB does not have one
                if not data["pbf_url"] and json_entry.get("pbf_url"):
                    data["pbf_url"] = json_entry["pbf_url"]

            relations[key] = data

            qid = None
            if hierarchy.wikidata_id:
                qid = hierarchy.wikidata_id.upper()
            elif hierarchy.wikidata_uri:
                qid = hierarchy.wikidata_uri.rsplit("/", 1)[-1].upper()
            if qid and qid != key and qid not in relations:
                relations[qid] = dict(data)

        logger.info(
            "Loaded %d country relations from OSMWikiDataHierarchy (merged with JSON metadata)",
            len(hierarchies),
        )
        _country_relations_cache = relations
        return relations
    except Exception as exc:
        logger.error("Failed to load country relations from OSMWikiDataHierarchy: %s", exc)
        return {}


def get_country_by_iso(iso_code: str) -> Optional[dict]:
    """
    Get country data for a specific ISO code.
    
    Args:
        iso_code: ISO 3166-1 alpha-2 or alpha-3 code, or Wikidata ID
    
    Returns:
        Country data dict or None if not found
    """
    try:
        hierarchy = OSMWikiDataHierarchy.objects.filter(
            admin_level=2
        ).filter(
            models.Q(wikidata_id__icontains=iso_code) | 
            models.Q(slug__icontains=iso_code.lower())
        ).first()
        
        if not hierarchy:
            return None
        
        return {
            'name': hierarchy.name,
            'relation_id': hierarchy.osm_relation_id,
            'slug': hierarchy.slug,
            'parent_slug': hierarchy.parent_slug,
            'continent_name': hierarchy.continent_name,
            'continent_id': str(hierarchy.continent_id) if hierarchy.continent_id else None,
            'pbf_url': hierarchy.pbf_url,
            'wkg_uri': hierarchy.wikidata_uri,
            'wikidata_id': hierarchy.wikidata_id
        }
    except Exception as exc:
        logger.error(f"Failed to get country by ISO {iso_code}: {exc}")
        return None


def get_country_by_name(country_name: str) -> Optional[dict]:
    """
    Get country data by country name (case-insensitive).
    
    Args:
        country_name: Country name (e.g., 'Monaco', 'El Salvador')
    
    Returns:
        Country data dict or None if not found
    """
    try:
        hierarchy = OSMWikiDataHierarchy.objects.filter(
            admin_level=2,
            name__icontains=country_name
        ).first()
        
        if not hierarchy:
            return None
        
        iso_code = hierarchy.wikidata_id or (hierarchy.wikidata_uri.split('/')[-1] if hierarchy.wikidata_uri else None)
        
        return {
            'name': hierarchy.name,
            'relation_id': hierarchy.osm_relation_id,
            'slug': hierarchy.slug,
            'parent_slug': hierarchy.parent_slug,
            'continent_name': hierarchy.continent_name,
            'continent_id': str(hierarchy.continent_id) if hierarchy.continent_id else None,
            'pbf_url': hierarchy.pbf_url,
            'wkg_uri': hierarchy.wikidata_uri,
            'wikidata_id': iso_code
        }
    except Exception as exc:
        logger.error(f"Failed to get country by name {country_name}: {exc}")
        return None
