"""
WorldKG Pipeline — Celery Tasks Package
"""
from __future__ import annotations
from pipeline.celery_app import CELERY_AVAILABLE


def _no_celery_error(*args, **kwargs):
    raise ImportError(
        "Celery is required to run pipeline tasks. "
        "Install with: pip install celery"
    )


# ══════════════════════════════════════════════════════════════════════════
# Step 0 — Planet / Continent / Pre-build
# ══════════════════════════════════════════════════════════════════════════
if CELERY_AVAILABLE:
    from pipeline.tasks.planet_initialization_pipeline_steps import (  # noqa: F401
        step_0_initialize_planet,
        step_0b_initialize_continent,
        step_0c_prebuild_structure,
        step_0d_prebuild_country_paths,
        step_0e_prebuild_subgraphs,
        step_0f_prebuild_wikidata_ids,
        step_0g_extract_continent_snapshots,
        step_0h_scan_embeddings,
        step_0h_copy_gb_to_uk,
        step_0i_prebuild_split_embeddings,
        step_0j_prebuild_merge_us_embeddings,
        step_0k_rescan_embeddings,
        step_0l_enrich_worldkg_classes,
        step_0m_generate_osm_boundaries,
        _finalize_planet_init_chain,
    )
else:
    step_0_initialize_planet = _no_celery_error
    step_0b_initialize_continent = _no_celery_error
    step_0c_prebuild_structure = _no_celery_error
    step_0d_prebuild_country_paths = _no_celery_error
    step_0e_prebuild_subgraphs = _no_celery_error
    step_0f_prebuild_wikidata_ids = _no_celery_error
    step_0g_extract_continent_snapshots = _no_celery_error
    step_0h_scan_embeddings = _no_celery_error
    step_0h_copy_gb_to_uk = _no_celery_error
    step_0i_prebuild_split_embeddings = _no_celery_error
    step_0j_prebuild_merge_us_embeddings = _no_celery_error
    step_0k_rescan_embeddings = _no_celery_error
    step_0l_enrich_worldkg_classes = _no_celery_error
    step_0m_generate_osm_boundaries = _no_celery_error
    _finalize_planet_init_chain = _no_celery_error

# ══════════════════════════════════════════════════════════════════════════
# Steps 1–6 — Country Pipeline
# ══════════════════════════════════════════════════════════════════════════
if CELERY_AVAILABLE:
    from pipeline.tasks.country_pipeline_steps.step_1_embed import (  # noqa: F401
        step_1_embed_osm_entities,
        _embed_subgraph,
    )
    from pipeline.tasks.country_pipeline_steps.step_2_harvest import (  # noqa: F401
        step_2_harvest_wikidata,
    )
    from pipeline.tasks.country_pipeline_steps.step_3_igea import (  # noqa: F401
        step_3_run_igea,
    )
    from pipeline.tasks.country_pipeline_steps.step_4_uslp import (  # noqa: F401
        step_4_predict_spatial_links,
        _run_subgraph_uslp,
        step_4b_finalize_subgraph_uslp,
    )
    from pipeline.tasks.country_pipeline_steps.step_5_nle import (  # noqa: F401
        step_5_train_gv_nle,
        _train_subgraph_gv_nle,
    )
    from pipeline.tasks.country_pipeline_steps.step_6_ready import (  # noqa: F401
        step_6_mark_search_ready,
    )
else:
    step_1_embed_osm_entities = _no_celery_error
    _embed_subgraph = _no_celery_error
    step_2_harvest_wikidata = _no_celery_error
    step_3_run_igea = _no_celery_error
    step_4_predict_spatial_links = _no_celery_error
    _run_subgraph_uslp = _no_celery_error
    step_4b_finalize_subgraph_uslp = _no_celery_error
    step_5_train_gv_nle = _no_celery_error
    _train_subgraph_gv_nle = _no_celery_error
    step_6_mark_search_ready = _no_celery_error
