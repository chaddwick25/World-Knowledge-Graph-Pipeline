"""Factor-table availability view (the MapQA G4 check).

Extracted from ``worldkg_nca/views/search.py`` (monolith split, Phase 5 of
PIPELINE_CONTROL_PLANE_AND_CLEANUP_PLAN).
"""

from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from core.services.planet_init.osm_wikidata_resolver import resolve_iso_code
from worldkg_nca.snapshot_utils import get_latest_snapshot_id


@api_view(["GET"])
def factor_availability(request):
    """Factor-table coverage for a (country, snapshot) — the G4 check.

    GET /api/nca/factor-availability/?country_code=BZ&snapshot_date=2025_12_31

    Returns booleans per latent-space table so an agent can verify data
    availability BEFORE proposing a branch that would fail G4:
        {country_code, snapshot_date,
         spectral, drift, amenity_embeddings, entity_embeddings}
    """
    country_code = (request.GET.get("country_code") or "").strip()
    snapshot_date = request.GET.get("snapshot_date")
    if not country_code:
        return Response({"error": "country_code required"},
                        status=status.HTTP_400_BAD_REQUEST)
    # Normalize to ISO first (override-aware — handles region names like
    # "Ireland And Northern Ireland" → IE) so the G4 booleans match the
    # factor rows' ISO country_code.
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

    def exists(table, where, params):
        cur.execute(f"SELECT 1 FROM {table} WHERE {where} LIMIT 1", params)
        return cur.fetchone() is not None

    spectral = exists(
        "factor_spectral_node_metric",
        "country_code = %s AND snapshot_id = %s",
        [country_code, snapshot_date],
    )
    # DriftNodeMetric keys on snapshot_to_id (no snapshot_id column).
    drift = exists(
        "factor_drift_node_metric",
        "country_code = %s AND snapshot_to_id = %s",
        [country_code, snapshot_date],
    )
    amenity_embeddings = exists(
        "factor_amenity_embedding", "1 = 1", [],
    )
    entity_embeddings = exists(
        "semantic_search_osmentity",
        "country_code = %s AND snapshot_id = %s "
        "AND gv_tags_embedding IS NOT NULL",
        [country_code, snapshot_date],
    )
    return Response({
        "country_code": country_code,
        "snapshot_date": snapshot_date,
        "spectral": spectral,
        "drift": drift,
        "amenity_embeddings": amenity_embeddings,
        "entity_embeddings": entity_embeddings,
    })
