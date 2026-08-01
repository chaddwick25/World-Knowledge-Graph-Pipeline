"""
WorldKG Pipeline — Celery Tasks Package
"""
from __future__ import annotations
from pipeline.celery_app import CELERY_AVAILABLE

# TODO: refactor and remove the multiple if statements
if CELERY_AVAILABLE:
    from pipeline.tasks.planet_initialization_pipeline_steps.step_finalize_planet_init import (
        _finalize_planet_init_chain,
    )
else:
    _finalize_planet_init_chain = _no_celery_error


# ══════════════════════════════════════════════════════════════════════════
# Step 0 — Planet / Continent / Pre-build
# ══════════════════════════════════════════════════════════════════════════
if CELERY_AVAILABLE:
    from pipeline.tasks.planet_initialization_pipeline_steps import (
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
    )
else:
    def _no_celery_error(*args, **kwargs):
        raise ImportError(
            "Celery is required to run pipeline tasks. "
            "Install with: pip install celery"
        )
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

# ══════════════════════════════════════════════════════════════════════════
# Step 1 — Embed OSM Entities
# ══════════════════════════════════════════════════════════════════════════
if CELERY_AVAILABLE:
    from pipeline.tasks.country_pipeline_steps.step_1_embed import (
        step_1_embed_osm_entities,
        _embed_subgraph,
    )
else:
    def _embed_subgraph(*args, **kwargs):
        raise ImportError(
            "Celery is required to run pipeline tasks. "
            "Install with: pip install celery"
        )
    step_1_embed_osm_entities = _no_celery_error

# ══════════════════════════════════════════════════════════════════════════
# Step 2 — Harvest Wikidata
# ══════════════════════════════════════════════════════════════════════════

if CELERY_AVAILABLE:
    from pipeline.tasks.country_pipeline_steps.step_2_harvest import step_2_harvest_wikidata
else:
    step_2_harvest_wikidata = _no_celery_error

# ══════════════════════════════════════════════════════════════════════════
# Step 3 — Run IGEA
# ══════════════════════════════════════════════════════════════════════════

if CELERY_AVAILABLE:
    from pipeline.tasks.country_pipeline_steps.step_3_igea import step_3_run_igea
else:
    step_3_run_igea = _no_celery_error

# ══════════════════════════════════════════════════════════════════════════
# Step 4 — Predict Spatial Links (USLP)
# ══════════════════════════════════════════════════════════════════════════

if CELERY_AVAILABLE:
    from pipeline.tasks.country_pipeline_steps.step_4_uslp import (
        step_4_predict_spatial_links,
        _run_subgraph_uslp,
        step_4b_finalize_subgraph_uslp,
    )
else:
    step_4_predict_spatial_links = _no_celery_error

    def _run_subgraph_uslp(*args, **kwargs):
        raise ImportError(
            "Celery is required to run pipeline tasks. "
            "Install with: pip install celery"
        )

    def step_4b_finalize_subgraph_uslp(*args, **kwargs):
        raise ImportError(
            "Celery is required to run pipeline tasks. "
            "Install with: pip install celery"
        )

# ══════════════════════════════════════════════════════════════════════════
# Step 5 — Train GV-NLE
# ══════════════════════════════════════════════════════════════════════════

if CELERY_AVAILABLE:
    from pipeline.tasks.country_pipeline_steps.step_5_nle import (
        step_5_train_gv_nle,
        _train_subgraph_gv_nle,
    )
else:
    step_5_train_gv_nle = _no_celery_error

    def _train_subgraph_gv_nle(*args, **kwargs):
        raise ImportError(
            "Celery is required to run pipeline tasks. "
            "Install with: pip install celery"
        )

# ══════════════════════════════════════════════════════════════════════════
# Step 6 — Mark Search Ready
# ══════════════════════════════════════════════════════════════════════════

if CELERY_AVAILABLE:
    from pipeline.tasks.country_pipeline_steps.step_6_ready import step_6_mark_search_ready
else:
    step_6_mark_search_ready = _no_celery_error
