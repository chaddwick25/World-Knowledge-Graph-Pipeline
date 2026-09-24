from django.urls import path
from . import views
from . import views_auth
from . import country_search_views
from . import views_augmented_data
from . import views_system_summary
from .services import views_app_state
from .views import (
    InitialStatusView,
    SystemStatusView,
    RegionMapDataView,
)

# Core API Endpoints - E2E Tested
urlpatterns = [
    # Session auth (SPA login guard)
    path('auth/csrf/', views_auth.csrf, name='auth_csrf'),
    path('auth/login/', views_auth.login_view, name='auth_login'),
    path('auth/logout/', views_auth.logout_view, name='auth_logout'),
    path('auth/register/', views_auth.register_view, name='auth_register'),
    path('auth/me/', views_auth.me, name='auth_me'),

    # Initial Status
    path('status/initial/', InitialStatusView.as_view(), name='initial_status'),

    # App State (frontend bootstrap — replaces multiple /status/initial/ + /state/ calls)
    path('app-state/<str:country_name>/', views_app_state.AppStateView.as_view(), name='app_state'),

    # System Status (planet-init readiness + home-page hydration)
    path('system/status/', SystemStatusView.as_view(), name='system_status'),

    # Recipe Builder - Region Map Data
    path('recipes/regions-map-data/', RegionMapDataView.as_view(), name='region_map_data'),

    # Country Search Status (Home Page Integration)
    path('country-search-status/<str:country_name>/', country_search_views.CountrySearchStatusView.as_view(), name='country_search_status'),

    # Country Pre-Processing (Recipe Builder Integration)
    path('country-preprocess/', country_search_views.CountryPreProcessView.as_view(), name='country_preprocess'),

    # WorldKG Pipeline state (v1 surface still used by the frontend)
    path('worldkg-pipeline/state/<str:country_name>/', views.WorldKGPipelineCountryStateView.as_view(), name='worldkg_pipeline_country_state'),

    # WorldKG Pipeline v2 (Celery Canvas) — Country-Level
    path("worldkg-pipeline-v2/start/", views.WorldKGPipelineV2StartView.as_view(), name="worldkg_pipeline_v2_start"),

    # Planet snapshots
    path("planet/snapshot-dates/", views.SnapshotDatesView.as_view(), name="snapshot_dates"),

    # Snapshot Jobs (DB ground truth — TEMPORAL_SNAPSHOT_REFACTOR.md Phases C/D/E)
    path("snapshot-jobs/", views.SnapshotJobStatusView.as_view(), name="snapshot_jobs_list"),
    path("snapshot-jobs/<str:country_code>/", views.SnapshotJobStatusView.as_view(), name="snapshot_jobs_by_country"),
    path("snapshot-jobs/<str:country_code>/<str:snapshot_date>/", views.SnapshotJobStatusView.as_view(), name="snapshot_job_detail"),
    path("snapshot-jobs/<str:country_code>/<str:snapshot_date>/results/", views.SnapshotJobResultsView.as_view(), name="snapshot_job_results"),

    # System Summary
    path('system/summary/', views_system_summary.SystemSummaryView.as_view(), name='system_summary'),

    # Augmented Data (Spatial Link Predictions + Augmentation Estimates)
    path('data/augmented-summary/<str:country_name>/', views_augmented_data.AugmentedDataSummaryView.as_view(), name='augmented_data_summary'),
    path('data/augmented-links-geom/<str:country_name>/', views_augmented_data.AugmentedLinksGeomView.as_view(), name='augmented_links_geom'),
]
