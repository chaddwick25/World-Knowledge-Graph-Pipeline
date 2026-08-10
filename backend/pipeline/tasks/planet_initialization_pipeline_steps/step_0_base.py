from __future__ import annotations
import logging
from pathlib import Path
from django.conf import settings
from pipeline.envelopes import PlanetEnvelope
from pipeline.task_decorator import pipeline_step
from django.core.management import call_command
from pipeline.tasks.helper import _log, _push_update, create_planet_run_record
from pipeline.celery_app import (
    pipeline_task,
    PipelineTask,
)

logger = logging.getLogger("pipeline")

@pipeline_task(
    bind=True, base=PipelineTask,
    name="step_0_initialize_planet",
    max_retries=1, default_retry_delay=300,
)
@pipeline_step("planet_init", PlanetEnvelope, 0.0)
def step_0_initialize_planet(self, env: PlanetEnvelope) -> PlanetEnvelope:
    """Step 0: Initialize Planet — file structure, metrics, Wikidata alignment."""
    from extraction.services.planet_initialization_service import (
        PlanetInitializationService,
    )
    # TODO: Update the code to use the path for the poly files
    # TODO: remove the embeddings directory (I dont thinks its used )
    policy = {
        "stages": {
            "planet_initialization": {
                "parameters": {
                    "planet_pbf_path": env.pbf_path or settings.PLANET_OSM_FILE_PATH,
                    "extract_continents": False,
                    "pipeline_run_id": env.pipeline_run_id or "",
                },
                "file_structure": {
                    "base_dir": str(Path(settings.BASE_DATA_DIR)),
                    "directories": {
                        "osm_pbf": "OSM-PBF-FILES",
                        "osm_wikidata": "OSM-PBF-FILES/osm_wikidata_extractions",
                        "polygons": "data/osm_polygon_files",
                        "logs": "logs",
                        "embeddings": "data/embeddings",
                        "wikidata": "data/wikidata_cache",
                    },
                },
                "planetary_metrics": {"enabled": True},
            }
        }
    }

    service = PlanetInitializationService(policy)
    result = service.execute()

    create_planet_run_record(env)

    _log(
        logger,
        "info",
        "Step 0 complete",
        pipeline_run_id=env.pipeline_run_id,
        planet_pbf=str(result.get("planet_pbf")),
        osm_hierarchy=str(result.get("osm_wikidata_hierarchy")),
    )

    return env


@pipeline_task(
    bind=True, base=PipelineTask,
    name="step_0b_initialize_continent",
    max_retries=2, default_retry_delay=120,
)
@pipeline_step("continent_init", PlanetEnvelope, 0.5)
def step_0b_initialize_continent(self, env: PlanetEnvelope) -> PlanetEnvelope:
    """Step 0.5: Extract continent PBFs from the planet file."""
    from extraction.services.planet_initialization_service import (
        PlanetInitializationService,
    )
    policy = {
        "stages": {
            "planet_initialization": {
                "parameters": {
                    "planet_pbf_path": env.pbf_path or settings.PLANET_OSM_FILE_PATH,
                    "pipeline_run_id": env.pipeline_run_id or "",
                },
                "file_structure": {
                    "base_dir": str(Path(settings.BASE_DATA_DIR)),
                },
            }
        }
    }
    service = PlanetInitializationService(policy)
    service._extract_continents_simple()
    _log(
        logger,
        "info",
        "Step 0.5 complete - continents extracted",
        pipeline_run_id=env.pipeline_run_id,
    )
    return env.with_state(continents_extracted=True)


# ══════════════════════════════════════════════════════════════════════
# Pre-build steps (0.6–0.95)
# ══════════════════════════════════════════════════════════════════════
@pipeline_task(
    bind=True, base=PipelineTask,
    name="step_0c_prebuild_structure",
    max_retries=1, default_retry_delay=60,
)
@pipeline_step("prebuild_structure", PlanetEnvelope, 0.6)
def step_0c_prebuild_structure(self, env: PlanetEnvelope) -> PlanetEnvelope:
    """Step 0.6: Pre-build CountryPipelineProfile from country_relations.json.

    Runs the ``prebuild_worldkg_structure`` management command to create
    CountryPipelineProfile and SubgraphProfile skeleton records.
    """
    call_command("prebuild_worldkg_structure")
    return env


@pipeline_task(
    bind=True, base=PipelineTask,
    name="step_0d_prebuild_country_paths",
    max_retries=1, default_retry_delay=60,
)
@pipeline_step("prebuild_country_paths", PlanetEnvelope, 0.7)
def step_0d_prebuild_country_paths(self, env: PlanetEnvelope) -> PlanetEnvelope:
    """Step 0.7: Resolve TSV, PBF, and pickle paths on CountryPipelineProfile.

    Runs the ``prebuild_country_paths`` management command.
    """
    call_command("prebuild_country_paths")
    return env


@pipeline_task(
    bind=True, base=PipelineTask,
    name="step_0e_prebuild_subgraphs",
    max_retries=1, default_retry_delay=60,
)
@pipeline_step("prebuild_subgraphs", PlanetEnvelope, 0.8)
def step_0e_prebuild_subgraphs(self, env: PlanetEnvelope) -> PlanetEnvelope:
    """Step 0.8: Generate SubgraphProfile rows from Geofabrik hierarchy.

    Runs the ``prebuild_subgraphs`` management command.
    """
    call_command("prebuild_subgraphs")
    return env


@pipeline_task(
    bind=True, base=PipelineTask,
    name="step_0f_prebuild_wikidata_ids",
    max_retries=1, default_retry_delay=60,
)
@pipeline_step("prebuild_wikidata_ids", PlanetEnvelope, 0.95)
def step_0f_prebuild_wikidata_ids(self, env: PlanetEnvelope) -> PlanetEnvelope:
    """Step 0.95: Backfill Q-IDs from relations + hierarchy.

    Runs the ``prebuild_wikidata_ids`` management command to enrich
    CountryPipelineProfile and SubgraphProfile with Wikidata identifiers.
    """
    call_command("prebuild_wikidata_ids")
    return env


@pipeline_task(
    bind=True, base=PipelineTask,
    name="step_0l_enrich_worldkg_classes",
    max_retries=1, default_retry_delay=60,
)
@pipeline_step("enrich_worldkg_classes", PlanetEnvelope, 0.96)
def step_0l_enrich_worldkg_classes(self, env: PlanetEnvelope) -> PlanetEnvelope:
    """Step 0.96: Enrich WorldKG classes from TTL ontology.

    Runs the ``enrich_worldkg_classes`` management command to load
    the WorldKG ontology TTL file into Redis for fast lookup.
    """
    call_command(
        "enrich_worldkg_classes",
        "--load-from-ttl",
        settings.WORLDKG_ONTOLOGY_PATH,
    )
    return env


@pipeline_task(
    bind=True, base=PipelineTask,
    name="step_0m_generate_osm_boundaries",
    max_retries=1, default_retry_delay=60,
)
@pipeline_step("generate_osm_boundaries", PlanetEnvelope, 0.97)
def step_0m_generate_osm_boundaries(self, env: PlanetEnvelope) -> PlanetEnvelope:
    """Step 0.97: Generate OSM boundaries.

    Runs the ``generate_osm_boundaries`` management command to populate
    boundary data for countries and regions.
    """
    call_command("generate_osm_boundaries")
    return env


@pipeline_task(
    bind=True, base=PipelineTask,
    name="step_0n_backfill_partition_keys",
    max_retries=1, default_retry_delay=60,
)
@pipeline_step("backfill_partition_keys", PlanetEnvelope, 0.98)
def step_0n_backfill_partition_keys(self, env: PlanetEnvelope) -> PlanetEnvelope:
    """Step 0.98: Backfill snapshot_id + country_code on OsmEntity.

    Runs after OSM boundaries are generated (step_0m) so that bbox data
    is available for the spatial join.  Idempotent — only updates NULL rows.

    On a fresh DB this is a no-op (no rows to backfill).  On a DB with
    existing monolith data, it populates the partition keys needed for
    ``create_country_partitions`` to route rows into the correct leaf.
    """
    call_command("backfill_partition_keys", force_country_code=True)
    return env
