"""Resolve a Wikidata QID to a bounding-box polygon for subdivision filtering.

Used by search endpoints to support ``subdivision_qid`` (e.g. ``Q260009`` for
Managua, Nicaragua) as an alternative to a country-level bbox.  The QID is
looked up in ``SubgraphProfile`` which is populated during pipeline runs with
the Wikidata P150/P402 hierarchy + bbox from the subgraph poly file.
"""

import logging
from typing import Optional, Tuple

from django.contrib.gis.geos import Polygon

logger = logging.getLogger(__name__)


def resolve_subdivision_bbox(
    subdivision_qid: str,
) -> Optional[Tuple[float, float, float, float]]:
    """Resolve a Wikidata QID to a ``(min_lon, min_lat, max_lon, max_lat)`` bbox.

    Looks up ``SubgraphProfile`` by ``wikidata_id`` and returns the stored bbox.
    Returns ``None`` if the QID is not found or the bbox is not populated.
    """
    if not subdivision_qid:
        return None

    qid = subdivision_qid.strip().upper()
    if not qid.startswith("Q"):
        # Allow callers to pass a bare numeric ID
        qid = f"Q{qid}"

    try:
        from core.models import SubgraphProfile

        sg = SubgraphProfile.objects.filter(wikidata_id=qid).first()
        if not sg:
            logger.warning("SubgraphProfile not found for wikidata_id=%s", qid)
            return None

        if (
            sg.bbox_min_lon is None
            or sg.bbox_min_lat is None
            or sg.bbox_max_lon is None
            or sg.bbox_max_lat is None
        ):
            logger.warning(
                "SubgraphProfile %s (qid=%s) has no bbox populated",
                sg.name, qid,
            )
            return None

        return (
            sg.bbox_min_lon,
            sg.bbox_min_lat,
            sg.bbox_max_lon,
            sg.bbox_max_lat,
        )
    except Exception as exc:
        logger.error("Error resolving subdivision bbox for %s: %s", qid, exc)
        return None


def resolve_subdivision_polygon(
    subdivision_qid: str,
) -> Optional[Polygon]:
    """Resolve a Wikidata QID to a GEOS ``Polygon`` suitable for ``geom__within``.

    Returns ``None`` if the bbox cannot be resolved.
    """
    bbox = resolve_subdivision_bbox(subdivision_qid)
    if not bbox:
        return None
    min_lon, min_lat, max_lon, max_lat = bbox
    return Polygon.from_bbox((min_lon, min_lat, max_lon, max_lat))


def resolve_subdivision_country_code(
    subdivision_qid: str,
) -> Optional[str]:
    """Resolve a Wikidata QID to the parent country's ISO-2 code.

    Useful when the caller provides ``subdivision_qid`` but not
    ``country_code`` — the country can be inferred from the
    ``SubgraphProfile.country_profile`` relation.
    """
    if not subdivision_qid:
        return None

    qid = subdivision_qid.strip().upper()
    if not qid.startswith("Q"):
        qid = f"Q{qid}"

    try:
        from core.models import SubgraphProfile

        sg = SubgraphProfile.objects.select_related("country_profile").filter(
            wikidata_id=qid,
        ).first()
        if not sg or not sg.country_profile:
            return None
        return sg.country_profile.iso2
    except Exception as exc:
        logger.error("Error resolving country for subdivision %s: %s", qid, exc)
        return None
