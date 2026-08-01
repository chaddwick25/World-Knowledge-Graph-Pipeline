"""Planet initialization pipeline steps"""
from __future__ import annotations
from pipeline.celery_app import CELERY_AVAILABLE

if CELERY_AVAILABLE:
    from .step_0_base import (  # noqa: F401
        step_0_initialize_planet,
        step_0b_initialize_continent,
        step_0c_prebuild_structure,
        step_0d_prebuild_country_paths,
        step_0e_prebuild_subgraphs,
        step_0f_prebuild_wikidata_ids,
        step_0l_enrich_worldkg_classes,
        step_0m_generate_osm_boundaries,
    )
    from .step_0g_continent_snapshots import (  # noqa: F401
        step_0g_extract_continent_snapshots,
    )
    from .step_0h_prebuild_embeddings import (  # noqa: F401
        step_0h_scan_embeddings,
        step_0h_copy_gb_to_uk,
        step_0i_prebuild_split_embeddings,
        step_0j_prebuild_merge_us_embeddings,
        step_0k_rescan_embeddings,
    )
    from .step_finalize_planet_init import (  # noqa: F401
        _finalize_planet_init_chain,
    )
else:
    def _no_celery_error(*args, **kwargs):  # type: ignore[override]
        raise ImportError(
            "Celery is required to run planet initialization tasks. "
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
