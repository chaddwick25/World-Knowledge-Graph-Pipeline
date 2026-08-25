from io import StringIO

from django.core.management import call_command
from django.db.models import Count
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, status, viewsets
from rest_framework.decorators import action
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response

from .models import (
    DataQualitySnapshot,
    DataSource,
    GeoDataset,
    GeoRecord,
)
from .serializers import (
    DataQualitySnapshotSerializer,
    DataSourceSerializer,
    GeoDatasetListSerializer,
    GeoDatasetSerializer,
    GeoRecordSerializer,
)


class StandardResultsSetPagination(PageNumberPagination):
    page_size = 100
    page_size_query_param = 'page_size'
    max_page_size = 1000


class DataSourceViewSet(viewsets.ReadOnlyModelViewSet):
    """List geodata sources with their dataset counts."""
    queryset = DataSource.objects.annotate(dataset_count=Count('datasets'))
    serializer_class = DataSourceSerializer
    pagination_class = StandardResultsSetPagination
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['adapter_type', 'country_code', 'is_active']
    search_fields = ['name', 'base_url']
    ordering_fields = ['name', 'updated_at']
    ordering = ['name']


class GeoDatasetViewSet(viewsets.ReadOnlyModelViewSet):
    """
    List geodata datasets with resources + latest quality score.

    Filters: ``?source=<id>``, ``?is_retired=true``, ``?search=ttc``.
    """
    queryset = GeoDataset.objects.annotate(record_count=Count('records'))
    pagination_class = StandardResultsSetPagination
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['source', 'is_retired', 'refresh_rate']
    search_fields = ['name', 'title', 'source_dataset_id']
    ordering_fields = ['title', 'last_synced_at', 'refresh_rate']
    ordering = ['-last_synced_at']

    def get_serializer_class(self):
        if self.action == 'list':
            return GeoDatasetListSerializer
        return GeoDatasetSerializer

    @action(detail=False, methods=['post'])
    def sync_metadata(self, request):
        """Trigger a metadata sync for a source's datasets.

        Body: ``{"source": "<name>", "datasets": ["slug", ...]}``
        (``datasets`` optional — defaults to the source's target_datasets).
        """
        source_name = request.data.get('source')
        if not source_name:
            return Response({'error': 'Missing "source" (DataSource name)'},
                            status=status.HTTP_400_BAD_REQUEST)
        source = DataSource.objects.filter(name=source_name).first()
        if source is None:
            return Response({'error': f'Source not found: {source_name}'},
                            status=status.HTTP_404_NOT_FOUND)

        dataset_ids = request.data.get('datasets') or []
        output = StringIO()
        try:
            if dataset_ids:
                call_command('sync_geodata_metadata', '--source', source_name,
                             '--datasets', *dataset_ids, stdout=output)
            else:
                call_command('sync_geodata_metadata', '--source', source_name,
                             stdout=output)
            return Response({
                'status': 'success',
                'message': 'Metadata sync completed',
                'output': output.getvalue(),
            })
        except Exception as e:
            return Response({
                'status': 'error',
                'message': str(e),
                'output': output.getvalue(),
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class DataQualitySnapshotViewSet(viewsets.ReadOnlyModelViewSet):
    """Quality snapshots; supports ``?dataset=<id>``, ``?grade=Gold``."""
    queryset = DataQualitySnapshot.objects.all()
    serializer_class = DataQualitySnapshotSerializer
    pagination_class = StandardResultsSetPagination
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['dataset', 'grade']
    ordering_fields = ['qa_recorded_at', 'ingested_at', 'quality_score_pct']
    ordering = ['-qa_recorded_at']

    @action(detail=False, methods=['get'])
    def dashboard(self, request):
        """Quality dashboard: latest grade/score per dataset."""
        datasets = GeoDataset.objects.all()
        data = []
        for dataset in datasets:
            latest = dataset.quality_snapshots.order_by('-ingested_at').first()
            data.append({
                'dataset_id': dataset.source_dataset_id,
                'name': dataset.name,
                'title': dataset.title,
                'refresh_rate': dataset.refresh_rate,
                'grade': latest.grade if latest else 'Unknown',
                'quality_score_pct': latest.quality_score_pct if latest else None,
                'ingested_at': latest.ingested_at if latest else None,
            })
        return Response({'datasets': data})


class GeoRecordViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Generic records from any dataset.

    Filters: ``?dataset=<id>``, ``?resource=<id>``, ``?source_id=...``,
    ``?ingestion_date=YYYY-MM-DD``.
    """
    queryset = GeoRecord.objects.select_related('dataset', 'resource')
    serializer_class = GeoRecordSerializer
    pagination_class = StandardResultsSetPagination
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['dataset', 'resource', 'source_id', 'ingestion_date']
    ordering_fields = ['imported_at', 'ingestion_date']
    ordering = ['-imported_at']
