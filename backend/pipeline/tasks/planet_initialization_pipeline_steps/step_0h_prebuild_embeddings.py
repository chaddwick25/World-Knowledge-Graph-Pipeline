"""
Celery tasks: Embedding Pre-build Steps

These steps handle embedding-related pre-build work during planet initialization:
  - step_0h_scan_embeddings      — Scan EMBEDDINGS_ROOT, populate EligibleCountry rows
  - step_0h_copy_gb_to_uk        — Copy great-britain TSVs to united-kingdom naming
  - step_0i_prebuild_split_embeddings — Split multi-country TSVs (GB, Malaysia/Singapore/Brunei) using shapely spatial splitter
  - step_0j_prebuild_merge_us_embeddings — Merge 5 US regional shards into single US TSVs

They sit conceptually between prebuild_country_paths (0.7) and prebuild_subgraphs (0.8)
because the TSV splits/merges must happen before subgraph profiles need resolved paths.

Splitting uses the shapely spatial splitter (--backend shapely) for 10-50x performance
improvement over the legacy pyosmium scanner.
"""

from __future__ import annotations

import logging
from pathlib import Path
from django.conf import settings
from pipeline.envelopes import PlanetEnvelope
from pipeline.task_decorator import pipeline_step
from django.core.management import call_command
from pipeline.tasks.helper import _log, _push_update
from pipeline.celery_app import (
    pipeline_task,
    PipelineTask,
)

logger = logging.getLogger("pipeline")

@pipeline_task(
    bind=True, base=PipelineTask,
    name="step_0h_scan_embeddings",
    max_retries=1, default_retry_delay=60,
)
@pipeline_step("prebuild_scan_embeddings", PlanetEnvelope, 0.75)
def step_0h_scan_embeddings(self, env: PlanetEnvelope) -> PlanetEnvelope:
    """Step 0.75: Scan EMBEDDINGS_ROOT and populate EligibleCountry rows.

    Runs the ``scan_embeddings`` management command to check every
    target country's embedding availability (ready / needs split /
    needs merge / no embeddings), which drives map colouring.
    """
    call_command("scan_embeddings", clear=True)
    return env


@pipeline_task(
    bind=True, base=PipelineTask,
    name="step_0h_copy_gb_to_uk",
    max_retries=1, default_retry_delay=60,
)
@pipeline_step("prebuild_copy_gb_to_uk", PlanetEnvelope, 0.755)
def step_0h_copy_gb_to_uk(self, env: PlanetEnvelope) -> PlanetEnvelope:
    """Step 0.755: Copy great-britain TSVs → united-kingdom naming.

    GeoVectors uses 'great-britain' as the slug but the pipeline uses
    'united-kingdom' (matching Geofabrik's canonical slug). This step
    copies the location and tags TSVs from great-britain-* to
    united-kingdom-* so the rest of the pipeline can find them.

    Both location and tags TSVs are copied. The operation is idempotent:
    if the target already exists and is the same size as the source,
    the copy is skipped.
    """
    from extraction.services.gb_uk_copy_service import GbToUkCopyService
    summary = GbToUkCopyService(Path(settings.EMBEDDINGS_ROOT)).run()
    _log(
        logger,
        "info",
        "GB→UK copy complete",
        copied=len(summary["copied"]),
        skipped=len(summary["skipped"]),
        failed=len(summary["failed"]),
        pipeline_run_id=env.pipeline_run_id,
    )
    return env


@pipeline_task(
    bind=True, base=PipelineTask,
    name="step_0i_prebuild_split_embeddings",
    max_retries=1, default_retry_delay=120,
)
@pipeline_step("prebuild_split_embeddings", PlanetEnvelope, 0.76)
def step_0i_prebuild_split_embeddings(self, env: PlanetEnvelope) -> PlanetEnvelope:
    """Step 0.76: Split multi-country TSVs into individual country TSVs.

    Runs the ``preprocess_embeddings`` management command to split:
      - great-britain-location → scotland, england, wales
      - malaysia-singapore-brunei-location → malaysia, singapore, brunei

    Uses osmium-based extraction (pyosmium) to collect OSM IDs within
    each country's boundary polygon, then filters the TSV by those IDs.
    No DB geometry queries needed — works even on a fresh reset.

    Performance: Extracts only the relevant region from continent PBFs using
    osmium extract (bbox of target polygons), copies to RAM disk (/dev/shm)
    for 50-100x I/O speedup, then cleans up after completion.
    """
    from extraction.services.embedding_split_service import EmbeddingSplitService
    continents_root = Path(settings.CONTINENTS_ROOT) if settings.CONTINENTS_ROOT else Path(Path(settings.BASE_DATA_DIR) / 'OSM-PBF-FILES' / 'osm_wikidata_extractions' / 'continents') if settings.BASE_DATA_DIR else Path('/app/data/OSM-PBF-FILES/osm_wikidata_extractions/continents')
    summary = EmbeddingSplitService(
        embeddings_root=Path(settings.EMBEDDINGS_ROOT),
        continents_root=continents_root,
        polygons_root=Path(settings.POLYGON_FILES_DIR),
    ).run()
    _log(
        logger,
        "info",
        "Split complete",
        split=len(summary["split"]),
        skipped=len(summary["skipped"]),
        failed=len(summary["failed"]),
        pipeline_run_id=env.pipeline_run_id,
    )
    return env

@pipeline_task(
    bind=True, base=PipelineTask,
    name="step_0j_prebuild_merge_us_embeddings",
    max_retries=1, default_retry_delay=120,
)
@pipeline_step("prebuild_merge_us_embeddings", PlanetEnvelope, 0.77)
def step_0j_prebuild_merge_us_embeddings(self, env: PlanetEnvelope) -> PlanetEnvelope:
    """Step 0.77: Merge 5 US regional shards into single US location/tags TSVs.

    GeoVectors stores the United States as 5 regional shards:
      us-midwest, us-northeast, us-pacific, us-south, us-west

    Each shard has its own location.tsv.gz. This step concatenates them
    into a single us-location.tsv.gz under EMBEDDINGS_ROOT/north-america/us/,
    making the US eligible for the pipeline as a single country.

    Sequential streaming — reads each shard line-by-line and pipes
    directly into the gzip output. No temp files, no ThreadPool.
    """
    from extraction.services.embedding_merge_service import EmbeddingMergeService
    summary = EmbeddingMergeService(Path(settings.EMBEDDINGS_ROOT)).run()
    _log(
        logger,
        "info",
        "Step 0.77 complete ({:.0f}s)".format(summary["elapsed_s"]),
        status=summary["status"],
        output=summary["output"],
        pipeline_run_id=env.pipeline_run_id,
    )
    return env


@pipeline_task(
    bind=True, base=PipelineTask,
    name="step_0k_rescan_embeddings",
    max_retries=1, default_retry_delay=60,
)
@pipeline_step("prebuild_rescan_embeddings", PlanetEnvelope, 0.78)
def step_0k_rescan_embeddings(self, env: PlanetEnvelope) -> PlanetEnvelope:
    """Step 0.78: Re-scan embeddings after split/merge operations.

    After splits and merges complete, run scan_embeddings again so the
    EligibleCountry rows reflect the new READY statuses instead of
    NEEDS_SPLIT or NEEDS_MERGE.
    """
    call_command("scan_embeddings", clear=True)
    return env
