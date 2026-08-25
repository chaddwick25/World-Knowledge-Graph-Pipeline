from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register(r'sources', views.DataSourceViewSet, basename='geodata-source')
router.register(r'datasets', views.GeoDatasetViewSet, basename='geodata-dataset')
router.register(r'quality', views.DataQualitySnapshotViewSet, basename='geodata-quality')
router.register(r'records', views.GeoRecordViewSet, basename='geodata-record')

urlpatterns = [
    path('', include(router.urls)),
]
