"""
PBF and Snapshot-Scoped Search API Views

Provides UUID-based search endpoints:
1. PBF-scoped search: Search entities from a specific PBF file
2. Snapshot-scoped search: Search entities from a temporal snapshot
3. Discovery: List available PBFs/snapshots with embeddings
"""

import json
import logging
from pathlib import Path

from django.db import models
from django.db.models import FloatField, Count, Max
from django.db.models.expressions import RawSQL
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from extraction.models import PbfFile
from api.models import TemporalSnapshot
from worldkg_nca.models import OsmEntity
from .services.fasttext_service import FastTextEmbeddingService

logger = logging.getLogger(__name__)


class SnapshotHybridSearchView(APIView):
    """
    Hybrid search for entities from a specific temporal snapshot.
    
    GET /api/search/snapshot/<uuid:snapshot_id>/hybrid/
    
    Query params:
        - query_tags: {"amenity": "cafe"}
        - alpha: 0.7 (semantic weight)
        - top_k: 10
    """
    
    def get(self, request, snapshot_id):
        try:
            snapshot = TemporalSnapshot.objects.get(id=snapshot_id)
        except TemporalSnapshot.DoesNotExist:
            return Response({
                'error': 'Snapshot not found',
                'snapshot_id': str(snapshot_id)
            }, status=status.HTTP_404_NOT_FOUND)
        
        entity_count = OsmEntity.objects.using('vectors').filter(
            source_snapshot_id=snapshot_id,
            gv_nle_trained=True
        ).count()
        
        if entity_count == 0:
            return Response({
                'error': 'No trained embeddings for this snapshot',
                'snapshot': {
                    'id': str(snapshot.id),
                    'region': snapshot.region,
                    'timestamp': snapshot.timestamp.isoformat()
                }
            }, status=status.HTTP_404_NOT_FOUND)
        
        query_tags = json.loads(request.GET.get('query_tags', '{}'))
        alpha = float(request.GET.get('alpha', 0.7))
        top_k = int(request.GET.get('top_k', 10))
        
        if not query_tags:
            return Response({
                'error': 'query_tags parameter required'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        tag_counts = {f"{k}={v}": 1 for k, v in query_tags.items()}
        query_embedding = FastTextEmbeddingService.calculate_embedding(tag_counts)
        
        entities = OsmEntity.objects.using('vectors').filter(
            source_snapshot_id=snapshot_id,
            gv_nle_trained=True,
            gv_tags_embedding__isnull=False,
            gv_nle_embedding__isnull=False
        ).annotate(
            semantic_dist=RawSQL(
                "gv_tags_embedding <=> %s::vector",
                (query_embedding.tolist(),),
                output_field=FloatField()
            ),
            spatial_dist=RawSQL(
                "gv_nle_embedding <=> %s::vector",
                (query_embedding.tolist(),),
                output_field=FloatField()
            ),
            hybrid_score=RawSQL(
                "%s * (gv_tags_embedding <=> %s::vector) + %s * (gv_nle_embedding <=> %s::vector)",
                (alpha, query_embedding.tolist(), 1-alpha, query_embedding.tolist()),
                output_field=FloatField()
            )
        ).order_by('hybrid_score')[:top_k]
        
        return Response({
            'snapshot': {
                'id': str(snapshot.id),
                'region': snapshot.region,
                'timestamp': snapshot.timestamp.isoformat(),
                'entity_count': entity_count
            },
            'query': {
                'tags': query_tags,
                'alpha': alpha,
                'top_k': top_k
            },
            'results': [self._serialize_entity(e) for e in entities]
        })
    
    def _serialize_entity(self, entity):
        return {
            'osm_id': entity.osm_id,
            'osm_type': entity.osm_type,
            'tags': entity.tags,
            'geometry': {
                'lat': entity.geom.y if entity.geom else None,
                'lon': entity.geom.x if entity.geom else None
            },
            'semantic_distance': float(entity.semantic_dist) if hasattr(entity, 'semantic_dist') else None,
            'spatial_distance': float(entity.spatial_dist) if hasattr(entity, 'spatial_dist') else None,
            'hybrid_score': float(entity.hybrid_score) if hasattr(entity, 'hybrid_score') else None,
            'osm_url': entity.osm_url
        }


class SnapshotGVTagsSearchView(APIView):
    """
    Semantic-only search for entities from a specific temporal snapshot.
    
    GET /api/search/snapshot/<uuid:snapshot_id>/gv-tags/
    """
    
    def get(self, request, snapshot_id):
        try:
            snapshot = TemporalSnapshot.objects.get(id=snapshot_id)
        except TemporalSnapshot.DoesNotExist:
            return Response({
                'error': 'Snapshot not found'
            }, status=status.HTTP_404_NOT_FOUND)
        
        query_tags = json.loads(request.GET.get('query_tags', '{}'))
        top_k = int(request.GET.get('top_k', 10))
        
        if not query_tags:
            return Response({
                'error': 'query_tags parameter required'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        tag_counts = {f"{k}={v}": 1 for k, v in query_tags.items()}
        query_embedding = FastTextEmbeddingService.calculate_embedding(tag_counts)
        
        entities = OsmEntity.objects.using('vectors').filter(
            source_snapshot_id=snapshot_id,
            gv_tags_embedding__isnull=False
        ).annotate(
            distance=RawSQL(
                "gv_tags_embedding <=> %s::vector",
                (query_embedding.tolist(),),
                output_field=FloatField()
            )
        ).order_by('distance')[:top_k]
        
        return Response({
            'snapshot': {
                'id': str(snapshot.id),
                'region': snapshot.region,
                'timestamp': snapshot.timestamp.isoformat()
            },
            'results': [{
                'osm_id': e.osm_id,
                'osm_type': e.osm_type,
                'tags': e.tags,
                'distance': float(e.distance),
                'geometry': {
                    'lat': e.geom.y if e.geom else None,
                    'lon': e.geom.x if e.geom else None
                }
            } for e in entities]
        })


class SnapshotGVNLESearchView(APIView):
    """
    Spatial-only search for entities from a specific temporal snapshot.
    
    GET /api/search/snapshot/<uuid:snapshot_id>/gv-nle/
    """
    
    def get(self, request, snapshot_id):
        try:
            snapshot = TemporalSnapshot.objects.get(id=snapshot_id)
        except TemporalSnapshot.DoesNotExist:
            return Response({
                'error': 'Snapshot not found'
            }, status=status.HTTP_404_NOT_FOUND)
        
        reference_osm_id = request.GET.get('reference_osm_id')
        top_k = int(request.GET.get('top_k', 10))
        
        if not reference_osm_id:
            return Response({
                'error': 'reference_osm_id parameter required'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            reference_entity = OsmEntity.objects.using('vectors').get(
                osm_id=reference_osm_id,
                gv_nle_embedding__isnull=False
            )
        except OsmEntity.DoesNotExist:
            return Response({
                'error': 'Reference entity not found or has no GV-NLE embedding'
            }, status=status.HTTP_404_NOT_FOUND)
        
        entities = OsmEntity.objects.using('vectors').filter(
            source_snapshot_id=snapshot_id,
            gv_nle_trained=True,
            gv_nle_embedding__isnull=False
        ).exclude(
            osm_id=reference_osm_id
        ).annotate(
            distance=RawSQL(
                "gv_nle_embedding <=> %s::vector",
                (reference_entity.gv_nle_embedding,),
                output_field=FloatField()
            )
        ).order_by('distance')[:top_k]
        
        return Response({
            'snapshot': {
                'id': str(snapshot.id),
                'region': snapshot.region,
                'timestamp': snapshot.timestamp.isoformat()
            },
            'reference': {
                'osm_id': reference_entity.osm_id,
                'osm_type': reference_entity.osm_type,
                'tags': reference_entity.tags
            },
            'results': [{
                'osm_id': e.osm_id,
                'osm_type': e.osm_type,
                'tags': e.tags,
                'distance': float(e.distance),
                'geometry': {
                    'lat': e.geom.y if e.geom else None,
                    'lon': e.geom.x if e.geom else None
                }
            } for e in entities]
        })


class SnapshotListView(APIView):
    """
    List all temporal snapshots that have trained embeddings.
    
    GET /api/search/snapshots/available/
    """
    
    def get(self, request):
        snapshot_stats = (
            OsmEntity.objects.using('vectors')
            .filter(gv_nle_trained=True, source_snapshot_id__isnull=False)
            .values('source_snapshot_id')
            .annotate(
                entity_count=Count('id'),
                latest_version=Max('gv_nle_version'),
                last_trained=Max('updated_at')
            )
        )
        
        results = []
        for stat in snapshot_stats:
            try:
                snapshot = TemporalSnapshot.objects.get(id=stat['source_snapshot_id'])
                results.append({
                    'id': str(snapshot.id),
                    'region': snapshot.region,
                    'timestamp': snapshot.timestamp.isoformat(),
                    'snapshot_interval': snapshot.snapshot_interval,
                    'pbf_file': {
                        'id': str(snapshot.pbf_file.id),
                        'path': snapshot.pbf_file.path
                    } if snapshot.pbf_file else None,
                    'embeddings': {
                        'entity_count': stat['entity_count'],
                        'latest_version': stat['latest_version'],
                        'last_trained': stat['last_trained'].isoformat()
                    }
                })
            except TemporalSnapshot.DoesNotExist:
                continue
        
        return Response({
            'count': len(results),
            'results': sorted(results, key=lambda x: x['region'])
        })


class PbfHybridSearchView(APIView):
    """
    Hybrid search for entities from a specific PBF file.
    
    GET /api/search/pbf/<uuid:pbf_id>/hybrid/
    
    Searches all entities linked to snapshots from this PBF.
    """
    
    def get(self, request, pbf_id):
        try:
            pbf = PbfFile.objects.get(id=pbf_id)
        except PbfFile.DoesNotExist:
            return Response({
                'error': 'PBF file not found'
            }, status=status.HTTP_404_NOT_FOUND)
        
        entity_count = OsmEntity.objects.using('vectors').filter(
            source_snapshot__pbf_file_id=pbf_id,
            gv_nle_trained=True
        ).count()
        
        if entity_count == 0:
            return Response({
                'error': 'No trained embeddings for this PBF file',
                'pbf': {
                    'id': str(pbf.id),
                    'path': pbf.path
                }
            }, status=status.HTTP_404_NOT_FOUND)
        
        query_tags = json.loads(request.GET.get('query_tags', '{}'))
        alpha = float(request.GET.get('alpha', 0.7))
        top_k = int(request.GET.get('top_k', 10))
        
        if not query_tags:
            return Response({
                'error': 'query_tags parameter required'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        tag_counts = {f"{k}={v}": 1 for k, v in query_tags.items()}
        query_embedding = FastTextEmbeddingService.calculate_embedding(tag_counts)
        
        entities = OsmEntity.objects.using('vectors').filter(
            source_snapshot__pbf_file_id=pbf_id,
            gv_nle_trained=True,
            gv_tags_embedding__isnull=False,
            gv_nle_embedding__isnull=False
        ).annotate(
            semantic_dist=RawSQL(
                "gv_tags_embedding <=> %s::vector",
                (query_embedding.tolist(),),
                output_field=FloatField()
            ),
            spatial_dist=RawSQL(
                "gv_nle_embedding <=> %s::vector",
                (query_embedding.tolist(),),
                output_field=FloatField()
            ),
            hybrid_score=RawSQL(
                "%s * (gv_tags_embedding <=> %s::vector) + %s * (gv_nle_embedding <=> %s::vector)",
                (alpha, query_embedding.tolist(), 1-alpha, query_embedding.tolist()),
                output_field=FloatField()
            )
        ).order_by('hybrid_score')[:top_k]
        
        region_name = self._extract_region_name(pbf.path) if pbf.path else 'unknown'
        
        return Response({
            'pbf': {
                'id': str(pbf.id),
                'path': pbf.path,
                'region': region_name,
                'extraction_level': pbf.extraction_level,
                'entity_count': entity_count
            },
            'query': {
                'tags': query_tags,
                'alpha': alpha,
                'top_k': top_k
            },
            'results': [self._serialize_entity(e) for e in entities]
        })
    
    def _extract_region_name(self, path):
        """Extract region name from PBF path."""
        return Path(path).stem.split('_')[0] if path else 'unknown'
    
    def _serialize_entity(self, entity):
        return {
            'osm_id': entity.osm_id,
            'osm_type': entity.osm_type,
            'tags': entity.tags,
            'geometry': {
                'lat': entity.geom.y if entity.geom else None,
                'lon': entity.geom.x if entity.geom else None
            },
            'semantic_distance': float(entity.semantic_dist) if hasattr(entity, 'semantic_dist') else None,
            'spatial_distance': float(entity.spatial_dist) if hasattr(entity, 'spatial_dist') else None,
            'hybrid_score': float(entity.hybrid_score) if hasattr(entity, 'hybrid_score') else None,
            'osm_url': entity.osm_url
        }


class PbfListView(APIView):
    """
    List all PBF files that have trained embeddings.
    
    GET /api/search/pbf/available/
    """
    
    def get(self, request):
        # Get unique snapshot IDs, then group by PBF
        snapshot_ids = (
            OsmEntity.objects.using('vectors')
            .filter(gv_nle_trained=True, source_snapshot_id__isnull=False)
            .values_list('source_snapshot_id', flat=True)
            .distinct()
        )
        
        # Group by PBF file
        pbf_map = {}
        for snapshot_id in snapshot_ids:
            try:
                snapshot = TemporalSnapshot.objects.get(id=snapshot_id)
                if snapshot.pbf_file:
                    pbf_id = str(snapshot.pbf_file.id)
                    if pbf_id not in pbf_map:
                        pbf_map[pbf_id] = {
                            'pbf': snapshot.pbf_file,
                            'snapshot_ids': []
                        }
                    pbf_map[pbf_id]['snapshot_ids'].append(snapshot_id)
            except TemporalSnapshot.DoesNotExist:
                continue
        
        # Get entity counts per PBF
        results = []
        for pbf_id, data in pbf_map.items():
            try:
                pbf = data['pbf']
                
                # Count entities across all snapshots for this PBF
                stats = OsmEntity.objects.using('vectors').filter(
                    source_snapshot_id__in=data['snapshot_ids'],
                    gv_nle_trained=True
                ).aggregate(
                    entity_count=Count('id'),
                    latest_version=Max('gv_nle_version'),
                    last_trained=Max('updated_at')
                )
                region_name = self._extract_region_name(pbf.path) if pbf.path else 'unknown'
                
                results.append({
                    'id': str(pbf.id),
                    'path': pbf.path,
                    'region': region_name,
                    'extraction_level': pbf.extraction_level,
                    'temporal_range': {
                        'start': pbf.min_timestamp.isoformat() if pbf.min_timestamp else None,
                        'end': pbf.max_timestamp.isoformat() if pbf.max_timestamp else None
                    },
                    'embeddings': {
                        'entity_count': stats['entity_count'],
                        'latest_version': stats['latest_version'],
                        'last_trained': stats['last_trained'].isoformat() if stats['last_trained'] else None
                    }
                })
            except PbfFile.DoesNotExist:
                continue
        
        return Response({
            'count': len(results),
            'results': sorted(results, key=lambda x: x['region'])
        })
    
    def _extract_region_name(self, path):
        return Path(path).stem.split('_')[0] if path else 'unknown'
