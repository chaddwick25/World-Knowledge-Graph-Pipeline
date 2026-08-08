from django.urls import path, include
from . import views
from . import tag_discovery_views
from . import filtered_snapshot_views
from . import asset_views
from . import preprocessing_views
from . import search_update_views
from . import country_search_views
from . import views_worldkg_pipeline as worldkg_pipeline_views
from . import projection_weight_views
from . import views_augmented_data
from . import views_system_summary
from .services import views_app_state
from .services import views_artifact_registry
from .views import (
    InitialStatusView,
    RegisterPlanetPbfView,
    SystemInitializeView,
    RegionMapDataView,
    ExtractionChainView,
    CreatePbfExtractTaskView,
    TaskStatusView,
    TemporalExtractView,
    PbfFileListView,
    PbfTemporalRangesView,
    PolygonFileListView,
)

# Core API Endpoints - E2E Tested
urlpatterns = [
    # Initial Status
    path('status/initial/', InitialStatusView.as_view(), name='initial_status'),
    
    # # App State (frontend bootstrap — replaces multiple /status/initial/ + /state/ calls)
    path('app-state/<str:country_name>/', views_app_state.AppStateView.as_view(), name='app_state'),
    
    # System Initialization
    path('system/initialize/', SystemInitializeView.as_view(), name='system_initialize'),
    
    # Recipe Builder - Region Map Data
    path('recipes/regions-map-data/', RegionMapDataView.as_view(), name='region_map_data'),
    path('recipes/extraction-chain/', ExtractionChainView.as_view(), name='extraction_chain'),
    
    # Extraction Service - Planet & Regional Extraction
    path('pbf/register-planet/', RegisterPlanetPbfView.as_view(), name='register_planet_pbf'),
    path('tasks/create-pbf-extract/', CreatePbfExtractTaskView.as_view(), name='create_pbf_extract_task'),
    path('tasks/<uuid:id>/', TaskStatusView.as_view(), name='task_status'),
    
    # Temporal Extraction - Yearly & Monthly
    path('temporal-extracts/generate/', TemporalExtractView.as_view(), name='generate-temporal-extracts'),
    
    # PBF File Management
    path('pbf/', PbfFileListView.as_view(), name='pbf_list'),
    path('pbf/temporal-ranges/', PbfTemporalRangesView.as_view(), name='pbf_temporal_ranges'),
    path('polygons/list/', PolygonFileListView.as_view(), name='polygon_list'),
    
    # Analysis Service - Tag Discovery
    path('tag-discovery/analyze/', tag_discovery_views.TagDiscoveryView.as_view(), name='tag-discovery-analyze'),
    
    # Asset Bundles (Optional - for frontend)
    path('assets/bundles/', asset_views.AssetBundleListView.as_view(), name='asset_bundles'),
    path('assets/bundles/<int:bundle_id>/', asset_views.AssetBundleDetailView.as_view(), name='asset_bundle_detail'),
    
    # Hierarchical Preprocessing
    # Legacy endpoints removed during refactoring.
    # Country preprocessing is now handled via:
    #   POST /api/preprocess_country/  (direct pipeline trigger)
    #   POST /api/run_pipeline/        (full WorldKG pipeline)
    
    # Search Update Pipeline
    path('search/update/', search_update_views.SearchUpdateView.as_view(), name='search_update'),
    path('search/update/status/<uuid:session_id>/', search_update_views.SearchUpdateStatusView.as_view(), name='search_update_status'),

    # Artifact Registry (TEMPORAL_SHARDING_ARTIFACT_PLAN.md Phase 3)
    # Note: '/api/artifacts/<str:country_code>/' (CountryArtifactsView) exists
    # below; these static 'artifacts/registry/...' routes MUST be declared
    # before it, otherwise <str:country_code> would swallow 'registry'.
    path('artifacts/registry/', views_artifact_registry.ArtifactListView.as_view(), name='artifact_registry_list'),
    path('artifacts/registry/by-stage/', views_artifact_registry.ArtifactAvailabilityView.as_view(), name='artifact_registry_availability'),
    path('artifacts/registry/<uuid:artifact_id>/', views_artifact_registry.ArtifactDetailView.as_view(), name='artifact_registry_detail'),
    path('task-results/<uuid:run_id>/', views_artifact_registry.TaskResultListView.as_view(), name='task_results'),

    path('artifacts/<str:country_code>/', search_update_views.CountryArtifactsView.as_view(), name='country_artifacts'),
    path('artifacts/monthly/check/<str:country_code>/', search_update_views.CheckMonthlyAvailabilityView.as_view(), name='check_monthly_availability'),
    
    # Country Search Update (Home Page Integration)
    path('country-search-update/', country_search_views.CountrySearchUpdateView.as_view(), name='country_search_update'),
    path('country-search-status/<str:country_name>/', country_search_views.CountrySearchStatusView.as_view(), name='country_search_status'),

    # Country Subgraphs (for subgraph-level pipelines)
    path('country-subgraphs/<str:country_name>/', country_search_views.CountrySubgraphsView.as_view(), name='country_subgraphs'),

    # Country Pre-Processing (Recipe Builder Integration)
    path('country-preprocess/', country_search_views.CountryPreProcessView.as_view(), name='country_preprocess'),

    # WorldKG Pipeline v1 (deprecated)
    path('worldkg-pipeline/start/', worldkg_pipeline_views.WorldKGPipelineStartView.as_view(), name='worldkg_pipeline_start'),
    path('worldkg-pipeline/status/<uuid:session_id>/', worldkg_pipeline_views.WorldKGPipelineStatusView.as_view(), name='worldkg_pipeline_status'),
    path('worldkg-pipeline/summary/<str:country_name>/', worldkg_pipeline_views.WorldKGPipelineSummaryView.as_view(), name='worldkg_pipeline_summary'),
    path('worldkg-pipeline/rejected-summary/<str:country_name>/', worldkg_pipeline_views.WorldKGPipelineRejectedSummaryView.as_view(), name='worldkg_pipeline_rejected_summary'),
    path('worldkg-pipeline/state/<str:country_name>/', worldkg_pipeline_views.WorldKGPipelineCountryStateView.as_view(), name='worldkg_pipeline_country_state'),
    path('worldkg-pipeline/validation-cost/<str:country_name>/', worldkg_pipeline_views.ValidationCostEstimateView.as_view(), name='worldkg_pipeline_validation_cost'),

    # WorldKG Pipeline v2 (Celery Canvas) — Country-Level
    path("worldkg-pipeline-v2/start/", worldkg_pipeline_views.WorldKGPipelineV2StartView.as_view(), name="worldkg_pipeline_v2_start"),

    # Planet Initialization (Celery Canvas async)
    path("planet/initialize/", worldkg_pipeline_views.PlanetInitializeView.as_view(), name="planet_initialize"),
    path("planet/status/<uuid:pipeline_run_id>/", worldkg_pipeline_views.PlanetInitStatusView.as_view(), name="planet_init_status"),
    path("planet/snapshot-dates/", worldkg_pipeline_views.SnapshotDatesView.as_view(), name="snapshot_dates"),

    # Snapshot Jobs (DB ground truth — TEMPORAL_SNAPSHOT_REFACTOR.md Phases C/D/E)
    path("snapshot-jobs/", worldkg_pipeline_views.SnapshotJobStatusView.as_view(), name="snapshot_jobs_list"),
    path("snapshot-jobs/<str:country_code>/", worldkg_pipeline_views.SnapshotJobStatusView.as_view(), name="snapshot_jobs_by_country"),
    path("snapshot-jobs/<str:country_code>/<str:snapshot_date>/", worldkg_pipeline_views.SnapshotJobStatusView.as_view(), name="snapshot_job_detail"),
    path("snapshot-jobs/<str:country_code>/<str:snapshot_date>/results/", worldkg_pipeline_views.SnapshotJobResultsView.as_view(), name="snapshot_job_results"),

    # System Summary
    path('system/summary/', views_system_summary.SystemSummaryView.as_view(), name='system_summary'),

    # Artifact Registry (TEMPORAL_SHARDING_ARTIFACT_PLAN.md Phase 3)
    # Note: '/api/artifacts/<country_code>/' already exists (CountryArtifactsView);
    # the new registry endpoints live under '/api/artifacts/registry/' to avoid
    # colliding with that route. 'by-stage/' is declared before '<uuid:artifact_id>'
    # so the static path wins (the uuid converter would otherwise reject 'by-stage').
    path('artifacts/registry/', views_artifact_registry.ArtifactListView.as_view(), name='artifact_registry_list'),
    path('artifacts/registry/by-stage/', views_artifact_registry.ArtifactAvailabilityView.as_view(), name='artifact_registry_availability'),
    path('artifacts/registry/<uuid:artifact_id>/', views_artifact_registry.ArtifactDetailView.as_view(), name='artifact_registry_detail'),
    path('task-results/<uuid:run_id>/', views_artifact_registry.TaskResultListView.as_view(), name='task_results'),

    # Projection weight assets
    path('projection-weights/', projection_weight_views.ProjectionWeightAssetListView.as_view(), name='projection_weight_list'),
    # NOTE: 'learn' must be declared before the dynamic '<str:country_code>' route
    path('projection-weights/learn/', projection_weight_views.ProjectionWeightLearnView.as_view(), name='projection_weight_learn'),
    path('projection-weights/<str:country_code>/', projection_weight_views.ProjectionWeightAssetDetailView.as_view(), name='projection_weight_detail'),

    # Augmented Data (Spatial Link Predictions + Augmentation Estimates)
    path('data/augmented-summary/<str:country_name>/', views_augmented_data.AugmentedDataSummaryView.as_view(), name='augmented_data_summary'),
    path('data/augmented-detail/<str:country_name>/', views_augmented_data.AugmentedDataDetailView.as_view(), name='augmented_data_detail'),
]
