from __future__ import annotations
import logging
from pathlib import Path
from django.conf import settings
from pipeline.config import CountryConfig
from django.core.management import call_command
from pipeline.tasks.helper import _log, _push_update, create_planet_run_record
from pipeline.celery_app import (
    celery_app,
    PipelineTask,
    CELERY_AVAILABLE,
)

if CELERY_AVAILABLE:
    logger = logging.getLogger("pipeline")
    @celery_app.task(
        bind=True, base=PipelineTask,
        name="step_0_initialize_planet",
        max_retries=1, default_retry_delay=300,
    )
    def step_0_initialize_planet(self, config_dict: dict) -> dict:
        """Step 0: Initialize Planet — file structure, metrics, Wikidata alignment."""
        config_dict = self.setup_pipeline_context(config_dict)
        cfg = CountryConfig.from_dict(config_dict)

        _push_update(
            pipeline_run_id=cfg.pipeline_run_id,
            name="planet_init", status="in_progress",
            message="Initializing planet PBF and Wikidata alignment...",
            pct=10, step=0,
        )

        _log(
            logger,
            "info",
            "Step 0: Initialize Planet",
            pipeline_run_id=cfg.pipeline_run_id,
            planet_pbf_path=cfg.pbf_path or settings.PLANET_OSM_FILE_PATH,
        )

        from extraction.services.planet_initialization_service import (
            PlanetInitializationService,
        )
        # TODO: Update the code to use the path for the poly files
        # TODO: remove the embeddings directory (I dont thinks its used )
        policy = {
            "stages": {
                "planet_initialization": {
                    "parameters": {
                        "planet_pbf_path": cfg.pbf_path or settings.PLANET_OSM_FILE_PATH,
                        "extract_continents": False,
                        "pipeline_run_id": cfg.pipeline_run_id or "",
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

        create_planet_run_record(cfg)

        _push_update(
            pipeline_run_id=cfg.pipeline_run_id,
            name="planet_init", status="completed",
            message="Planet initialization complete.",
            pct=100, step=0,
        )

        _log(
            logger,
            "info",
            "Step 0 complete",
            pipeline_run_id=cfg.pipeline_run_id,
            planet_pbf=str(result.get("planet_pbf")),
            osm_hierarchy=str(result.get("osm_wikidata_hierarchy")),
        )

        return cfg.to_dict()


    @celery_app.task(
        bind=True, base=PipelineTask,
        name="step_0b_initialize_continent",
        max_retries=2, default_retry_delay=120,
    )
    def step_0b_initialize_continent(self, config_dict: dict) -> dict:
        """Step 0.5: Extract continent PBFs from the planet file."""
        
        config_dict = self.setup_pipeline_context(config_dict)
        cfg = CountryConfig.from_dict(config_dict)
        _log(
            logger,
            "info",
            "Step 0.5: Initialize Continents",
            pipeline_run_id=cfg.pipeline_run_id,
        )
        from extraction.services.planet_initialization_service import (
            PlanetInitializationService,
        )
        policy = {
            "stages": {
                "planet_initialization": {
                    "parameters": {
                        "planet_pbf_path": cfg.pbf_path or settings.PLANET_OSM_FILE_PATH,
                        "pipeline_run_id": cfg.pipeline_run_id or "",
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
            pipeline_run_id=cfg.pipeline_run_id,
        )
        _push_update(
            pipeline_run_id=cfg.pipeline_run_id,
            name="continent_init", status="completed",
            message="Continent extraction complete.",
            pct=100, step=1,
        )
        return cfg.to_dict()


    # ══════════════════════════════════════════════════════════════════════
    # Pre-build steps (0.6–0.95)
    # ══════════════════════════════════════════════════════════════════════
    @celery_app.task(
        bind=True, base=PipelineTask,
        name="step_0c_prebuild_structure",
        max_retries=1, default_retry_delay=60,
    )
    def step_0c_prebuild_structure(self, config_dict: dict) -> dict:
        """Step 0.6: Pre-build CountryPipelineProfile from country_relations.json.

        Runs the ``prebuild_worldkg_structure`` management command to create
        CountryPipelineProfile and SubgraphProfile skeleton records.
        """
        config_dict = self.setup_pipeline_context(config_dict)
        cfg = CountryConfig.from_dict(config_dict)
        _log(
            logger,
            "info",
            "Step 0.6: Pre-build WorldKG structure",
            pipeline_run_id=cfg.pipeline_run_id,
        )
        call_command("prebuild_worldkg_structure")
        _push_update(
            pipeline_run_id=cfg.pipeline_run_id,
            name="prebuild_structure", status="completed",
            message="WorldKG structure pre-built.",
            pct=100,
        )
        _log(
            logger,
            "info",
            "Step 0.6 complete",
            pipeline_run_id=cfg.pipeline_run_id,
        )
        return cfg.to_dict()


    @celery_app.task(
        bind=True, base=PipelineTask,
        name="step_0d_prebuild_country_paths",
        max_retries=1, default_retry_delay=60,
    )
    def step_0d_prebuild_country_paths(self, config_dict: dict) -> dict:
        """Step 0.7: Resolve TSV, PBF, and pickle paths on CountryPipelineProfile.

        Runs the ``prebuild_country_paths`` management command.
        """
        config_dict = self.setup_pipeline_context(config_dict)
        cfg = CountryConfig.from_dict(config_dict)
        _log(
            logger,
            "info",
            "Step 0.7: Pre-build country paths",
            pipeline_run_id=cfg.pipeline_run_id,
        )
        call_command("prebuild_country_paths")
        _push_update(
            pipeline_run_id=cfg.pipeline_run_id,
            name="prebuild_country_paths", status="completed",
            message="Country paths resolved.",
            pct=100,
        )
        _log(
            logger,
            "info",
            "Step 0.7 complete",
            pipeline_run_id=cfg.pipeline_run_id,
        )
        return cfg.to_dict()


    @celery_app.task(
        bind=True, base=PipelineTask,
        name="step_0e_prebuild_subgraphs",
        max_retries=1, default_retry_delay=60,
    )
    def step_0e_prebuild_subgraphs(self, config_dict: dict) -> dict:
        """Step 0.8: Generate SubgraphProfile rows from Geofabrik hierarchy.

        Runs the ``prebuild_subgraphs`` management command.
        """
        config_dict = self.setup_pipeline_context(config_dict)
        cfg = CountryConfig.from_dict(config_dict)
        _log(
            logger,
            "info",
            "Step 0.8: Pre-build subgraphs",
            pipeline_run_id=cfg.pipeline_run_id,
        )
        call_command("prebuild_subgraphs")
        _push_update(
            pipeline_run_id=cfg.pipeline_run_id,
            name="prebuild_subgraphs", status="completed",
            message="Subgraph profiles generated.",
            pct=100,
        )

        _log(
            logger,
            "info",
            "Step 0.8 complete",
            pipeline_run_id=cfg.pipeline_run_id,
        )
        return cfg.to_dict()


    @celery_app.task(
        bind=True, base=PipelineTask,
        name="step_0f_prebuild_wikidata_ids",
        max_retries=1, default_retry_delay=60,
    )
    def step_0f_prebuild_wikidata_ids(self, config_dict: dict) -> dict:
        """Step 0.95: Backfill Q-IDs from relations + hierarchy.

        Runs the ``prebuild_wikidata_ids`` management command to enrich
        CountryPipelineProfile and SubgraphProfile with Wikidata identifiers.
        """
        config_dict = self.setup_pipeline_context(config_dict)
        cfg = CountryConfig.from_dict(config_dict)
        _log(
            logger,
            "info",
            "Step 0.95: Pre-build Wikidata IDs",
            pipeline_run_id=cfg.pipeline_run_id,
        )
        call_command("prebuild_wikidata_ids")
        _push_update(
            pipeline_run_id=cfg.pipeline_run_id,
            name="prebuild_wikidata_ids", status="completed",
            message="Wikidata IDs backfilled.",
            pct=100,
        )
        _log(
            logger,
            "info",
            "Step 0.95 complete",
            pipeline_run_id=cfg.pipeline_run_id,
        )
        return cfg.to_dict()


    @celery_app.task(
        bind=True, base=PipelineTask,
        name="step_0l_enrich_worldkg_classes",
        max_retries=1, default_retry_delay=60,
    )
    def step_0l_enrich_worldkg_classes(self, config_dict: dict) -> dict:
        """Step 0.96: Enrich WorldKG classes from TTL ontology.

        Runs the ``enrich_worldkg_classes`` management command to load
        the WorldKG ontology TTL file into Redis for fast lookup.
        """
        config_dict = self.setup_pipeline_context(config_dict)
        cfg = CountryConfig.from_dict(config_dict)
        _log(
            logger,
            "info",
            "Step 0.96: Enrich WorldKG classes",
            pipeline_run_id=cfg.pipeline_run_id,
        )
        from django.conf import settings
        call_command(
            "enrich_worldkg_classes",
            "--load-from-ttl",
            settings.WORLDKG_ONTOLOGY_PATH,
        )
        _push_update(
            pipeline_run_id=cfg.pipeline_run_id,
            name="enrich_worldkg_classes", status="completed",
            message="WorldKG classes enriched from TTL.",
            pct=100,
        )
        _log(
            logger,
            "info",
            "Step 0.96 complete",
            pipeline_run_id=cfg.pipeline_run_id,
        )
        return cfg.to_dict()


    @celery_app.task(
        bind=True, base=PipelineTask,
        name="step_0m_generate_osm_boundaries",
        max_retries=1, default_retry_delay=60,
    )
    def step_0m_generate_osm_boundaries(self, config_dict: dict) -> dict:
        """Step 0.97: Generate OSM boundaries.

        Runs the ``generate_osm_boundaries`` management command to populate
        boundary data for countries and regions.
        """
        config_dict = self.setup_pipeline_context(config_dict)
        cfg = CountryConfig.from_dict(config_dict)
        _log(
            logger,
            "info",
            "Step 0.97: Generate OSM boundaries",
            pipeline_run_id=cfg.pipeline_run_id,
        )
        call_command("generate_osm_boundaries")
        _push_update(
            pipeline_run_id=cfg.pipeline_run_id,
            name="generate_osm_boundaries", status="completed",
            message="OSM boundaries generated.",
            pct=100,
        )
        _log(
            logger,
            "info",
            "Step 0.97 complete",
            pipeline_run_id=cfg.pipeline_run_id,
        )
        return cfg.to_dict()
    