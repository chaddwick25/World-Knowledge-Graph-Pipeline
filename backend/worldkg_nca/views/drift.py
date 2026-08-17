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
from core.models import ProjectionWeightAsset
from semantic_search.services.worldkg_enrichment_service import get_worldkg_enrichment_service
from worldkg_nca.services.ontology_service import get_worldkg_ontology_service
from semantic_search.services.worldkg_drift_service import get_worldkg_drift_service
from api.models import Snapshot, WorldKGClassDrift
from semantic_search.services.fasttext_service import FastTextEmbeddingService
from core.services.planet_init.osm_wikidata_resolver import resolve_country_bbox, get_country_by_name
from worldkg_nca.services.link_candidate_service import WorldKGLinkCandidateService
from core.services.planet_init.osm_wikidata_resolver import resolve_iso_code
from worldkg_nca.snapshot_utils import get_latest_snapshot_id


@api_view(['POST'])
def worldkg_compute_fingerprint(request):
    """
    Compute WorldKG class fingerprint for a snapshot.
    
    POST /api/worldkg/fingerprint/compute/
    Body: {
        "snapshot_id": uuid
    }
    
    Returns:
        {
            "id": uuid,
            "region": str,
            "snapshot_id": uuid,
            "class_distribution": {...},
            "mean_depth": float,
            "shannon_entropy": float,
            "unique_classes": int,
            "total_entities": int
        }
    """
    snapshot_id = request.data.get('snapshot_id')
    
    if not snapshot_id:
        return Response(
            {"error": "snapshot_id required"},
            status=status.HTTP_400_BAD_REQUEST
        )
    
    try:
        snapshot = Snapshot.objects.get(id=snapshot_id)
    except Snapshot.DoesNotExist:
        return Response(
            {"error": f"Snapshot {snapshot_id} not found"},
            status=status.HTTP_404_NOT_FOUND
        )
    
    drift_service = get_worldkg_drift_service()
    fingerprint = drift_service.compute_fingerprint(snapshot)
    
    return Response({
        "id": str(fingerprint.id),
        "region": fingerprint.region,
        "snapshot_id": str(fingerprint.snapshot.id),
        "class_distribution": fingerprint.class_distribution,
        "mean_depth": fingerprint.mean_depth,
        "depth_std": fingerprint.depth_std,
        "shannon_entropy": fingerprint.shannon_entropy,
        "simpson_index": fingerprint.simpson_index,
        "unique_classes": fingerprint.unique_classes,
        "top_5_classes": fingerprint.top_5_classes,
        "total_entities": fingerprint.total_entities
    })


@api_view(['POST'])
def worldkg_compute_drift(request):
    """
    Compute WorldKG class drift between two snapshots.
    
    POST /api/worldkg/drift/compute/
    Body: {
        "snapshot_from_id": uuid,
        "snapshot_to_id": uuid,
        "bbox": [min_lon, min_lat, max_lon, max_lat] (optional)
    }
    
    Returns:
        {
            "id": uuid,
            "region": str,
            "kl_divergence": float,
            "js_divergence": float,
            "depth_delta": float,
            "drift_magnitude": str,
            "new_classes": [...],
            "lost_classes": [...],
            "dominant_shift": {...}
        }
    """
    snapshot_from_id = request.data.get('snapshot_from_id')
    snapshot_to_id = request.data.get('snapshot_to_id')
    bbox = request.data.get('bbox')
    
    if not snapshot_from_id or not snapshot_to_id:
        return Response(
            {"error": "snapshot_from_id and snapshot_to_id required"},
            status=status.HTTP_400_BAD_REQUEST
        )
    
    try:
        snapshot_from = Snapshot.objects.get(id=snapshot_from_id)
        snapshot_to = Snapshot.objects.get(id=snapshot_to_id)
    except Snapshot.DoesNotExist as e:
        return Response(
            {"error": str(e)},
            status=status.HTTP_404_NOT_FOUND
        )
    
    drift_service = get_worldkg_drift_service()
    drift = drift_service.compute_drift(
        snapshot_from,
        snapshot_to,
        bbox=bbox
    )
    
    return Response({
        "id": str(drift.id),
        "region": drift.region,
        "snapshot_from_id": str(drift.snapshot_from.id),
        "snapshot_to_id": str(drift.snapshot_to.id),
        "kl_divergence": drift.kl_divergence,
        "js_divergence": drift.js_divergence,
        "depth_delta": drift.depth_delta,
        "drift_magnitude": drift.drift_magnitude,
        "new_classes": drift.new_classes,
        "lost_classes": drift.lost_classes,
        "dominant_shift": drift.dominant_shift
    })


@api_view(['GET'])
def worldkg_drift_list(request):
    """
    List WorldKG class drift records.
    
    GET /api/worldkg/drift/?region={str}&magnitude={low|medium|high|extreme}
    
    Returns paginated list of drift records.
    """
    region = request.GET.get('region')
    magnitude = request.GET.get('magnitude')
    page = int(request.GET.get('page', 1))
    page_size = int(request.GET.get('page_size', 20))
    
    drifts = WorldKGClassDrift.objects.all()
    
    if region:
        drifts = drifts.filter(region=region)
    if magnitude:
        drifts = drifts.filter(drift_magnitude=magnitude)
    
    paginator = Paginator(drifts, page_size)
    page_obj = paginator.get_page(page)
    
    results = [{
        "id": str(d.id),
        "region": d.region,
        "snapshot_from_timestamp": d.snapshot_from.timestamp.isoformat(),
        "snapshot_to_timestamp": d.snapshot_to.timestamp.isoformat(),
        "js_divergence": d.js_divergence,
        "drift_magnitude": d.drift_magnitude,
        "depth_delta": d.depth_delta
    } for d in page_obj]
    
    return Response({
        "count": paginator.count,
        "page": page,
        "page_size": page_size,
        "results": results
    })


