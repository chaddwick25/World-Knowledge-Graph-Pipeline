"""Graph / spectral / community / event query endpoints.

New endpoints from GRAPH_SPECTRAL_TEMPORAL_PLAN.md Phase 4. These use the
existing ``QueryExecutorService`` (which dispatches to the new
``SpectralAnalysisService`` / ``CommunityDetectionService`` / etc.) so the
control-plane parsing pipeline is reused.

Endpoints:
- ``POST /api/nca/spectral-query/``        — SPECTRAL-ANALYSIS
- ``POST /api/nca/temporal-query/``        — TEMPORAL-DRIFT
- ``POST /api/nca/community-query/``       — COMMUNITY-DETECT
- ``POST /api/nca/event-diffusion-query/`` — EVENT-DIFFUSION
"""

from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from core.services.planet_init.osm_wikidata_resolver import resolve_iso_code


def _normalize_country_code(country_code):
    if not country_code:
        return None
    try:
        return resolve_iso_code(country_code)
    except Exception:
        return country_code


def _run_template(request, template_name: str) -> Response:
    """Dispatch ``request`` to ``QueryExecutorService`` for a fixed template."""
    from semantic_search.services.query_parser_service import QueryParserService
    from semantic_search.services.query_executor_service import QueryExecutorService

    query_text = (request.data.get("query") or "").strip()
    country_code = _normalize_country_code(request.data.get("country_code"))
    snapshot_date = request.data.get("snapshot_date")

    if not query_text:
        return Response(
            {"error": "query required"}, status=status.HTTP_400_BAD_REQUEST,
        )

    # Parse to extract concepts (the template is forced — we don't rely on
    # the classifier, which would need retraining to recognize the new
    # templates).  Concepts + roles are still useful for the executor.
    parser = QueryParserService.get_instance()
    parsed = parser.parse(query_text)
    parsed["template"] = template_name

    result = QueryExecutorService.execute(
        parsed, country_code, snapshot_date, question=query_text
    )
    return Response({
        "query": query_text,
        "country_code": country_code,
        "template": template_name,
        "parsed": parsed,
        "result": result,
    })


@api_view(["POST"])
def spectral_query(request):
    """Spectral features for a region/snapshot.

    POST /api/nca/spectral-query/
    Body: {"query": "what is the spatial connectivity of this region",
           "country_code": "BZ", "snapshot_date": "2025_12_31"}
    """
    return _run_template(request, "SPECTRAL-ANALYSIS (#11)")


@api_view(["POST"])
def temporal_query(request):
    """Temporal drift between snapshots.

    POST /api/nca/temporal-query/
    Body: {"query": "how has this area changed",
           "country_code": "BZ", "snapshot_date": "2025_12_31"}
    """
    return _run_template(request, "TEMPORAL-DRIFT (#12)")


@api_view(["POST"])
def community_query(request):
    """Community detection + optional class filtering.

    POST /api/nca/community-query/
    Body: {"query": "where are the clusters of cafes",
           "country_code": "BZ", "snapshot_date": "2025_12_31"}
    """
    return _run_template(request, "COMMUNITY-DETECT (#13)")


@api_view(["POST"])
def event_diffusion_query(request):
    """Heat kernel diffusion from a source entity.

    POST /api/nca/event-diffusion-query/
    Body: {"query": "what is affected within 30 minutes of the airport",
           "country_code": "BZ", "snapshot_date": "2025_12_31"}
    """
    return _run_template(request, "EVENT-DIFFUSION (#14)")
