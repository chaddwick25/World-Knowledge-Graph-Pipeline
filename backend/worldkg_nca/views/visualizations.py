"""Visualization endpoints for the deck.gl integration (v2 Phase 1b).

DECKGL_VISUALIZATION_INTEGRATION_PLAN_V2.md Phase 1: the hierarchical
TextLayer needs two data sources —

1. ``class-centroids`` — WKG class labels aggregated to their geometric
   centroid (one row per class per (country, snapshot)), sized by entity
   count.  These render at low zoom in the deck.gl TextLayer.

2. ``worldkg_entities_by_class`` (extended in ``enrichment.py``) — the
   per-entity labels for high zoom, now scoped by ``country_code`` +
   ``snapshot_date`` with a raised ``limit`` cap.

Both queries are partition-pruned by ``snapshot_id`` + ``country_code``
(the composite index on ``semantic_search_osmentity``), so they stay on
one partition at country scale.
"""

import re

from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from core.services.planet_init.osm_wikidata_resolver import resolve_iso_code
from worldkg_nca.snapshot_utils import get_latest_snapshot_id

# Cap on the number of class-centroid rows returned.  The plan limits the
# label view to the top classes per country (a couple of hundred at most).
CLASS_CENTROIDS_LIMIT = 200

# Display labels for the OSM GIS key classes.  The generic pluralizer
# produces nonsense for these ("Naturals", "Powers", "Amenitys"), and they
# are exactly the classes the label view surfaces at low zoom.
# Keys are the camelCase-split, title-joined class names (see
# humanize_class_label); values are the display labels.
CLASS_LABEL_OVERRIDES = {
    "Amenity": "Amenities",
    "Natural": "Natural Features",
    "Power": "Power Lines",
    "Highway": "Highways",
    "Waterway": "Waterways",
    "Shop": "Shops",
    "Building": "Buildings",
    "Landuse": "Land Use",
    "Man Made": "Man-Made",
    "Place": "Places",
    "Route": "Routes",
    "Sport": "Sports",
    "Office": "Offices",
    "Leisure": "Leisure",
    "Emergency": "Emergency",
    "Military": "Military",
    "Tourism": "Tourism",
    "Historic": "Historic",
    "Railway": "Railway",
    "Aeroway": "Aeroway",
    "Aerialway": "Aerialway",
    "Public Transport": "Public Transport",
    "Craft": "Crafts",
    "Healthcare": "Healthcare",
    "Club": "Clubs",
    "Barrier": "Barriers",
    "Boundary": "Boundary",
    "Bridge": "Bridges",
    "Telecom": "Telecom",
    "Tower": "Towers",
    "Junction": "Junctions",
    "Beach": "Beaches",
    "Island": "Islands",
    "Mountain": "Mountains",
    "Peak": "Peaks",
    "Forest": "Forests",
    "Lake": "Lakes",
    "River": "Rivers",
    "Stream": "Streams",
    "Wetland": "Wetlands",
    "Glacier": "Glaciers",
}


def _pluralize(word: str) -> str:
    """Naive English pluralization (deterministic, no library).

    y→ies when preceded by a consonant, es for sibilant endings, else +s.
    Overrides in CLASS_LABEL_OVERRIDES handle the irregular GIS classes.
    """
    if word.endswith(("s", "x", "z", "ch", "sh")):
        return word + "es"
    if word.endswith("y") and len(word) > 1 and word[-2] not in "aeiou":
        return word[:-1] + "ies"
    return word + "s"


def humanize_class_label(wkg_class: str) -> str:
    """Turn a ``wkgs:`` class name into a pluralized display label.

    ``wkgs:BusStation`` -> ``"Bus Stations"``, ``wkgs:Cafe`` -> ``"Cafes"``,
    ``wkgs:Natural`` -> ``"Natural Features"`` (override), ``wkgs:Amenity``
    -> ``"Amenities"``.  Deterministic.  Used only for display; the raw
    ``wkg_class`` is always carried alongside.
    """
    name = wkg_class.split(":", 1)[-1]
    words = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name).replace("_", " ").split()
    if not words:
        return wkg_class
    words = [w[:1].upper() + w[1:] for w in words]
    joined = " ".join(words)
    if joined in CLASS_LABEL_OVERRIDES:
        return CLASS_LABEL_OVERRIDES[joined]
    words[-1] = _pluralize(words[-1])
    return " ".join(words)


@api_view(["GET"])
def worldkg_class_centroids(request):
    """WKG class labels aggregated to their centroid for a (country, snapshot).

    GET /api/nca/class-centroids/?country_code=JM&snapshot_date=2025_06_10

    Returns:
        {
            "country_code": str,
            "snapshot_date": str,
            "classes": [
                {"label": "Restaurants", "wkg_class": "wkgs:Restaurant",
                 "centroid": [-76.8, 18.0], "count": 1240},
                ...
            ]
        }

    One row per ``wkg_class``, ordered by entity count descending (top
    ``CLASS_CENTROIDS_LIMIT``).  ``centroid`` is the geometric centroid of
    the class's entity points (PostGIS ``ST_Centroid(ST_Collect(geom))``).
    Entities without geometry or without a class are excluded.
    """
    country_code = (request.GET.get("country_code") or "").strip()
    snapshot_date = request.GET.get("snapshot_date")
    if not country_code:
        return Response(
            {"error": "country_code required"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    # Normalize to ISO first (override-aware — handles region names like
    # "Ireland And Northern Ireland" → IE), mirroring factor_availability.
    try:
        resolved = resolve_iso_code(country_code)
        if resolved:
            country_code = resolved
    except Exception:
        pass
    country_code = country_code.upper()
    if not snapshot_date:
        snapshot_date = get_latest_snapshot_id()

    from django.db import connections

    cur = connections["vectors"].cursor()
    cur.execute(
        """
        SELECT
            wkg_class,
            ST_X(ST_Centroid(ST_Collect(geom))) AS lon,
            ST_Y(ST_Centroid(ST_Collect(geom))) AS lat,
            COUNT(*) AS entity_count
        FROM semantic_search_osmentity
        WHERE country_code = %s AND snapshot_id = %s
          AND geom IS NOT NULL AND wkg_class IS NOT NULL
        GROUP BY wkg_class
        ORDER BY entity_count DESC
        LIMIT %s
        """,
        [country_code, snapshot_date, CLASS_CENTROIDS_LIMIT],
    )
    rows = cur.fetchall()

    classes = [
        {
            "label": humanize_class_label(row[0]),
            "wkg_class": row[0],
            "centroid": [round(row[1], 6), round(row[2], 6)],
            "count": row[3],
        }
        for row in rows
    ]

    return Response({
        "country_code": country_code,
        "snapshot_date": snapshot_date,
        "classes": classes,
    })
