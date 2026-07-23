from django.urls import path, include
from rest_framework.routers import DefaultRouter
from . import views

router = DefaultRouter()
***REMOVED*** Metadata
router.register(r'datasets', views.CkanDatasetViewSet, basename='ckan-dataset')
router.register(r'quality', views.QualitySnapshotViewSet, basename='quality-snapshot')

***REMOVED*** Existing datasets
router.register(r'traffic', views.TrafficVolumeViewSet, basename='traffic-volume')
router.register(r'ttc-delays', views.TtcSubwayDelayViewSet, basename='ttc-delay')
router.register(r'cafeto', views.CafetoLocationViewSet, basename='cafeto-location')

***REMOVED*** Spatial Infrastructure
router.register(r'centreline', views.TorontoCentrelineViewSet, basename='centreline')
router.register(r'intersections', views.IntersectionFileViewSet, basename='intersection')
router.register(r'cycling-network', views.CyclingNetworkViewSet, basename='cycling')
router.register(r'neighbourhoods', views.NeighbourhoodViewSet, basename='neighbourhood')
router.register(r'zoning', views.ZoningByLawViewSet, basename='zoning')
router.register(r'bia', views.BusinessImprovementAreaViewSet, basename='bia')

***REMOVED*** Temporal Flows
router.register(r'bicycle-counters', views.BicycleCounterViewSet, basename='bicycle-counter')
router.register(r'ttc-routes', views.TtcRouteViewSet, basename='ttc-route')
router.register(r'rain-gauges', views.RainGaugeViewSet, basename='rain-gauge')
router.register(r'zoning-reviews', views.ZoningReviewViewSet, basename='zoning-review')
router.register(r'neighbourhood-profiles', views.NeighbourhoodProfileViewSet, basename='neighbourhood-profile')

***REMOVED*** Additional
router.register(r'forest-land-cover', views.ForestLandCoverViewSet, basename='forest-land-cover')
router.register(r'committee-adjustment', views.CommitteeAdjustmentApplicationViewSet, basename='committee-adjustment')

urlpatterns = [
    path('', include(router.urls)),
]
