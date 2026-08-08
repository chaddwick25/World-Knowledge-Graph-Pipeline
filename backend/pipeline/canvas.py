"""
WorldKG Pipeline v3 — Celery Canvas Control Plane
Pipeline steps (0-indexed):
    0. Initialize Planet          (infrastructure, runs once)
    0.5 Initialize Continents     (extract continent PBFs from planet)
    0.6 Pre-build Structure       (CountryPipelineProfile from country_relations.json)
    0.7 Pre-build Country Paths   (resolve TSV, PBF, pickle paths on profiles)
    0.8 Pre-build Subgraphs       (Geofabrik-based SubgraphProfile rows)
    0.95 Pre-build Wikidata IDs   (backfill Q-IDs from relations + hierarchy)
    1. Embed OSM Entities         (preprocessing + GV-Tags + provisional GV-NLE)
    2. Harvest Wikidata           (SPARQL candidates)
    3. Run IGEA                   (Iterative Geographic Entity Alignment)
    4. Predict Spatial Links      (USLP — name+geo+class scoring / gating)
    5. Train GV-NLE               (Authoritative DeepWalk embeddings)

Small-territory handling (Monaco, Belize, etc.):
    Countries WITHOUT subgraphs (has_subgraphs=False) skip the subgraph fan-out
    in Steps 1 and 5. They run at country level only, which is the natural
    behavior for micro-states and island nations.

Pre-build steps (0.6–0.95) run automatically after Steps 0+0.5 during
planet initialization. They populate the DB structure so the frontend can
show available countries and their status without requiring country PBFs.
"""

# TODO: canvas might actually benefit from lazy loading the imports refactor imports below

from __future__ import annotations
import dataclasses
import logging
from uuid import uuid4
from typing import Optional
from datetime import datetime, timezone
from pipeline.tasks.helper import _push_update, _send_pipeline_complete
from pipeline.exceptions import PipelineDispatchError, PipelineAlreadyRunning
from pipeline.celery_app import celery_app
from pipeline.pipeline_logger import setup_pipeline_run_logger
from celery import chord, group


logger = logging.getLogger("pipeline")
def _log(logger, level, msg, **kwargs):
    extra = " ".join(f"{k}={v}" for k, v in kwargs.items() if v is not None)
    getattr(logger, level)(f"{msg} [{extra}]" if extra else msg)


# ══════════════════════════════════════════════════════════════════════════
# Concurrency Guard
# ══════════════════════════════════════════════════════════════════════════
def _check_existing_run(iso: str) -> None:
    """Check for existing runs and detect zombies.

    Cross-checks DB state with Celery's result backend (when available).
    Raises PipelineAlreadyRunning if a genuine run is in progress.
    Auto-recovers zombies (DB says RUNNING but Celery says SUCCESS/FAILURE).

    In eager mode, relies solely on DB state since there's no Celery worker
    to cross-check with.
    """
    from orchestration.models import PipelineRun

    eager = getattr(celery_app.conf, "task_always_eager", False)

    if eager:
        # In eager mode, a RUNNING record means the task is executing
        # synchronously right now in the current process. Block new runs.
        existing = PipelineRun.objects.filter(
            country_code=iso,
            status=PipelineRun.PipelineStatus.RUNNING,
        ).order_by("-created_at").first()
        if existing:
            raise PipelineAlreadyRunning(
                f"Pipeline for {iso} is already running in eager mode "
                f"(run_id={existing.id})"
            )
        return

    # Normal async path
    existing = PipelineRun.objects.filter(
        country_code=iso,
        status__in=[
            PipelineRun.PipelineStatus.PENDING,
            PipelineRun.PipelineStatus.RUNNING,
        ],
    ).order_by("-created_at").first()

    if not existing:
        return

    # Cross-check with Celery's own task state (requires result backend)
    try:
        from celery.result import AsyncResult
        from pipeline.celery_app import celery_app as _celery_app

        async_result = AsyncResult(str(existing.id), app=_celery_app)
        celery_state = async_result.state
    except Exception:
        # Redis might be down -- trust DB, refuse to start
        raise PipelineDispatchError(
            f"Cannot verify pipeline state for {iso}: "
            f"Redis unavailable. Run ID: {existing.id}"
        )

    if celery_state in ("PENDING", "RECEIVED", "STARTED"):
        # Check if this task actually exists in the result backend.
        # After Redis FLUSHALL, AsyncResult returns PENDING for tasks
        # that no longer exist — treat those as zombies, not running.
        try:
            result_obj = async_result.result  # fetches from backend if available
            task_exists = async_result.date_done is not None or result_obj is not None
        except Exception:
            task_exists = False

        if not task_exists:
            # Task not found in backend — zombie. Recover and allow new run.
            logger.warning(
                "Zombie recovery: PipelineRun %s has DB status=%s "
                "but Celery task %s not found in result backend (state=%s)",
                existing.id, existing.status, existing.id, celery_state,
            )
            existing.mark_failed(
                f"Zombie recovered — Celery task not found (state was {celery_state})"
            )
        else:
            # Genuinely running -- block new run
            raise PipelineAlreadyRunning(
                f"Pipeline for {iso} is already running "
                f"(Celery state={celery_state}, run_id={existing.id})"
            )

    if celery_state in ("SUCCESS", "FAILURE"):
        # Zombie detected: Celery finished but DB is stuck
        logger.warning(
            "Zombie recovery: PipelineRun %s has DB status=%s "
            "but Celery reports state=%s",
            existing.id,
            existing.status,
            celery_state,
        )
        if celery_state == "SUCCESS":
            existing.mark_completed({"recovered_from": "zombie"})
        else:
            existing.mark_failed(
                f"Zombie recovered -- Celery state was {celery_state}"
            )
        return  # Clear to start new

    return  # Unknown state -- clear to start


# ══════════════════════════════════════════════════════════════════════════
# Pipeline Entry Points
# ══════════════════════════════════════════════════════════════════════════
def _get_step_tasks():
    """Lazy-import Celery tasks to avoid circular imports at module load."""
    from pipeline.tasks import (
        step_0_initialize_planet,
        step_0b_initialize_continent,
        step_0c_prebuild_structure,
        step_0d_prebuild_country_paths,
        step_0e_prebuild_subgraphs,
        step_0f_prebuild_wikidata_ids,
        step_0l_enrich_worldkg_classes,
        step_0m_generate_osm_boundaries,
        # step_0g_extract_continent_snapshots,  # LEGACY: historical continent snapshots — to be removed
        step_0h_scan_embeddings,
        step_0h_copy_gb_to_uk,
        step_0i_prebuild_split_embeddings,
        step_0j_prebuild_merge_us_embeddings,
        step_0k_rescan_embeddings,
        step_1_embed_osm_entities,
        step_2_harvest_wikidata,
        step_3_run_igea,
        step_4_predict_spatial_links,
        step_4b_finalize_subgraph_uslp,
        step_5_train_gv_nle,
        step_6_mark_search_ready,
    )
    return {
        0: step_0_initialize_planet,
        0.5: step_0b_initialize_continent,
        # 0.6: step_0g_extract_continent_snapshots,  # LEGACY: historical continent snapshots — to be removed
        0.7: step_0c_prebuild_structure,
        0.8: step_0d_prebuild_country_paths,
        0.85: step_0h_scan_embeddings,              # Scan embeddings → EligibleCountry
        0.855: step_0h_copy_gb_to_uk,              # Copy great-britain → united-kingdom naming
        0.86: step_0i_prebuild_split_embeddings,     # Split multi-country TSVs (GB, MY/SG/BN)
        0.87: step_0j_prebuild_merge_us_embeddings,  # Merge US regional shards
        0.88: step_0k_rescan_embeddings,             # Re-scan after split/merge
        0.95: step_0e_prebuild_subgraphs,
        0.96: step_0f_prebuild_wikidata_ids,
        0.97: step_0l_enrich_worldkg_classes,       # Load WorldKG ontology TTL into Redis
        0.98: step_0m_generate_osm_boundaries,      # Generate OSM boundary data
        1: step_1_embed_osm_entities,
        2: step_2_harvest_wikidata,
        3: step_3_run_igea,
        4: step_4_predict_spatial_links,
        4.5: step_4b_finalize_subgraph_uslp,
        5: step_5_train_gv_nle,
        6: step_6_mark_search_ready,
    }


# ══════════════════════════════════════════════════════════════════════════
# Subgraph USLP Chord Helpers
# ══════════════════════════════════════════════════════════════════════════
def _get_subgraph_uslp_tasks(cfg) -> list:
    """Build the header for a parallel subgraph USLP chord.

    Returns a list of Celery task signatures (one per subgraph).
    The chord's callback (``step_4b_finalize_subgraph_uslp``) aggregates
    results in ``canvas.py`` and passes the config dict downstream so
    Steps 5+ can proceed.

    For countries without subgraphs (small territories), returns the
    singleton country-level USLP task wrapped in a list so the chord
    still works.
    """
    from pipeline.tasks import _run_subgraph_uslp

    config_dict = cfg.to_dict()

    if cfg.has_subgraphs and cfg.subgraphs:
        # Fan out: one USLP task per subgraph
        # Use .si() (immutable signature) so the group does NOT inherit
        # positional args from the parent chain. Without .si(), Celery
        # chains the config_dict from Step 3 as an extra positional arg,
        # causing "_run_subgraph_uslp() takes 3 positional args but 4 given".
        subgraph_tasks = [
            _run_subgraph_uslp.si(sg.to_dict(), config_dict)
            for sg in cfg.subgraphs
        ]
    else:
        # Small territory: run USLP at country level only
        _log(logger, "info",
            "No subgraphs — using country-level USLP",
            country=cfg.iso,
            pipeline_run_id=cfg.pipeline_run_id,
        )
        from pipeline.tasks import step_4_predict_spatial_links
        subgraph_tasks = [step_4_predict_spatial_links.si(config_dict)]

    return subgraph_tasks


# ══════════════════════════════════════════════════════════════════════════
# Planet / Continent Init (Foundation Layer)
# ══════════════════════════════════════════════════════════════════════════
def run_planet_initialization(
    planet_pbf_path: Optional[str] = None,
    extract_continents: bool = True,
) -> str:
    """Execute Planet Initialization (Steps 0–0.95) via Celery Canvas.

    This is a **foundation** chain that must complete before any country-level
    pipeline can run. It sets up:
      - File structure (directories)
      - Planetary metrics (osmium fileinfo)
      - Wikidata alignment (country_relations.json → OSMWikiDataHierarchy)
      - Continent extraction (planet → continent PBFs)
      - Pre-build DB structure (CountryPipelineProfile, paths, subgraphs, Q-IDs)

    Steps 0.6–0.95 run automatically after Steps 0+0.5 so that the frontend
    and country pipeline have the DB records they need.

    Args:
        planet_pbf_path: Path to the planet .osm.pbf file.
                         Defaults to settings.PLANET_OSM_FILE_PATH.
        extract_continents: If True, extract continent PBFs from the planet.

    Returns:
        pipeline_run_id (UUID string) — track progress via PipelineRun model.
    """
    from celery import chain

    # Build a PlanetEnvelope for tracking
    from pipeline.envelopes import PlanetEnvelope
    cfg = PlanetEnvelope(
        pipeline_run_id=str(uuid4()),
        pbf_path=planet_pbf_path,
        extract_continents=extract_continents,
    )

    from orchestration.models import PipelineRun
    run = PipelineRun.objects.create(
        country_code="PL",
        country_name="Planet",
        pipeline_type="planet_init",
        status=PipelineRun.PipelineStatus.PENDING,
        configuration={
            "planet_pbf_path": planet_pbf_path,
            "extract_continents": extract_continents,
        },
    )
    cfg = dataclasses.replace(cfg, pipeline_run_id=str(run.id))

    # Set up per-run log file
    setup_pipeline_run_logger(
        pipeline_run_id=cfg.pipeline_run_id,
        country_iso=cfg.iso,
    )

    steps = _get_step_tasks()

    _log(logger, "info",
        "Starting Planet Initialization",
        pipeline_run_id=cfg.pipeline_run_id,
        planet_pbf_path=planet_pbf_path,
        extract_continents=extract_continents,
    )

    # Chain: Step 0 (planet init) → Step 0.5 (continent extraction, optional)
    # → Step 0.6 (Phase 1: extract continent snapshots from historical planets)
    # → Steps 0.7–0.98 (pre-build DB structure, always runs after planet init)
    canvas_tasks = [steps[0].s(cfg.to_dict())]
    if extract_continents:
        canvas_tasks.append(steps[0.5].s())

    # Phase 1: Extract historical continent snapshots (idempotent)
    # LEGACY: step_0g_extract_continent_snapshots disabled — to be removed
    # canvas_tasks.append(steps[0.6].s())   # step_0g_extract_continent_snapshots

    # Pre-build steps after planet and continents are ready.
    # These populate DB records needed by the frontend and country pipeline.
    canvas_tasks.append(steps[0.7].s())   # prebuild_worldkg_structure
    canvas_tasks.append(steps[0.8].s())   # prebuild_country_paths
    canvas_tasks.append(steps[0.85].s())  # prebuild_scan_embeddings — EligibleCountry table
    canvas_tasks.append(steps[0.855].s()) # prebuild_copy_gb_to_uk — great-britain → united-kingdom
    canvas_tasks.append(steps[0.86].s())  # prebuild_split_embeddings — TSV splits (GB, MY/SG/BN)
    canvas_tasks.append(steps[0.87].s())  # prebuild_merge_us_embeddings — US merge
    canvas_tasks.append(steps[0.88].s())  # prebuild_rescan_embeddings — re-scan after split/merge
    canvas_tasks.append(steps[0.95].s())  # prebuild_subgraphs
    canvas_tasks.append(steps[0.96].s())  # prebuild_wikidata_ids
    canvas_tasks.append(steps[0.97].s())  # enrich_worldkg_classes — Load WorldKG ontology TTL into Redis
    canvas_tasks.append(steps[0.98].s())  # generate_osm_boundaries — Generate OSM boundary data

    # Add a final callback that marks the PipelineRun as COMPLETED
    # and sends a pipeline_complete WebSocket message
    from pipeline.tasks import _finalize_planet_init_chain
    canvas = chain(*canvas_tasks) | _finalize_planet_init_chain.s(cfg.pipeline_run_id)

    # Dispatch to Celery FIRST, then mark as RUNNING
    result = canvas.apply_async(task_id=cfg.pipeline_run_id)

    run.status = PipelineRun.PipelineStatus.RUNNING
    run.queued_at = datetime.now(timezone.utc)
    run.started_at = datetime.now(timezone.utc)
    run.save(update_fields=["status", "queued_at", "started_at"])

    _log(logger, "info",
        "Planet initialization dispatched",
        pipeline_run_id=cfg.pipeline_run_id,
        task_id=result.id,
    )

    return cfg.pipeline_run_id


def run_continent_initialization(
    continent_slug: str,
    planet_pbf_path: Optional[str] = None,
) -> str:
    """Extract a single continent PBF from the planet (Step 0.5).

    Args:
        continent_slug: Continent slug (e.g., "europe", "africa").
        planet_pbf_path: Path to planet .osm.pbf.

    Returns:
        pipeline_run_id (UUID string).
    """
    from pipeline.envelopes import PlanetEnvelope
    cfg = PlanetEnvelope(
        pipeline_run_id=str(uuid4()),
        pbf_path=planet_pbf_path,
        extract_continents=True,
    )

    from orchestration.models import PipelineRun
    run = PipelineRun.objects.create(
        country_code=continent_slug.upper(),
        country_name=continent_slug.capitalize(),
        pipeline_type="continent_init",
        status=PipelineRun.PipelineStatus.PENDING,
        configuration={
            "continent_slug": continent_slug,
            "planet_pbf_path": planet_pbf_path,
        },
    )
    cfg = dataclasses.replace(cfg, pipeline_run_id=str(run.id))

    steps = _get_step_tasks()
    task = steps[0.5]

    # Set up per-run log file
    setup_pipeline_run_logger(
        pipeline_run_id=cfg.pipeline_run_id,
        country_iso=cfg.iso,
    )

    # Dispatch to Celery FIRST, then mark as RUNNING
    result = task.apply_async(
        kwargs={"config_dict": cfg.to_dict()},
        task_id=cfg.pipeline_run_id,
    )

    run.status = PipelineRun.PipelineStatus.RUNNING
    run.queued_at = datetime.now(timezone.utc)
    run.started_at = datetime.now(timezone.utc)
    run.save(update_fields=["status", "queued_at", "started_at"])

    _log(logger, "info",
        "Continent initialization dispatched",
        continent=continent_slug,
        pipeline_run_id=cfg.pipeline_run_id,
        task_id=result.id,
    )

    return cfg.pipeline_run_id

# ══════════════════════════════════════════════════════════════════════════
# Country-Level Pipeline
# ══════════════════════════════════════════════════════════════════════════

# Map 1-based step index to canonical name matching AppStateService.
# Used by the eager path to label steps in logs/WS updates.
_STEP_NAMES = {
    1: 'embed_osm_entities',
    2: 'harvest_wikidata',
    3: 'run_igea',
    4: 'predict_spatial_links',
    5: 'train_gv_nle',
    6: 'mark_search_ready',
}


def _mark_pipeline_failed(run, exc, cfg) -> None:
    """Mark a PipelineRun as FAILED + log + send pipeline_complete WS.

    Used by the eager path's outer except block. The on_failure hook handles
    per-step failures; this handles the pipeline-level failure wrapper.
    """
    from orchestration.models import PipelineRun
    run.status = PipelineRun.PipelineStatus.FAILED
    run.error_message = str(exc)
    run.completed_at = datetime.now(timezone.utc)
    run.save(update_fields=["status", "error_message", "completed_at"])

    _log(logger, "error",
        "Pipeline FAILED",
        country=cfg.iso,
        error=str(exc),
        pipeline_run_id=cfg.pipeline_run_id,
    )
    _send_pipeline_complete(
        cfg.pipeline_run_id, status="failed",
        error=str(exc), country=cfg.iso,
    )


def _run_eager(cfg, run, steps) -> None:
    """Execute pipeline steps synchronously (eager mode).

    Calls each task directly via ``task(config_dict)``. Stage tracking
    (PipelineRun.start_stage / complete_stage / mark_failed) is owned by
    the ``@pipeline_step`` decorator + on_success/on_failure hooks — this
    function just calls the tasks and logs the orchestration-level view.
    """
    from orchestration.models import PipelineRun

    _log(logger, "info",
        "Eager mode detected — executing steps synchronously",
        country=cfg.iso,
        pipeline_run_id=cfg.pipeline_run_id,
    )
    # TODO: Reuse this pattern for the DAG implementation
    tasks_list = [steps[1], steps[2], steps[3], steps[4], steps[5], steps[6]]
    config_dict = cfg.to_dict()

    # Mark as RUNNING before starting (eager runs synchronously)
    run.status = PipelineRun.PipelineStatus.RUNNING
    run.queued_at = datetime.now(timezone.utc)
    run.started_at = datetime.now(timezone.utc)
    run.save(update_fields=["status", "queued_at", "started_at"])

    def _run_sync_step(name, task, config):
        _log(logger, "info",
            f"Starting step {name}",
            country=cfg.iso,
            pipeline_run_id=cfg.pipeline_run_id,
        )
        try:
            result = task(config)
            _log(logger, "info",
                f"Step {name} completed",
                country=cfg.iso,
                pipeline_run_id=cfg.pipeline_run_id,
            )
            return result
        except Exception as e:
            _log(logger, "error",
                f"Step {name} FAILED: {e}",
                country=cfg.iso,
                pipeline_run_id=cfg.pipeline_run_id,
            )
            raise

    try:
        for i, task in enumerate(tasks_list, 1):
            step_name = _STEP_NAMES.get(i, f'step_{i}')
            config_dict = _run_sync_step(step_name, task, config_dict)

        run.status = PipelineRun.PipelineStatus.COMPLETED
        run.completed_at = datetime.now(timezone.utc)
        run.save(update_fields=["status", "completed_at"])

        _log(logger, "info",
            "Pipeline completed",
            country=cfg.iso,
            pipeline_run_id=cfg.pipeline_run_id,
        )
        _push_update(
            pipeline_run_id=cfg.pipeline_run_id,
            name="step_6_mark_search_ready",
            status="completed",
            message="Pipeline completed successfully",
            pct=100,
        )
        _send_pipeline_complete(
            cfg.pipeline_run_id, status="completed", country=cfg.iso,
        )
    except Exception as exc:
        _mark_pipeline_failed(run, exc, cfg)


def _run_async(cfg, run, steps) -> None:
    """Execute pipeline steps via Celery Canvas (normal async mode).

    Step 4 uses a chord for parallel subgraph USLP when subgraphs exist.
    For small territories (no subgraphs), the chord still works with a
    single country-level USLP task. The chord callback receives the
    aggregated list of subgraph results; we wrap it with .s() and pass
    the config_dict so the callback can reconstruct CountryEnvelope for
    logging and chain continuation.
    """
    from celery import chain
    from orchestration.models import PipelineRun

    uslp_header = _get_subgraph_uslp_tasks(cfg)
    uslp_callback = steps[4.5].s(cfg.to_dict())  # step_4b_finalize_subgraph_uslp
    uslp_chord = chord(uslp_header, uslp_callback)

    # Build chain — all tasks call _push_update internally,
    # which now sends via Redis ChannelLayer (cross-process).
    canvas = chain(
        steps[1].s(cfg.to_dict()),
        steps[2].s(),
        steps[3].s(),
        uslp_chord,
        steps[5].s(),
        steps[6].s(),
    )

    # Dispatch to Celery FIRST, then mark as RUNNING.
    # This eliminates the PENDING -> RUNNING race window where Celery
    # could pick up the task before the DB is updated.
    result = canvas.apply_async(task_id=cfg.pipeline_run_id)

    run.status = PipelineRun.PipelineStatus.RUNNING
    run.queued_at = datetime.now(timezone.utc)
    run.started_at = datetime.now(timezone.utc)
    run.save(update_fields=["status", "queued_at", "started_at"])

    _log(logger, "info",
        "Pipeline dispatched",
        country=cfg.iso,
        pipeline_run_id=cfg.pipeline_run_id,
        task_id=result.id,
    )


def run_worldkg_pipeline(
    iso: str,
    snapshot_date: Optional[str] = None,
    skip_entropy_gate: bool = False,
    skip_enrich: bool = False,
) -> str:
    """Execute the full WorldKG pipeline for a country (Steps 1–5).

    Small territories like Monaco (has_subgraphs=False) automatically skip
    the subgraph fan-out and run at country level only — this is built into
    the task implementations (see step_1_embed.py and step_5_nle.py).

    Args:
        iso: ISO 3166-1 alpha-2 code (e.g., "MZ", "GB", "CA")
        snapshot_date: Override snapshot date (default from CountryEnvelope)
        skip_entropy_gate: Bypass entropy check (for manual forcing)
        skip_enrich: Skip WorldKG enrichment step (for debugging)

    Returns:
        pipeline_run_id (UUID string) — use this to track progress via
        PipelineRun model or Celery result backend.
    """
    # ── 0. Concurrency guard ──
    # Reject duplicate runs BEFORE creating a new PipelineRun
    _check_existing_run(iso)

    # ── 1. Build centralized config ─────────────────────────────────────
    from pipeline.envelopes import CountryEnvelope
    cfg = CountryEnvelope.from_db(iso, snapshot_date=snapshot_date)
    if skip_entropy_gate:
        # Override frozen hyperparams to disable the entropy gate
        new_hp = dataclasses.replace(cfg.hyperparams, min_entropy=0.0)
        cfg = dataclasses.replace(cfg, hyperparams=new_hp)
    if skip_enrich:
        cfg = cfg.with_state(skip_enrich=True)

    # Monaco handling: small territories without subgraphs run fine at
    # country level. The CountryEnvelope.from_db() will naturally set
    # has_subgraphs=False, and the task implementations handle this.
    _log(logger, "info",
        "Country pipeline config",
        country=cfg.iso,
        name=cfg.name,
        has_subgraphs=cfg.has_subgraphs,
        subgraph_count=len(cfg.subgraphs),
        is_small_territory=not cfg.has_subgraphs,
    )

    # ── 2. Create PipelineRun (DB tracking) ────────────────────────────
    from orchestration.models import PipelineRun
    run = PipelineRun.objects.create(
        country_code=cfg.iso,
        country_name=cfg.name,
        pipeline_type="worldkg_v2",
        status=PipelineRun.PipelineStatus.PENDING,
        configuration=cfg.to_dict(),
    )
    cfg = cfg.with_state(pipeline_run_id=str(run.id))
    # Set up per-run log file
    setup_pipeline_run_logger(
        pipeline_run_id=cfg.pipeline_run_id,
        country_iso=cfg.iso,
    )
    steps = _get_step_tasks()
    _log(logger, "info",
        "Starting WorldKG pipeline",
        country=cfg.iso,
        country_name=cfg.name,
        pipeline_run_id=cfg.pipeline_run_id,
        has_pretrained_nle=cfg.has_pretrained_nle,
        has_subgraphs=cfg.has_subgraphs,
        subgraph_count=len(cfg.subgraphs),
        snapshot_date=cfg.snapshot_date,
    )

    # ── 3. Execute with tracking ────────────────────────────────────────
    # Eager mode: direct task calls (reliable). Async: Celery Canvas chain.
    eager = getattr(celery_app.conf, 'task_always_eager', False)

    if eager:
        _run_eager(cfg, run, steps)
    else:
        _run_async(cfg, run, steps)

    return cfg.pipeline_run_id


def run_pipeline_stage(
    iso: str,
    stage: int,
    snapshot_date: Optional[str] = None,
) -> str:
    """Run a single pipeline stage independently (for debugging / resume).

    Creates a PipelineRun record (pipeline_type="single_stage") so the run
    is tracked in the DB and visible in the frontend, consistent with
    ``run_worldkg_pipeline``.
    """
    # TODO: might need to pass the config dict instead to support Agentic related envelopes(new type)
    from pipeline.envelopes import CountryEnvelope
    from orchestration.models import PipelineRun

    cfg = CountryEnvelope.from_db(iso, snapshot_date=snapshot_date)
    steps = _get_step_tasks()
    task = steps.get(stage)
    if not task:
        raise ValueError(f"Invalid stage: {stage}. Must be 0–5, 0.5.")

    # Create a PipelineRun for tracking (consistent with run_worldkg_pipeline)
    run = PipelineRun.objects.create(
        country_code=cfg.iso,
        country_name=cfg.name,
        pipeline_type="single_stage",
        status=PipelineRun.PipelineStatus.PENDING,
        configuration={"stage": stage, **cfg.to_dict()},
    )
    cfg = cfg.with_state(pipeline_run_id=str(run.id))
    # Set up per-run log file
    setup_pipeline_run_logger(
        pipeline_run_id=cfg.pipeline_run_id,
        country_iso=cfg.iso,
    )
    _log(logger, "info",
        f"Running single stage {stage}",
        country=cfg.iso,
        pipeline_run_id=cfg.pipeline_run_id,
    )
    result = task.apply_async(
        kwargs={"config_dict": cfg.to_dict()},
        task_id=cfg.pipeline_run_id,
    )
    run.status = PipelineRun.PipelineStatus.RUNNING
    run.queued_at = datetime.now(timezone.utc)
    run.started_at = datetime.now(timezone.utc)
    run.save(update_fields=["status", "queued_at", "started_at"])
    return result.id
