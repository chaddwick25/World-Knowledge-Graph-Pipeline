"""
Hybrid Search API Views - Dual Embedding Queries

Provides endpoints for:
1. Semantic search (GV-Tags only)
2. Spatial search (GV-NLE only)
3. Hybrid search (weighted combination of both)

Implements the GeoVectors dual embedding query patterns.
"""

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.db.models import FloatField
from django.db.models.expressions import RawSQL
from worldkg_nca.models import OsmEntity
from worldkg_nca.snapshot_utils import get_latest_snapshot_id
from semantic_search.services.fasttext_service import FastTextEmbeddingService
from worldkg_nca.services.ontology_service import get_worldkg_ontology_service
import logging
import time
import numpy as np

logger = logging.getLogger(__name__)


class SemanticSearchGVTagsView(APIView):
    """
    Semantic search using GV-Tags embeddings only.
    
    POST /api/semantic-search/gv-tags/
    {
        "query_tags": {"amenity": "cafe", "name": "starbucks"},
        "top_k": 10,
        "filters": {"osm_type": "node"}
    }
    
    Returns entities functionally similar to query (e.g., cafe → restaurant, bar)
    """
    
    def post(self, request):
        query_tags = request.data.get('query_tags', {})
        wkg_class = request.data.get('wkg_class')
        top_k = request.data.get('top_k', 10)
        filters = request.data.get('filters', {})
        # Phase 6: scope to snapshot partition (falls back to latest for monolith)
        snapshot_id = request.data.get('snapshot_id') or get_latest_snapshot_id()
        
        if not query_tags and wkg_class:
            ontology = get_worldkg_ontology_service()
            key = ontology.get_canonical_osm_key(wkg_class)
            val = ontology.get_canonical_osm_value(wkg_class)
            if key and val:
                query_tags = {key: val}
            elif key:
                query_tags = {key: 'yes'}
        
        if not query_tags:
            return Response(
                {'error': 'query_tags or wkg_class required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        start_time = time.time()
        
        try:
            # Convert tags to tag_counts format.
            # If query_tags looks like a raw OSM tag dict (keys like 'amenity', 'shop')
            # use the GeoVectors-preserving key/value tokenization. If it already
            # looks aggregated (keys such as 'amenity=cafe'), keep the legacy path.
            if any('=' in k for k in query_tags.keys()):
                tag_counts = {k: 1 for k in query_tags.keys()}
            else:
                tag_counts = FastTextEmbeddingService.build_tag_counts_from_osm_tags(query_tags)

            # Generate query embedding
            query_embedding = FastTextEmbeddingService.calculate_embedding(tag_counts)
            query_list = query_embedding.tolist()
            
            # Search using pgvector cosine distance
            results = OsmEntity.objects.using('vectors').filter(
                gv_tags_embedding__isnull=False
            )
            # Phase 6: scope to snapshot partition
            if snapshot_id:
                results = results.filter(snapshot_id=snapshot_id)

            # Apply filters
            if filters:
                results = results.filter(**filters)
            
            # Annotate with distance and order
            results = results.annotate(
                distance=RawSQL(
                    "gv_tags_embedding <=> %s::vector",
                    (query_list,),
                    output_field=FloatField()
                )
            ).order_by('distance')[:top_k * 15]  # Get more to account for duplicates (10x per entity)
            
            # Deduplicate by (osm_id, osm_type) - keep first occurrence
            seen_ids = set()
            unique_results = []
            for entity in results:
                entity_key = (entity.osm_id, entity.osm_type)
                if entity_key not in seen_ids:
                    seen_ids.add(entity_key)
                    unique_results.append(entity)
                    if len(unique_results) >= top_k:
                        break
            
            results = unique_results
            
            # Format response
            entities = []
            for entity in results:
                entities.append({
                    'osm_id': entity.osm_id,
                    'osm_type': entity.osm_type,
                    'tags': entity.tags,
                    'distance': entity.distance,
                    'similarity': 1.0 - entity.distance,  # Cosine similarity
                    'location': {
                        'lat': entity.geom.y if entity.geom else None,
                        'lon': entity.geom.x if entity.geom else None,
                    }
                })
            
            inference_time = (time.time() - start_time) * 1000
            
            return Response({
                'method': 'gv-tags',
                'query_tags': query_tags,
                'results': entities,
                'count': len(entities),
                'inference_time_ms': round(inference_time, 2)
            }, status=status.HTTP_200_OK)
            
        except Exception as e:
            logger.error(f"GV-Tags search error: {e}", exc_info=True)
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class SpatialSearchGVNLEView(APIView):
    """
    Spatial search using GV-NLE embeddings only.
    
    POST /api/semantic-search/gv-nle/
    {
        "reference_osm_id": 12345,
        "top_k": 10,
        "filters": {}
    }
    
    Returns entities geographically near the reference entity.
    """
    
    def post(self, request):
        reference_osm_id = request.data.get('reference_osm_id')
        top_k = request.data.get('top_k', 10)
        filters = request.data.get('filters', {})
        # Phase 6: scope to snapshot partition (falls back to latest for monolith)
        snapshot_id = request.data.get('snapshot_id') or get_latest_snapshot_id()
        
        if not reference_osm_id:
            return Response(
                {'error': 'reference_osm_id required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        start_time = time.time()
        
        try:
            # Get reference entity (using filter().first() to avoid crash on duplicates)
            ref_qs = OsmEntity.objects.using('vectors').filter(osm_id=reference_osm_id)
            if snapshot_id:
                ref_qs = ref_qs.filter(snapshot_id=snapshot_id)
            reference = ref_qs.first()
            
            if not reference:
                return Response(
                    {'error': f'Reference entity with osm_id {reference_osm_id} not found.'},
                    status=status.HTTP_404_NOT_FOUND
                )
            
            if reference.gv_nle_embedding is None:
                return Response(
                    {'error': 'Reference entity does not have GV-NLE embedding. Run train_gv_nle first.'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            query_list = reference.gv_nle_embedding
            
            # Convert to list if numpy array
            if isinstance(query_list, np.ndarray):
                query_list = query_list.tolist()
            
            # Search using pgvector cosine distance
            results = OsmEntity.objects.using('vectors').filter(
                gv_nle_embedding__isnull=False
            ).exclude(
                osm_id=reference_osm_id  # Exclude self
            )
            # Phase 6: scope to snapshot partition
            if snapshot_id:
                results = results.filter(snapshot_id=snapshot_id)
            
            # Apply filters
            if filters:
                results = results.filter(**filters)
            
            # Annotate with distance and order
            results = results.annotate(
                distance=RawSQL(
                    "gv_nle_embedding <=> %s::vector",
                    (query_list,),
                    output_field=FloatField()
                )
            ).order_by('distance')[:top_k]
            
            # Format response
            entities = []
            for entity in results:
                entities.append({
                    'osm_id': entity.osm_id,
                    'osm_type': entity.osm_type,
                    'tags': entity.tags,
                    'distance': entity.distance,
                    'similarity': 1.0 - entity.distance,
                    'location': {
                        'lat': entity.geom.y if entity.geom else None,
                        'lon': entity.geom.x if entity.geom else None,
                    }
                })
            
            inference_time = (time.time() - start_time) * 1000
            
            return Response({
                'method': 'gv-nle',
                'reference_entity': {
                    'osm_id': reference.osm_id,
                    'tags': reference.tags,
                },
                'results': entities,
                'count': len(entities),
                'inference_time_ms': round(inference_time, 2)
            }, status=status.HTTP_200_OK)
            
        except OsmEntity.DoesNotExist:
            return Response(
                {'error': f'Entity {reference_osm_id} not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        except Exception as e:
            logger.error(f"GV-NLE search error: {e}", exc_info=True)
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class HybridSearchView(APIView):
    """
    Hybrid search combining GV-Tags (semantic) and GV-NLE (spatial).
    
    POST /api/semantic-search/hybrid/
    {
        "query_tags": {"amenity": "cafe"},
        "reference_location": {"osm_id": 12345},  // or {"lat": 36.14, "lon": -5.35}
        "alpha": 0.7,  // Weight for semantic (0-1)
        "beta": 0.3,   // Weight for spatial (0-1, should sum to 1 with alpha)
        "top_k": 10,
        "filters": {}
    }
    
    Returns entities matching BOTH semantic and spatial criteria.
    Example: "Find cafes (semantic) near downtown (spatial)"
    """
    
    def post(self, request):
        query_tags = request.data.get('query_tags', {})
        wkg_class = request.data.get('wkg_class')
        reference_location = request.data.get('reference_location', {})
        alpha = request.data.get('alpha', 0.7)
        beta = request.data.get('beta', 0.3)
        top_k = request.data.get('top_k', 10)
        filters = request.data.get('filters', {})
        # Phase 6: scope to snapshot partition (falls back to latest for monolith)
        snapshot_id = request.data.get('snapshot_id') or get_latest_snapshot_id()
        
        # Fallback for semantic query
        if not query_tags and wkg_class:
            ontology = get_worldkg_ontology_service()
            key = ontology.get_canonical_osm_key(wkg_class)
            val = ontology.get_canonical_osm_value(wkg_class)
            if key and val:
                query_tags = {key: val}
            elif key:
                query_tags = {key: 'yes'}
        
        if not query_tags:
            return Response(
                {'error': 'query_tags or wkg_class required for semantic component'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        if not reference_location:
            return Response(
                {'error': 'reference_location required for spatial component'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        if abs(alpha + beta - 1.0) > 0.001:
            return Response(
                {'error': 'alpha + beta must sum to 1.0'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        start_time = time.time()
        
        try:
            # Generate semantic query embedding
            tag_counts = {f"{k}={v}": 1 for k, v in query_tags.items()}
            semantic_embedding = FastTextEmbeddingService.calculate_embedding(tag_counts)
            semantic_list = semantic_embedding.tolist()
            
            # Get spatial query embedding
            if 'osm_id' in reference_location:
                # Use filter + first() instead of get() to handle multiple osm_id matches
                ref_qs = OsmEntity.objects.using('vectors').filter(
                    osm_id=reference_location['osm_id']
                )
                if snapshot_id:
                    ref_qs = ref_qs.filter(snapshot_id=snapshot_id)
                reference = ref_qs.first()
                
                if not reference:
                    return Response(
                        {'error': f"Reference entity {reference_location['osm_id']} not found"},
                        status=status.HTTP_404_NOT_FOUND
                    )
                if reference.gv_nle_embedding is None:
                    return Response(
                        {'error': 'Reference entity missing GV-NLE embedding. Run train_gv_nle first.'},
                        status=status.HTTP_400_BAD_REQUEST
                    )
                spatial_list = reference.gv_nle_embedding
                
                # Convert to list if numpy array
                if isinstance(spatial_list, np.ndarray):
                    spatial_list = spatial_list.tolist()
                
                reference_info = {'osm_id': reference.osm_id, 'tags': reference.tags}
            else:
                return Response(
                    {'error': 'Lat/lon encoding not yet implemented. Use osm_id for now.'},
                    status=status.HTTP_501_NOT_IMPLEMENTED
                )
            
            # Hybrid query: weighted combination
            results = OsmEntity.objects.using('vectors').filter(
                gv_tags_embedding__isnull=False,
                gv_nle_embedding__isnull=False
            )
            # Phase 6: scope to snapshot partition
            if snapshot_id:
                results = results.filter(snapshot_id=snapshot_id)
            
            # Apply WorldKG class filtering if specified
            wkg_class = request.data.get('wkg_class')
            include_subclasses = request.data.get('include_subclasses', True)
            
            if wkg_class:
                ontology = get_worldkg_ontology_service()
                if include_subclasses:
                    # Get all subclasses
                    subclasses = ontology.get_subclasses(wkg_class, recursive=True)
                    class_filter = [wkg_class] + subclasses
                    results = results.filter(wkg_class__in=class_filter)
                else:
                    results = results.filter(wkg_class=wkg_class)
            
            # Apply other filters
            if filters:
                results = results.filter(**filters)
            
            # Annotate with combined score
            results = results.annotate(
                semantic_distance=RawSQL(
                    "gv_tags_embedding <=> %s::vector",
                    (semantic_list,),
                    output_field=FloatField()
                ),
                spatial_distance=RawSQL(
                    "gv_nle_embedding <=> %s::vector",
                    (spatial_list,),
                    output_field=FloatField()
                ),
                combined_score=RawSQL(
                    f"{alpha} * (gv_tags_embedding <=> %s::vector) + " +
                    f"{beta} * (gv_nle_embedding <=> %s::vector)",
                    (semantic_list, spatial_list),
                    output_field=FloatField()
                )
            ).order_by('combined_score')[:top_k]
            
            # Format response
            entities = []
            for entity in results:
                entities.append({
                    'osm_id': entity.osm_id,
                    'osm_type': entity.osm_type,
                    'tags': entity.tags,
                    'scores': {
                        'combined': entity.combined_score,
                        'semantic_distance': entity.semantic_distance,
                        'spatial_distance': entity.spatial_distance,
                        'semantic_similarity': 1.0 - entity.semantic_distance,
                        'spatial_similarity': 1.0 - entity.spatial_distance,
                    },
                    'location': {
                        'lat': entity.geom.y if entity.geom else None,
                        'lon': entity.geom.x if entity.geom else None,
                    }
                })
            
            inference_time = (time.time() - start_time) * 1000
            
            return Response({
                'method': 'hybrid',
                'query_tags': query_tags,
                'reference_location': reference_info,
                'weights': {'alpha': alpha, 'beta': beta},
                'results': entities,
                'count': len(entities),
                'inference_time_ms': round(inference_time, 2)
            }, status=status.HTTP_200_OK)
            
        except OsmEntity.DoesNotExist:
            return Response(
                {'error': 'Reference entity not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        except Exception as e:
            logger.error(f"Hybrid search error: {e}", exc_info=True)
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class TrainingStatusGVNLEView(APIView):
    """
    Get GV-NLE training status and statistics.
    
    GET /api/semantic-search/gv-nle/status/
    """
    
    def get(self, request):
        try:
            # Count entities with GV-NLE embeddings
            total_entities = OsmEntity.objects.using('vectors').count()
            trained_entities = OsmEntity.objects.using('vectors').filter(
                gv_nle_trained=True
            ).count()
            
            # Get latest version
            latest = OsmEntity.objects.using('vectors').filter(
                gv_nle_version__isnull=False
            ).first()
            
            latest_version = latest.gv_nle_version if latest else None
            
            return Response({
                'total_entities': total_entities,
                'trained_entities': trained_entities,
                'coverage': round(trained_entities / total_entities, 3) if total_entities > 0 else 0,
                'latest_version': latest_version,
                'status': 'ready' if trained_entities > 0 else 'not_trained'
            }, status=status.HTTP_200_OK)
            
        except Exception as e:
            logger.error(f"Status check error: {e}", exc_info=True)
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
