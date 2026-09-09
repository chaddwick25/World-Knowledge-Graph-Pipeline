from django.urls import path
from .views_hybrid import (
    SemanticSearchGVTagsView,
    SpatialSearchGVNLEView,
    HybridSearchView,
    TrainingStatusGVNLEView
)
from .views_pbf import (
    SnapshotHybridSearchView,
    SnapshotGVTagsSearchView,
    SnapshotGVNLESearchView,
    SnapshotListView,
    PbfHybridSearchView,
    PbfListView
)
***REMOVED*** WorldKG views moved to worldkg_nca app
***REMOVED*** from .views_worldkg import (...)

app_name = 'semantic_search'

urlpatterns = [
    ***REMOVED*** Dual embedding endpoints (OSM entity search)
    path('gv-tags/', SemanticSearchGVTagsView.as_view(), name='search_gv_tags'),
    path('gv-nle/', SpatialSearchGVNLEView.as_view(), name='search_gv_nle'),
    path('hybrid/', HybridSearchView.as_view(), name='search_hybrid'),
    path('gv-nle/status/', TrainingStatusGVNLEView.as_view(), name='gv_nle_status'),

    ***REMOVED*** Snapshot-scoped search (UUID-based)
    path('snapshots/available/', SnapshotListView.as_view(), name='snapshots_available'),
    path('snapshot/<uuid:snapshot_id>/hybrid/', SnapshotHybridSearchView.as_view(), name='snapshot_hybrid_search'),
    path('snapshot/<uuid:snapshot_id>/gv-tags/', SnapshotGVTagsSearchView.as_view(), name='snapshot_gv_tags_search'),
    path('snapshot/<uuid:snapshot_id>/gv-nle/', SnapshotGVNLESearchView.as_view(), name='snapshot_gv_nle_search'),

    ***REMOVED*** PBF-scoped search (UUID-based)
    path('pbf/available/', PbfListView.as_view(), name='pbf_available'),
    path('pbf/<uuid:pbf_id>/hybrid/', PbfHybridSearchView.as_view(), name='pbf_hybrid_search'),

    ***REMOVED*** WorldKG semantic enrichment endpoints moved to /api/nca/ (worldkg_nca app)
    ***REMOVED*** See worldkg_nca/urls.py for new endpoints
]
