from django.urls import path
from . import views

urlpatterns = [
    # Ontology endpoints
    path('ontology/info/', views.worldkg_ontology_info, name='nca_ontology_info'),
    path('ontology/class/<str:class_name>/', views.worldkg_class_hierarchy, name='nca_class_hierarchy'),
    
    # Entity enrichment
    path('enrich/entity/', views.worldkg_enrich_entity, name='nca_enrich_entity'),
    
    # Entity queries
    path('entities/', views.worldkg_entities_by_class, name='nca_query_entities'),
    # Visualization data (DECKGL_VISUALIZATION_INTEGRATION_PLAN_V2.md Phase 1)
    path('class-centroids/', views.worldkg_class_centroids, name='nca_class_centroids'),
    path('entities/detail/<str:osm_type>/<int:osm_id>/', views.worldkg_entity_detail, name='nca_entity_detail'),
    path('distribution/', views.worldkg_class_distribution, name='nca_class_distribution'),
    path('semantic-query/plan/', views.worldkg_semantic_query_plan, name='nca_semantic_query_plan'),
    path('semantic-triplet-search/', views.worldkg_semantic_triplet_search, name='nca_semantic_triplet_search'),
    path('similar-entities/', views.worldkg_similar_entities, name='nca_similar_entities'),
    path('execute-query/', views.execute_query, name='nca_execute_query'),
    path('execute-query/stream/', views.execute_query_stream, name='nca_execute_query_stream'),
    path('research/stream/', views.research_stream, name='nca_research_stream'),
    path('research/chat/', views.research_chat, name='nca_research_chat'),
    path('research/finalize/', views.research_finalize, name='nca_research_finalize'),
    path('research/seed-examples/', views.research_seed_examples, name='nca_research_seed_examples'),
    path('research/runs/', views.research_runs, name='nca_research_runs'),
    path('research/runs/<uuid:run_id>/', views.research_run_detail, name='nca_research_run_detail'),
    path('factor-availability/', views.factor_availability, name='nca_factor_availability'),
    path('subdivisions/', views.worldkg_subdivisions, name='nca_subdivisions'),
    path('sample-questions/', views.sample_questions, name='nca_sample_questions'),
    # Graph / spectral / community / event queries (Phase 4)
    path('spectral-query/', views.spectral_query, name='nca_spectral_query'),
    path('temporal-query/', views.temporal_query, name='nca_temporal_query'),
    path('community-query/', views.community_query, name='nca_community_query'),
    path('event-diffusion-query/', views.event_diffusion_query, name='nca_event_diffusion_query'),
    path('link-candidates/', views.worldkg_link_candidates, name='nca_link_candidates'),
    path('apply-link/', views.worldkg_apply_link, name='nca_apply_link'),
    
    # Temporal analysis (read-only; computation is pipeline-side — Step 6
    # + the compute_class_drift backfill command)
    path('drift/', views.worldkg_drift_list, name='nca_list_drift'),
]
