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
    availability BEFORE proposing a branch that would fail G4, plus the
    class-drift capability blocks the Doppelganger agent narrates:
        {country_code, snapshot_date,
         spectral, drift, amenity_embeddings, entity_embeddings,
         class_profile: {available, top_classes, shannon_entropy, ...},
         class_drift: {available, pairs, latest: {from, to,
                         js_divergence, magnitude}}}
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

    # Class-drift capability (ISSUE-class-drift-capability): the osmsnapshot
    # tables live on the default DB, so these read through the ORM. The
    # fingerprint is the per-snapshot ontology profile — present on every
    # processed country after the Step 6 hook; class_drift needs a
    # snapshot pair, so it reports available=false on single-snapshot
    # countries until compute_class_drift has a pair to fill.
    from osmsnapshot.models import (
        Snapshot,
        WorldKGClassDrift,
        WorldKGClassFingerprint,
    )
    snapshot_row = (
        Snapshot.objects
        .filter(country_code__iexact=country_code, snapshot_date=snapshot_date)
        .first()
    )
    fp_row = (
        WorldKGClassFingerprint.objects.filter(snapshot=snapshot_row).first()
        if snapshot_row else None
    )
    class_profile = {"available": fp_row is not None}
    if fp_row is not None:
        class_profile.update({
            "top_classes": fp_row.top_5_classes,
            "shannon_entropy": fp_row.shannon_entropy,
            "unique_classes": fp_row.unique_classes,
            "total_entities": fp_row.total_entities,
        })

    drift_qs = WorldKGClassDrift.objects.filter(region__iexact=country_code)
    latest_drift = drift_qs.order_by("snapshot_to__snapshot_date").last()
    class_drift = {"available": latest_drift is not None,
                   "pairs": drift_qs.count()}
    if latest_drift is not None:
        class_drift["latest"] = {
            "from": latest_drift.snapshot_from.snapshot_date,
            "to": latest_drift.snapshot_to.snapshot_date,
            "js_divergence": latest_drift.js_divergence,
            "magnitude": latest_drift.drift_magnitude,
        }

    return Response({
        "country_code": country_code,
        "snapshot_date": snapshot_date,
        "spectral": spectral,
        "drift": drift,
        "amenity_embeddings": amenity_embeddings,
        "entity_embeddings": entity_embeddings,
        "class_profile": class_profile,
        "class_drift": class_drift,
    })
