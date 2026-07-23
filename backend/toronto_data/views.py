from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.pagination import PageNumberPagination
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters
from django.core.management import call_command
from io import StringIO

from .models import (
    CkanDataset, QualitySnapshot, CkanResource,
    TrafficVolume, TtcSubwayDelay, CafetoLocation,
    # Spatial Infrastructure
    TorontoCentreline, IntersectionFile, CyclingNetwork,
    Neighbourhood, ZoningByLaw, BusinessImprovementArea,
    # Temporal Flows
    BicycleCounter, TtcRoute, RainGauge, ZoningReview,
    NeighbourhoodProfile,
    # Additional
    ForestLandCover, CommitteeAdjustmentApplication
)
from .serializers import (
    CkanDatasetSerializer, CkanDatasetListSerializer,
    QualitySnapshotSerializer, CkanResourceSerializer,
    TrafficVolumeSerializer, TtcSubwayDelaySerializer,
    CafetoLocationSerializer,
    # Spatial Infrastructure
    TorontoCentrelineSerializer, IntersectionFileSerializer, CyclingNetworkSerializer,
    NeighbourhoodSerializer, ZoningByLawSerializer, BusinessImprovementAreaSerializer,
    # Temporal Flows
    BicycleCounterSerializer, TtcRouteSerializer, RainGaugeSerializer,
    ZoningReviewSerializer, NeighbourhoodProfileSerializer,
    # Additional
    ForestLandCoverSerializer, CommitteeAdjustmentApplicationSerializer
)


class StandardResultsSetPagination(PageNumberPagination):
    page_size = 100
    page_size_query_param = 'page_size'
    max_page_size = 1000


class CkanDatasetViewSet(viewsets.ReadOnlyModelViewSet):
    """
    ViewSet for CKAN datasets
    
    list: Get all datasets with latest quality scores
    retrieve: Get detailed dataset information including resources
    """
    queryset = CkanDataset.objects.all()
    pagination_class = StandardResultsSetPagination
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['is_retired', 'refresh_rate']
    search_fields = ['ckan_id', 'title', 'name']
    ordering_fields = ['title', 'last_synced_at', 'refresh_rate']
    ordering = ['-last_synced_at']
    
    def get_serializer_class(self):
        if self.action == 'list':
            return CkanDatasetListSerializer
        return CkanDatasetSerializer
    
    @action(detail=False, methods=['post'])
    def sync_metadata(self, request):
        """Trigger metadata sync for all or specific datasets"""
        dataset_ids = request.data.get('datasets', [])
        
        output = StringIO()
        try:
            if dataset_ids:
                call_command('ingest_toronto_metadata', '--datasets', *dataset_ids, stdout=output)
            else:
                call_command('ingest_toronto_metadata', stdout=output)
            
            return Response({
                'status': 'success',
                'message': 'Metadata sync completed',
                'output': output.getvalue()
            })
        except Exception as e:
            return Response({
                'status': 'error',
                'message': str(e),
                'output': output.getvalue()
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class QualitySnapshotViewSet(viewsets.ReadOnlyModelViewSet):
    """
    ViewSet for quality snapshots
    
    list: Get all quality snapshots
    retrieve: Get specific quality snapshot
    history: Get quality history for a specific dataset
    """
    queryset = QualitySnapshot.objects.all()
    serializer_class = QualitySnapshotSerializer
    pagination_class = StandardResultsSetPagination
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['dataset', 'grade']
    ordering_fields = ['qa_recorded_at', 'ingested_at', 'quality_score_pct']
    ordering = ['-qa_recorded_at']
    
    @action(detail=False, methods=['get'])
    def dashboard(self, request):
        """Get quality dashboard data for all datasets"""
        datasets = CkanDataset.objects.all()
        data = []
        
        for dataset in datasets:
            latest = dataset.snapshots.order_by('-ingested_at').first()
            data.append({
                'ckan_id': dataset.ckan_id,
                'title': dataset.title,
                'refresh_rate': dataset.refresh_rate,
                'grade': latest.grade if latest else 'Unknown',
                'quality_score_pct': latest.quality_score_pct if latest else None,
                'ingested_at': latest.ingested_at if latest else None,
            })
        
        return Response({'datasets': data})
    
    @action(detail=False, methods=['get'], url_path='history/(?P<dataset_id>[^/.]+)')
    def history(self, request, dataset_id=None):
        """Get quality score history for a specific dataset"""
        try:
            dataset = CkanDataset.objects.get(ckan_id=dataset_id)
            snapshots = dataset.snapshots.order_by('-qa_recorded_at')
            serializer = self.get_serializer(snapshots, many=True)
            return Response({
                'dataset': dataset.title,
                'history': serializer.data
            })
        except CkanDataset.DoesNotExist:
            return Response({
                'error': f'Dataset {dataset_id} not found'
            }, status=status.HTTP_404_NOT_FOUND)


class TrafficVolumeViewSet(viewsets.ReadOnlyModelViewSet):
    """
    ViewSet for traffic volume data
    
    Supports filtering by date range, intersection, location
    """
    queryset = TrafficVolume.objects.all()
    serializer_class = TrafficVolumeSerializer
    pagination_class = StandardResultsSetPagination
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['date', 'intersection_id']
    search_fields = ['location', 'intersection_id']
    ordering_fields = ['date', 'vehicle_count']
    ordering = ['-date']


class TtcSubwayDelayViewSet(viewsets.ReadOnlyModelViewSet):
    """
    ViewSet for TTC subway delay data
    
    Supports filtering by date range, station, line, delay code
    """
    queryset = TtcSubwayDelay.objects.all()
    serializer_class = TtcSubwayDelaySerializer
    pagination_class = StandardResultsSetPagination
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['date', 'station', 'line', 'delay_code']
    search_fields = ['station', 'delay_reason']
    ordering_fields = ['date', 'delay_minutes']
    ordering = ['-date', '-time']


class CafetoLocationViewSet(viewsets.ReadOnlyModelViewSet):
    """
    ViewSet for CaféTO parklet location data
    
    Supports filtering by ward, status
    """
    queryset = CafetoLocation.objects.all()
    serializer_class = CafetoLocationSerializer
    pagination_class = StandardResultsSetPagination
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['ward', 'status']
    search_fields = ['business_name', 'address']
    ordering_fields = ['business_name', 'installation_date']
    ordering = ['business_name']


# ============================================================================
# SPATIAL INFRASTRUCTURE VIEWSETS
# ============================================================================

class TorontoCentrelineViewSet(viewsets.ReadOnlyModelViewSet):
    """ViewSet for Toronto Centreline (road network edges)"""
    queryset = TorontoCentreline.objects.all()
    serializer_class = TorontoCentrelineSerializer
    pagination_class = StandardResultsSetPagination
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['feature_code', 'centreline_id']
    search_fields = ['linear_name_full', 'address_l', 'address_r']
    ordering_fields = ['centreline_id', 'linear_name_full']
    ordering = ['centreline_id']


class IntersectionFileViewSet(viewsets.ReadOnlyModelViewSet):
    """ViewSet for Intersection File (road network nodes)"""
    queryset = IntersectionFile.objects.all()
    serializer_class = IntersectionFileSerializer
    pagination_class = StandardResultsSetPagination
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['intersection_id']
    search_fields = ['intersection_desc', 'intersection_id']
    ordering_fields = ['intersection_id', 'latitude', 'longitude']
    ordering = ['intersection_id']


class CyclingNetworkViewSet(viewsets.ReadOnlyModelViewSet):
    """ViewSet for Cycling Network infrastructure"""
    queryset = CyclingNetwork.objects.all()
    serializer_class = CyclingNetworkSerializer
    pagination_class = StandardResultsSetPagination
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['infrastructure_type', 'segment_id']
    search_fields = ['street_name', 'from_street', 'to_street']
    ordering_fields = ['street_name', 'infrastructure_type']
    ordering = ['street_name']


class NeighbourhoodViewSet(viewsets.ReadOnlyModelViewSet):
    """ViewSet for Neighbourhood boundaries"""
    queryset = Neighbourhood.objects.all()
    serializer_class = NeighbourhoodSerializer
    pagination_class = StandardResultsSetPagination
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['neighbourhood_id']
    search_fields = ['neighbourhood_name', 'neighbourhood_id']
    ordering_fields = ['neighbourhood_name', 'area_sqkm']
    ordering = ['neighbourhood_name']


class ZoningByLawViewSet(viewsets.ReadOnlyModelViewSet):
    """ViewSet for Zoning By-Law classifications"""
    queryset = ZoningByLaw.objects.all()
    serializer_class = ZoningByLawSerializer
    pagination_class = StandardResultsSetPagination
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['zone_category', 'zone_id']
    search_fields = ['zone_label', 'description']
    ordering_fields = ['zone_category', 'zone_label']
    ordering = ['zone_category', 'zone_label']


class BusinessImprovementAreaViewSet(viewsets.ReadOnlyModelViewSet):
    """ViewSet for Business Improvement Areas"""
    queryset = BusinessImprovementArea.objects.all()
    serializer_class = BusinessImprovementAreaSerializer
    pagination_class = StandardResultsSetPagination
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['bia_id']
    search_fields = ['bia_name']
    ordering_fields = ['bia_name', 'area_sqkm']
    ordering = ['bia_name']


# ============================================================================
# TEMPORAL FLOW VIEWSETS
# ============================================================================

class BicycleCounterViewSet(viewsets.ReadOnlyModelViewSet):
    """ViewSet for Bicycle Counter readings"""
    queryset = BicycleCounter.objects.all()
    serializer_class = BicycleCounterSerializer
    pagination_class = StandardResultsSetPagination
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['count_date', 'location_id']
    search_fields = ['location_name']
    ordering_fields = ['count_date', 'count_value']
    ordering = ['-count_date', '-count_time']


class TtcRouteViewSet(viewsets.ReadOnlyModelViewSet):
    """ViewSet for TTC Routes and Schedules"""
    queryset = TtcRoute.objects.all()
    serializer_class = TtcRouteSerializer
    pagination_class = StandardResultsSetPagination
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['route_type', 'route_id']
    search_fields = ['route_name']
    ordering_fields = ['route_name', 'route_type']
    ordering = ['route_name']


class RainGaugeViewSet(viewsets.ReadOnlyModelViewSet):
    """ViewSet for Rain Gauge precipitation readings"""
    queryset = RainGauge.objects.all()
    serializer_class = RainGaugeSerializer
    pagination_class = StandardResultsSetPagination
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['measurement_date', 'station_id']
    search_fields = ['station_name']
    ordering_fields = ['measurement_date', 'precipitation_mm']
    ordering = ['-measurement_date', '-measurement_time']


class ZoningReviewViewSet(viewsets.ReadOnlyModelViewSet):
    """ViewSet for Zoning Review applications"""
    queryset = ZoningReview.objects.all()
    serializer_class = ZoningReviewSerializer
    pagination_class = StandardResultsSetPagination
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['application_date', 'status', 'application_id']
    search_fields = ['address', 'proposal_description']
    ordering_fields = ['application_date', 'status']
    ordering = ['-application_date']


class NeighbourhoodProfileViewSet(viewsets.ReadOnlyModelViewSet):
    """ViewSet for Neighbourhood Census Profiles"""
    queryset = NeighbourhoodProfile.objects.all()
    serializer_class = NeighbourhoodProfileSerializer
    pagination_class = StandardResultsSetPagination
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['neighbourhood_id', 'census_year']
    search_fields = ['neighbourhood_id']
    ordering_fields = ['neighbourhood_id', 'census_year', 'population']
    ordering = ['neighbourhood_id', '-census_year']


# ============================================================================
# ADDITIONAL VIEWSETS
# ============================================================================

class ForestLandCoverViewSet(viewsets.ReadOnlyModelViewSet):
    """ViewSet for Forest and Land Cover data"""
    queryset = ForestLandCover.objects.all()
    serializer_class = ForestLandCoverSerializer
    pagination_class = StandardResultsSetPagination
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['land_cover_type', 'feature_id']
    search_fields = ['land_cover_type']
    ordering_fields = ['land_cover_type', 'area_sqm']
    ordering = ['land_cover_type']


class CommitteeAdjustmentApplicationViewSet(viewsets.ReadOnlyModelViewSet):
    """ViewSet for Committee of Adjustment Applications"""
    queryset = CommitteeAdjustmentApplication.objects.all()
    serializer_class = CommitteeAdjustmentApplicationSerializer
    pagination_class = StandardResultsSetPagination
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['application_date', 'status', 'application_type']
    search_fields = ['address', 'application_number']
    ordering_fields = ['application_date', 'status']
    ordering = ['-application_date']
