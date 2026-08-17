"""
WorldKG Pipeline — Celery Tasks Package

Planet initialization (formerly step_0a..step_0m) is now handled by the
``init_planet`` management command, run as a Docker entrypoint step on
the backend container after migrations. See
``docs/plans/CORE_APP_CONSOLIDATION_PLAN.md`` and
``core/management/commands/init_planet.py``.
"""
from __future__ import annotations

# ══════════════════════════════════════════════════════════════════════════
# Steps 1–6 — Country Pipeline
# ══════════════════════════════════════════════════════════════════════════
from pipeline.tasks.country_pipeline_steps.step_1_embed import (  # noqa: F401
    step_1_embed_osm_entities,
    _embed_subgraph,
    step_1b_finalize_subgraph_embeds,
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
    step_5b_finalize_subgraph_nle,
)
from pipeline.tasks.country_pipeline_steps.step_6_ready import (  # noqa: F401
    step_6_mark_search_ready,
)
