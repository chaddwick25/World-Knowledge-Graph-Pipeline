from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.db.models import Q
from .models import CensusTract
from .services.fasttext_service import FastTextEmbeddingService
from .services.sbert_service import SBERTEmbeddingService
from .serializers import (
    SemanticSearchRequestSerializer,
    SemanticSearchResponseSerializer,
    ComparisonRequestSerializer,
    EmbeddingComparisonSerializer,
    ProjectionHeadCheckpointSerializer
)
from .models import EmbeddingComparison, ProjectionHeadCheckpoint
import time
import logging
import numpy as np

logger = logging.getLogger(__name__)


class SemanticSearchView(APIView):
    """
    Semantic search endpoint with dual method support.
    
    POST /api/semantic-search/search/
    {
        "method": "fasttext" | "hidden_state",
        "tag_counts": {"cafe": 40, "residential": 30},
        "filters": {"country": "canada"},
        "top_k": 10
    }
    """
    
    def post(self, request):
        serializer = SemanticSearchRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        
        data = serializer.validated_data
        method = data['method']
        tag_counts = data['tag_counts']
        filters = data.get('filters', {})
        top_k = data['top_k']
        
        start_time = time.time()
        
        try:
            if method == 'fasttext':
                query_vector = FastTextEmbeddingService.calculate_embedding(tag_counts)
                query_text = None
            elif method == 'sbert':
                query_vector = SBERTEmbeddingService.encode_tags(tag_counts)
                query_text = None
            elif method == 'hidden_state':
                return Response(
                    {'error': 'Hidden state method not yet implemented. Install PyTorch and transformers first.'},
                    status=status.HTTP_501_NOT_IMPLEMENTED
                )
            else:
                return Response(
                    {'error': f'Unknown method: {method}'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            embedding_time_ms = (time.time() - start_time) * 1000
            search_start_time = time.time()
            
            # Search using pgvector
            results = self._search_similar_tracts(query_vector, filters, top_k)
            
            inference_time_ms = (time.time() - start_time) * 1000
            
            response_data = {
                'method': method,
                'query_tags': tag_counts,
                'query_text': query_text,
                'embedding_time_ms': round(embedding_time_ms, 2),
                'search_time_ms': round((time.time() - search_start_time) * 1000, 2),
                'inference_time_ms': round(inference_time_ms, 2),
                'results': results
            }
            
            # Log performance metrics for comparison
            logger.info(f"Encoder performance: method={method}, embedding_ms={embedding_time_ms:.2f}, search_ms={(time.time() - search_start_time) * 1000:.2f}, total_ms={inference_time_ms:.2f}")
            
            return Response(response_data, status=status.HTTP_200_OK)
            
        except Exception as e:
            logger.error(f"Semantic search error: {e}", exc_info=True)
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    def _search_similar_tracts(self, query_vector, filters, top_k):
        """
        Search for similar census tracts using pgvector cosine similarity.
        
        Args:
            query_vector: numpy array of shape (300,)
            filters: dict of filters (country, region, etc.)
            top_k: number of results to return
        
        Returns:
            List of dicts with census tract info and similarity scores
        """
        # Build query
        queryset = CensusTract.objects.using('vectors').all()
        
        # Apply filters
        if 'country' in filters:
            queryset = queryset.filter(country=filters['country'])
        if 'region' in filters:
            queryset = queryset.filter(region=filters['region'])
        
        # Convert numpy to list for pgvector
        query_vector_list = query_vector.tolist()
        
        # Perform vector similarity search using pgvector
        # Note: pgvector's <=> operator computes cosine distance
        results = queryset.order_by(
            CensusTract.semantic_embedding.cosine_distance(query_vector_list)
        )[:top_k]
        
        # Format results
        formatted_results = []
        for idx, tract in enumerate(results):
            # Calculate cosine similarity (1 - cosine_distance)
            # Note: We need to compute it manually since pgvector returns distance
            tract_vector = np.array(tract.semantic_embedding)
            similarity = float(np.dot(query_vector, tract_vector))
            
            formatted_results.append({
                'census_tract': {
                    'geouid': tract.geouid,
                    'name': tract.name,
                    'country': tract.country,
                    'region': tract.region,
                },
                'similarity': round(similarity, 4),
                'rank': idx + 1
            })
        
        return formatted_results


class ComparisonView(APIView):
    """
    Compare multiple embedding methods side-by-side.
    
    POST /api/semantic-search/compare/
    {
        "tag_counts": {"cafe": 40, "residential": 30},
        "methods": ["fasttext", "hidden_state"],
        "top_k": 10
    }
    """
    
    def post(self, request):
        serializer = ComparisonRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        
        data = serializer.validated_data
        tag_counts = data['tag_counts']
        methods = data['methods']
        filters = data.get('filters', {})
        top_k = data['top_k']
        
        results = {}
        
        # Run each method
        for method in methods:
            search_request = {
                'method': method,
                'tag_counts': tag_counts,
                'filters': filters,
                'top_k': top_k
            }
            
            # Call search endpoint internally
            search_view = SemanticSearchView()
            response = search_view.post(type('Request', (), {'data': search_request})())
            
            if response.status_code == 200:
                results[method] = response.data
            else:
                results[method] = {'error': response.data}
        
        # Calculate agreement metrics if both methods succeeded
        if len(results) == 2 and all('error' not in r for r in results.values()):
            agreement = self._calculate_agreement(results)
            results['agreement'] = agreement
        
        return Response(results, status=status.HTTP_200_OK)
    
    def _calculate_agreement(self, results):
        """Calculate agreement metrics between two methods."""
        methods = list(results.keys())
        if len(methods) != 2:
            return {}
        
        results_1 = results[methods[0]]['results']
        results_2 = results[methods[1]]['results']
        
        # Extract geouid sets
        top_5_1 = {r['census_tract']['geouid'] for r in results_1[:5]}
        top_5_2 = {r['census_tract']['geouid'] for r in results_2[:5]}
        
        top_10_1 = {r['census_tract']['geouid'] for r in results_1[:10]}
        top_10_2 = {r['census_tract']['geouid'] for r in results_2[:10]}
        
        # Jaccard overlap
        overlap_at_5 = len(top_5_1 & top_5_2) / len(top_5_1 | top_5_2) if top_5_1 or top_5_2 else 0
        overlap_at_10 = len(top_10_1 & top_10_2) / len(top_10_1 | top_10_2) if top_10_1 or top_10_2 else 0
        
        # Rank correlation would require scipy, skip for now
        
        return {
            'overlap_at_5': round(overlap_at_5, 3),
            'overlap_at_10': round(overlap_at_10, 3),
        }


class TrainingStatusView(APIView):
    """
    Get training status for projection head.
    
    GET /api/semantic-search/training/status/
    """
    
    def get(self, request):
        try:
            latest_checkpoint = ProjectionHeadCheckpoint.objects.first()
            
            if not latest_checkpoint:
                return Response({
                    'message': 'No training checkpoints found',
                    'latest_checkpoint': None
                }, status=status.HTTP_200_OK)
            
            serializer = ProjectionHeadCheckpointSerializer(latest_checkpoint)
            
            # Calculate metrics summary
            all_checkpoints = ProjectionHeadCheckpoint.objects.filter(
                version=latest_checkpoint.version
            )
            
            metrics_summary = {
                'total_epochs': max(cp.epoch for cp in all_checkpoints),
                'best_val_loss': min(
                    (cp.val_loss for cp in all_checkpoints if cp.val_loss is not None),
                    default=None
                ),
                'convergence_status': 'training' if latest_checkpoint.epoch < 20 else 'completed'
            }
            
            return Response({
                'latest_checkpoint': serializer.data,
                'metrics_summary': metrics_summary
            }, status=status.HTTP_200_OK)
            
        except Exception as e:
            logger.error(f"Training status error: {e}", exc_info=True)
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class BenchmarkResultsView(APIView):
    """
    Get benchmark results.
    
    GET /api/semantic-search/benchmarks/?test_name=&method=&start_date=&end_date=
    """
    
    def get(self, request):
        queryset = EmbeddingComparison.objects.all()
        
        # Apply filters
        test_name = request.query_params.get('test_name')
        if test_name:
            queryset = queryset.filter(test_name=test_name)
        
        method = request.query_params.get('method')
        if method:
            queryset = queryset.filter(method=method)
        
        start_date = request.query_params.get('start_date')
        if start_date:
            queryset = queryset.filter(timestamp__gte=start_date)
        
        end_date = request.query_params.get('end_date')
        if end_date:
            queryset = queryset.filter(timestamp__lte=end_date)
        
        serializer = EmbeddingComparisonSerializer(queryset[:100], many=True)
        
        # Calculate aggregates
        if queryset.exists():
            aggregates = self._calculate_aggregates(queryset)
        else:
            aggregates = {}
        
        return Response({
            'results': serializer.data,
            'aggregates': aggregates,
            'count': queryset.count()
        }, status=status.HTTP_200_OK)
    
    def _calculate_aggregates(self, queryset):
        """Calculate aggregate statistics."""
        from django.db.models import Avg, Count
        
        by_method = queryset.values('method').annotate(
            avg_recall_at_5=Avg('recall_at_5'),
            avg_mrr=Avg('mrr'),
            avg_inference_ms=Avg('inference_time_ms'),
            count=Count('id')
        )
        
        return {
            'by_method': list(by_method)
        }
