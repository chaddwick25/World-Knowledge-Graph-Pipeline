from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response
from django.core.paginator import Paginator
from django.db.models import FloatField, Q
from django.db.models.expressions import RawSQL
from django.contrib.gis.geos import Polygon, Point
from django.contrib.gis.db.models.functions import Distance
from django.conf import settings
import math

from worldkg_nca.models import OsmEntity, PrecomputedLinkCandidate
from extraction.models import ProjectionWeightAsset
from semantic_search.services.worldkg_enrichment_service import get_worldkg_enrichment_service
from worldkg_nca.services.ontology_service import get_worldkg_ontology_service
from semantic_search.services.worldkg_drift_service import get_worldkg_drift_service
from api.models import TemporalSnapshot, WorldKGClassDrift
from semantic_search.services.fasttext_service import FastTextEmbeddingService
from extraction.services.osm_wikidata_resolver import resolve_country_bbox, get_country_by_name
from worldkg_nca.services.link_candidate_service import WorldKGLinkCandidateService
from extraction.services.osm_wikidata_resolver import resolve_iso_code
from worldkg_nca.snapshot_utils import get_latest_snapshot_id


@api_view(["POST"])
def worldkg_link_candidates(request):
    osm_type = request.data.get("osm_type")
    osm_id = request.data.get("osm_id")
    country_code = request.data.get("country_code") or request.data.get("country")
    top_k = request.data.get("top_k", 10)
    # Phase 6: scope to snapshot partition (falls back to latest for monolith)
    snapshot_id = request.data.get("snapshot_id") or get_latest_snapshot_id()

    if not osm_type or osm_id is None:
        return Response({"error": "osm_type and osm_id required"}, status=status.HTTP_400_BAD_REQUEST)

    try:
        osm_id_int = int(osm_id)
    except (TypeError, ValueError):
        return Response({"error": "osm_id must be an integer"}, status=status.HTTP_400_BAD_REQUEST)

    try:
        qs = OsmEntity.objects.using("vectors").filter(osm_type=osm_type, osm_id=osm_id_int)
        if snapshot_id:
            qs = qs.filter(snapshot_id=snapshot_id)
        entity = qs.get()
    except OsmEntity.DoesNotExist:
        return Response({"error": f"Entity {osm_type}/{osm_id_int} not found"}, status=status.HTTP_404_NOT_FOUND)

    try:
        top_k_int = int(top_k)
    except (TypeError, ValueError):
        top_k_int = 10

    service = WorldKGLinkCandidateService(country_code=country_code)

    enable_precomputed = getattr(settings, "ENABLE_PRECOMPUTED_LINK_CANDIDATES", True)

    projection_weights = None
    candidates = None

    if enable_precomputed:
        w_geo, w_name, w_class, weights_info = service._get_projection_weights()
        asset_id = weights_info.get("asset_id")

        pre = PrecomputedLinkCandidate.objects.using("vectors").filter(
            osm_type=osm_type,
            osm_id=osm_id_int,
            projection_asset_id=asset_id,
        ).first()

        if pre is not None:
            candidates = pre.candidates or []
            if top_k_int and top_k_int > 0:
                candidates = candidates[:top_k_int]
            projection_weights = {
                "w_geo": w_geo,
                "w_name": w_name,
                "w_class": w_class,
                **weights_info,
            }

    if candidates is None or projection_weights is None:
        result = service.get_alignment_candidates(entity, top_k=top_k_int)
        projection_weights = result.get("projection_weights")
        candidates = result.get("candidates", [])

        if enable_precomputed:
            asset_id = None
            if projection_weights is not None:
                asset_id = projection_weights.get("asset_id")

            PrecomputedLinkCandidate.objects.using("vectors").update_or_create(
                osm_type=osm_type,
                osm_id=osm_id_int,
                projection_asset_id=asset_id,
                defaults={
                    "country_code": (country_code or "").strip(),
                    "candidates": candidates,
                },
            )

    response_payload = {
        "osm_type": osm_type,
        "osm_id": osm_id_int,
        "country_code": country_code,
        "projection_weights": projection_weights,
        "candidates": candidates,
    }

    return Response(response_payload)


@api_view(["POST"])
def worldkg_apply_link(request):
    from igea.models import EntityAlignment

    osm_type = request.data.get("osm_type")
    osm_id = request.data.get("osm_id")
    wikidata_uri = request.data.get("wikidata_uri")
    wikidata_id = request.data.get("wikidata_id")
    label = request.data.get("label")
    confidence = request.data.get("confidence", 1.0)
    # Phase 6: scope to snapshot partition (falls back to latest for monolith)
    snapshot_id = request.data.get("snapshot_id") or get_latest_snapshot_id()

    if not osm_type or osm_id is None:
        return Response({"error": "osm_type and osm_id required"}, status=status.HTTP_400_BAD_REQUEST)

    if not wikidata_uri and not wikidata_id:
        return Response({"error": "wikidata_uri or wikidata_id required"}, status=status.HTTP_400_BAD_REQUEST)

    try:
        osm_id_int = int(osm_id)
    except (TypeError, ValueError):
        return Response({"error": "osm_id must be an integer"}, status=status.HTTP_400_BAD_REQUEST)

    if not wikidata_uri and wikidata_id:
        if wikidata_id.startswith("http://") or wikidata_id.startswith("https://"):
            wikidata_uri = wikidata_id
        else:
            wikidata_uri = f"https://www.wikidata.org/entity/{wikidata_id}"

    try:
        qs = OsmEntity.objects.using("vectors").filter(osm_type=osm_type, osm_id=osm_id_int)
        if snapshot_id:
            qs = qs.filter(snapshot_id=snapshot_id)
        entity = qs.get()
    except OsmEntity.DoesNotExist:
        return Response({"error": f"Entity {osm_type}/{osm_id_int} not found"}, status=status.HTTP_404_NOT_FOUND)

    tags = dict(entity.tags or {})
    uri_parts = wikidata_uri.rstrip("/").split("/") if wikidata_uri else []
    qid = uri_parts[-1] if uri_parts else None
    if qid:
        tags["wikidata"] = qid
    entity.tags = tags
    entity.save(using="vectors")

    EntityAlignment.objects.update_or_create(
        osm_type=osm_type,
        osm_id=osm_id_int,
        wikidata_uri=wikidata_uri,
        defaults={
            "wikidata_label": label,
            "alignment_method": "SEED",
            "confidence": float(confidence),
        },
    )

    return Response(
        {
            "status": "linked",
            "osm_type": osm_type,
            "osm_id": osm_id_int,
            "wikidata_uri": wikidata_uri,
            "wikidata_id": qid,
        }
    )


