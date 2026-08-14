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


