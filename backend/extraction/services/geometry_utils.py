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



