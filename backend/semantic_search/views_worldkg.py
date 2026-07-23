from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response
from django.core.paginator import Paginator

from worldkg_nca.models import OsmEntity
from worldkg_nca.services.enrichment_service import get_worldkg_enrichment_service
from worldkg_nca.services.ontology_service import get_worldkg_ontology_service
from api.models import TemporalSnapshot, WorldKGClassDrift, WorldKGClassFingerprint
from semantic_search.services.worldkg_drift_service import get_worldkg_drift_service


@api_view(['GET'])
def worldkg_ontology_info(request):
    """
    Get WorldKG ontology information.
    
    GET /api/worldkg/ontology/info/
    
    Returns:
        {
            "total_classes": int,
            "sample_classes": [...],
            "max_depth": int
        }
    """
    ontology = get_worldkg_ontology_service()
    all_classes = ontology.get_all_classes()
    
    if not all_classes:
        return Response(
            {"error": "WorldKG ontology not loaded. Use management command to load."},
            status=status.HTTP_503_SERVICE_UNAVAILABLE
        )
    
    # Get depth distribution
    depths = [ontology.get_depth(cls) for cls in list(all_classes)[:100]]
    max_depth = max(depths) if depths else 0
    
    # Sample classes
    sample = []
    for cls in list(all_classes)[:10]:
        sample.append({
            "class": cls,
            "depth": ontology.get_depth(cls),
            "superclasses": ontology.get_superclasses(cls),
            "canonical_tags": ontology.get_canonical_tags(cls)
        })
    
    return Response({
        "total_classes": len(all_classes),
        "sample_classes": sample,
        "max_depth": max_depth
    })


@api_view(['GET'])
def worldkg_class_hierarchy(request, class_name):
    """
    Get class hierarchy for a specific WorldKG class.
    
    GET /api/worldkg/ontology/class/{class_name}/
    
    Returns:
        {
            "class": str,
            "depth": int,
            "superclasses": [...],
            "subclasses": [...],
            "canonical_tags": {...}
        }
    """
    ontology = get_worldkg_ontology_service()
    
    if class_name not in ontology.get_all_classes():
        return Response(
            {"error": f"Class '{class_name}' not found in ontology"},
            status=status.HTTP_404_NOT_FOUND
        )
    
    return Response({
        "class": class_name,
        "depth": ontology.get_depth(class_name),
        "superclasses": ontology.get_superclasses(class_name),
        "subclasses": ontology.get_subclasses(class_name, recursive=False),
        "all_subclasses": ontology.get_subclasses(class_name, recursive=True),
        "canonical_tags": ontology.get_canonical_tags(class_name)
    })


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
    
    if not osm_type or not osm_id:
        return Response(
            {"error": "osm_type and osm_id required"},
            status=status.HTTP_400_BAD_REQUEST
        )
    
    try:
        entity = OsmEntity.objects.using('vectors').get(
            osm_type=osm_type,
            osm_id=osm_id
        )
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


@api_view(['GET'])
def worldkg_entities_by_class(request):
    """
    Get entities belonging to a WorldKG class.
    
    GET /api/worldkg/entities/?class={class_name}&include_subclasses={bool}&limit={int}
    
    Returns:
        {
            "class": str,
            "include_subclasses": bool,
            "total": int,
            "entities": [...]
        }
    """
    class_name = request.GET.get('class')
    include_subclasses = request.GET.get('include_subclasses', 'true').lower() == 'true'
    limit = int(request.GET.get('limit', 100))
    
    if not class_name:
        return Response(
            {"error": "class parameter required"},
            status=status.HTTP_400_BAD_REQUEST
        )
    
    enrichment_service = get_worldkg_enrichment_service()
    entities = enrichment_service.get_entities_by_class(
        class_name,
        include_subclasses=include_subclasses,
        limit=limit
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
        "total": len(entities_data),
        "entities": entities_data
    })


@api_view(['GET'])
def worldkg_class_distribution(request):
    """
    Get WorldKG class distribution for a snapshot or region.
    
    GET /api/worldkg/distribution/?snapshot_id={uuid}&region={str}
    
    Returns:
        {
            "snapshot_id": str | null,
            "region": str | null,
            "distribution": {class: count, ...},
            "total_entities": int
        }
    """
    snapshot_id = request.GET.get('snapshot_id')
    region = request.GET.get('region')
    
    enrichment_service = get_worldkg_enrichment_service()
    distribution = enrichment_service.get_class_distribution(
        snapshot_id=snapshot_id,
        region=region
    )
    
    total = sum(distribution.values())
    
    return Response({
        "snapshot_id": snapshot_id,
        "region": region,
        "distribution": distribution,
        "total_entities": total
    })


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
        snapshot = TemporalSnapshot.objects.get(id=snapshot_id)
    except TemporalSnapshot.DoesNotExist:
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
        snapshot_from = TemporalSnapshot.objects.get(id=snapshot_from_id)
        snapshot_to = TemporalSnapshot.objects.get(id=snapshot_to_id)
    except TemporalSnapshot.DoesNotExist as e:
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
