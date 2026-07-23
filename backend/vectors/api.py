from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from .models import OsmEmbedding
from pgvector.django import L2Distance

class VectorSearchAPIView(APIView):
    """
    API view for performing vector similarity search on OSM embeddings.
    """
    def post(self, request, *args, **kwargs):
        query_vector = request.data.get('vector')
        top_k = request.data.get('top_k', 10)

        if not query_vector or not isinstance(query_vector, list):
            return Response(
                {'error': 'A valid vector must be provided.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if not isinstance(top_k, int) or top_k <= 0:
            return Response(
                {'error': 'top_k must be a positive integer.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            # Query using pgvector's L2 distance operator
            results = OsmEmbedding.objects.annotate(
                similarity=L2Distance('embedding', query_vector)
            ).order_by('similarity')[:top_k]

            response_data = [
                {
                    'tag': obj.tag,
                    'similarity': obj.similarity
                }
                for obj in results
            ]

            return Response({'results': response_data})

        except Exception as e:
            return Response(
                {'error': f'An error occurred during the search: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
