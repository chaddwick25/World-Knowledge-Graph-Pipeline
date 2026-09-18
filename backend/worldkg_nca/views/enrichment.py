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
from api.models import WorldKGClassDrift
from semantic_search.services.fasttext_service import FastTextEmbeddingService
from core.services.planet_init.osm_wikidata_resolver import resolve_country_bbox, get_country_by_name
from worldkg_nca.services.link_candidate_service import WorldKGLinkCandidateService
from core.services.planet_init.osm_wikidata_resolver import resolve_iso_code
from worldkg_nca.snapshot_utils import get_latest_snapshot_id


@api_view(['POST'])
def worldkg_enrich_entity(request):
    """
    Enrich a single OSM entity with WorldKG classification.
    
    POST /api/worldkg/enrich/entity/
    Body: {
        "osm_type": "node|way|relation",
        "osm_id": int,
        "use_sparql": bool (optional)
    }
    
    Returns:
        {
            "osm_type": str,
            "osm_id": int,
            "wkg_class": str,
            "wkg_superclasses": [...],
            "wikidata_uri": str | null,
            "wkg_depth": int,
            "method": "sparql" | "local"
        }
    """
    osm_type = request.data.get('osm_type')
    osm_id = request.data.get('osm_id')
    use_sparql = request.data.get('use_sparql', False)
    # Phase 6: scope to snapshot partition (falls back to latest for monolith)
    snapshot_id = request.data.get('snapshot_id') or get_latest_snapshot_id()

    if not osm_type or not osm_id:
        return Response(
            {"error": "osm_type and osm_id required"},
            status=status.HTTP_400_BAD_REQUEST
        )

    try:
        qs = OsmEntity.objects.using('vectors').filter(
            osm_type=osm_type,
            osm_id=osm_id,
        )
        if snapshot_id:
            qs = qs.filter(snapshot_id=snapshot_id)
        entity = qs.get()
    except OsmEntity.DoesNotExist:
        return Response(
            {"error": f"Entity {osm_type}/{osm_id} not found"},
            status=status.HTTP_404_NOT_FOUND
        )
    
    enrichment_service = get_worldkg_enrichment_service()
    result = enrichment_service.enrich_entity(entity, use_sparql=use_sparql)
    
    if not result:
        return Response(
            {"error": "Could not classify entity. Tags may not match WorldKG ontology."},
            status=status.HTTP_422_UNPROCESSABLE_ENTITY
        )
    
    # Update entity
    entity.wkg_class = result['wkg_class']
    entity.wkg_superclasses = result['wkg_superclasses']
    entity.wikidata_uri = result.get('wikidata_uri')
    entity.wkg_depth = result['wkg_depth']
    entity.save(using='vectors')
    
    return Response({
        "osm_type": osm_type,
        "osm_id": osm_id,
        **result
    })


# Cap on the entities endpoint limit — the deck.gl label view fetches up to
# 5000 entities per class (DECKGL_VISUALIZATION_INTEGRATION_PLAN_V2.md §3.2.7).
ENTITIES_LIMIT_MAX = 5000


@api_view(['GET'])
def worldkg_entities_by_class(request):
    """
    Get entities belonging to a WorldKG class.
    
    GET /api/nca/entities/?class={class_name}&include_subclasses={bool}&limit={int}
        &country_code={ISO}&snapshot_date={YYYY_MM_DD}
    
    `country_code` and `snapshot_date` (optional) scope the query to a single
    snapshot partition — required for country-scale label views.  `limit` is
    clamped to [1, 5000].
    
    Returns:
        {
            "class": str,
            "include_subclasses": bool,
            "country_code": str | null,
            "snapshot_date": str | null,
            "total": int,
            "entities": [...]
        }
    """
    class_name = request.GET.get('class')
    include_subclasses = request.GET.get('include_subclasses', 'true').lower() == 'true'
    try:
        limit = int(request.GET.get('limit', 100))
    except (TypeError, ValueError):
        limit = 100
    limit = max(1, min(limit, ENTITIES_LIMIT_MAX))
    country_code = request.GET.get('country_code')
    snapshot_date = request.GET.get('snapshot_date')
    
    if not class_name:
        return Response(
            {"error": "class parameter required"},
            status=status.HTTP_400_BAD_REQUEST
        )
    
    if country_code:
        # Normalize to ISO first (override-aware), mirroring factor_availability.
        try:
            resolved = resolve_iso_code(country_code)
            if resolved:
                country_code = resolved
        except Exception:
            pass
        country_code = country_code.upper()
    
    enrichment_service = get_worldkg_enrichment_service()
    entities = enrichment_service.get_entities_by_class(
        class_name,
        include_subclasses=include_subclasses,
        limit=limit,
        country_code=country_code or None,
        snapshot_date=snapshot_date or None,
    )
    
    entities_data = [{
        "osm_type": e.osm_type,
        "osm_id": e.osm_id,
        "tags": e.tags,
        "wkg_class": e.wkg_class,
        "wkg_depth": e.wkg_depth,
        "geom": {
            "lat": e.geom.y if e.geom else None,
            "lon": e.geom.x if e.geom else None
        } if e.geom else None
    } for e in entities]
    
    return Response({
        "class": class_name,
        "include_subclasses": include_subclasses,
        "country_code": country_code or None,
        "snapshot_date": snapshot_date or None,
        "total": len(entities_data),
        "entities": entities_data
    })


@api_view(['GET'])
def worldkg_entity_detail(request, osm_type, osm_id):
    """
    Get detailed information for a single WorldKG-enriched OSM entity.
    
    GET /api/nca/entities/detail/{osm_type}/{osm_id}/
    
    Returns:
        {
            "osm_type": str,
            "osm_id": int,
            "tags": {...},
            "wkg_class": str,
            "wkg_superclasses": [...],
            "wkg_depth": int,
            "wikidata_uri": str,
            "geom": {"lat": float, "lon": float} | null,
            "osm_url": str,
            "has_nle": bool
        }
    """
    # Phase 6: scope to snapshot partition (falls back to latest for monolith)
    snapshot_id = request.query_params.get('snapshot_id') or get_latest_snapshot_id()
    try:
        qs = OsmEntity.objects.using('vectors').filter(
            osm_type=osm_type,
            osm_id=osm_id,
        )
        if snapshot_id:
            qs = qs.filter(snapshot_id=snapshot_id)
        entity = qs.get()
    except OsmEntity.DoesNotExist:
        return Response(
            {"error": f"Entity {osm_type}/{osm_id} not found"},
            status=status.HTTP_404_NOT_FOUND
        )

    return Response({
        "osm_type": entity.osm_type,
        "osm_id": entity.osm_id,
        "tags": entity.tags,
        "wkg_class": entity.wkg_class,
        "wkg_superclasses": entity.wkg_superclasses,
        "wkg_depth": entity.wkg_depth,
        "wikidata_uri": entity.wikidata_uri,
        "geom": {
            "lat": entity.geom.y if entity.geom else None,
            "lon": entity.geom.x if entity.geom else None
        } if entity.geom else None,
        "osm_url": entity.osm_url,
        "has_nle": entity.gv_nle_trained,
        "gv_tags_version": entity.gv_tags_version
    })


