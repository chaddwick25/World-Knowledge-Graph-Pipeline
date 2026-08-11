"""
WorldKG Pipeline — Celery Tasks Package
"""
from __future__ import annotations

# ══════════════════════════════════════════════════════════════════════════
# Step 0 — Planet / Continent / Pre-build
# ══════════════════════════════════════════════════════════════════════════
from pipeline.tasks.planet_initialization_pipeline_steps import (  # noqa: F401
    step_0_initialize_planet,
    step_0b_initialize_continent,
    step_0c_prebuild_structure,
    step_0d_prebuild_country_paths,
    step_0e_prebuild_subgraphs,
    step_0f_prebuild_wikidata_ids,
    step_0h_scan_embeddings,
    step_0h_copy_gb_to_uk,
    step_0i_prebuild_split_embeddings,
    step_0j_prebuild_merge_us_embeddings,
    step_0k_rescan_embeddings,
    step_0l_enrich_worldkg_classes,
    step_0m_generate_osm_boundaries,
    _finalize_planet_init_chain,
)

# ══════════════════════════════════════════════════════════════════════════
# Steps 1–6 — Country Pipeline
# ══════════════════════════════════════════════════════════════════════════
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
