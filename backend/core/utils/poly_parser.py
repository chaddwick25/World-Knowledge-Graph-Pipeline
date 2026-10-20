from django.contrib.gis.geos import Polygon, MultiPolygon
from pathlib import Path
import re


def parse_poly_file(poly_path):
    """
    Parse a .poly file and return a Django GEOSGeometry.

    Collects ALL rings from the .poly file and returns:
      - A single Polygon if only one ring is found
      - A MultiPolygon if multiple rings are found (e.g. archipelago nations)

    The old behaviour of picking only the largest ring caused entire islands
    (Shetland, Hebrides, Corsica, etc.) to disappear from the map. Every ring
    is now preserved as a separate Polygon in the MultiPolygon.

    Args:
        poly_path: Path to .poly file

    Returns:
        Polygon or MultiPolygon: Django GEOSGeometry with SRID 4326

    Example .poly format:
        polygon_name
        1
            -79.4  43.6
            ...
        END
        2
            -3.15  49.16
            ...
        END
        END
    """
    with open(poly_path, 'r') as f:
        lines = f.readlines()

    rings = []          # list of coordinate lists, one per ring
    current = []        # coordinates for the ring being parsed
    in_ring = False

    for line in lines:
        line = line.strip()

        if not line:
            continue

        # A bare integer (optionally preceded by '!' for excluded rings) marks
        # the start of a new ring section.
        stripped = line.lstrip('!')
        if stripped.isdigit():
            if in_ring and current:
                rings.append(current)
                current = []
            in_ring = True
            current = []
            continue

        if line == 'END':
            if in_ring and current:
                rings.append(current)
                current = []
                in_ring = False
            continue

        if in_ring:
            try:
                parts = line.split()
                if len(parts) >= 2:
                    lon = float(parts[0])
                    lat = float(parts[1])
                    current.append((lon, lat))
            except (ValueError, IndexError):
                continue

    # Flush any remaining ring
    if in_ring and current:
        rings.append(current)

    if not rings:
        raise ValueError(f"No polygon rings found in {poly_path}")

    # Build a Polygon for every ring that has at least 3 valid points.
    polygons = []
    for coords in rings:
        if len(coords) < 3:
            continue
        # Ensure the ring is closed
        if coords[0] != coords[-1]:
            coords.append(coords[0])
        try:
            polygons.append(Polygon(coords, srid=4326))
        except Exception:
            # Skip rings that produce invalid geometry (e.g., self-intersecting)
            continue

    if not polygons:
        raise ValueError(f"No valid polygon rings found in {poly_path}")

    if len(polygons) == 1:
        return polygons[0]

    return MultiPolygon(polygons, srid=4326)


def extract_metadata_from_path(poly_path):
    """
    Extract country, region, and name from polygon file path.
    
    Args:
        poly_path: Path to .poly file (absolute or relative)
        
    Returns:
        dict: {
            'country': str,
            'region': str or None,
            'name': str,
            'geouid': str
        }
        
    Examples:
        /path/to/canada/ontario/golden_horseshoe.poly
        → country='canada', region='ontario', name='Golden Horseshoe', 
          geouid='canada-ontario-golden_horseshoe'
        
        /path/to/europe/italy.poly
        → country='europe', region=None, name='Italy',
          geouid='europe-italy'
    """
    path = Path(poly_path)
    filename = path.stem  # Remove .poly extension
    
    # Get parent directories
    parts = path.parts
    
    # Find country and region from path structure
    # Expected: .../north-america/canada/ontario/golden_horseshoe.poly
    # or: .../europe/italy.poly
    
    country = None
    region = None
    
    # Look for common continent/country patterns
    for i, part in enumerate(parts):
        if part in ['north-america', 'south-america', 'europe', 'asia', 'africa', 'oceania']:
            # Next part is country
            if i + 1 < len(parts):
                country = parts[i + 1]
            # Part after that might be region (if not the filename)
            if i + 2 < len(parts) and parts[i + 2] != filename + '.poly':
                region = parts[i + 2]
            break
    
    # Fallback: use last two directories before filename
    if not country:
        if len(parts) >= 3:
            country = parts[-3]
            region = parts[-2]
        elif len(parts) >= 2:
            country = parts[-2]
    
    # Convert filename to human-readable name
    # golden_horseshoe → Golden Horseshoe
    name = filename.replace('_', ' ').replace('-', ' ').title()
    
    # Generate geouid in Geofabrik format
    if region:
        geouid = f"{country}-{region}-{filename}"
    else:
        geouid = f"{country}-{filename}"
    
    return {
        'country': country,
        'region': region,
        'name': name,
        'geouid': geouid
    }
