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
    from core.models import PipelineRun

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
    """Lazy-import Celery tasks to avoid circular imports at module load.

    Planet-init steps (formerly 0–0.98) are no longer Celery tasks — they
    are now sub-steps of the ``init_planet`` management command (see
    ``core/management/commands/init_planet.py``). Only Steps 1–6 (country
    pipeline) remain in the Celery canvas.
    """
    from pipeline.tasks import (
        step_1_embed_osm_entities,
        step_1b_finalize_subgraph_embeds,
        step_2_harvest_wikidata,
        step_3_run_igea,
        step_4_predict_spatial_links,
        step_4b_finalize_subgraph_uslp,
        step_5_train_gv_nle,
        step_5b_finalize_subgraph_nle,
        step_5c_graph_spectral_analysis,
        step_5d_temporal_drift,
        step_6_mark_search_ready,
    )
    return {
        1: step_1_embed_osm_entities,
        1.5: step_1b_finalize_subgraph_embeds,      # Chord callback for Step 1 subgraphs
        2: step_2_harvest_wikidata,
        3: step_3_run_igea,
        4: step_4_predict_spatial_links,
        4.5: step_4b_finalize_subgraph_uslp,
        5: step_5_train_gv_nle,
        5.5: step_5b_finalize_subgraph_nle,         # Chord callback for Step 5 subgraphs
        5.7: step_5c_graph_spectral_analysis,       # Graph & spectral analysis (non-fatal)
        5.8: step_5d_temporal_drift,                # Temporal drift (conditional: >=2 snapshots)
        6: step_6_mark_search_ready,
    }


# ══════════════════════════════════════════════════════════════════════════
# Subgraph Chord Helpers (Steps 1, 4, 5)
# ══════════════════════════════════════════════════════════════════════════
def _lightweight_config_dict(cfg) -> dict:
    """Return a config dict with the subgraphs list stripped out.

    Each subgraph task receives its own ``sg.to_dict()`` as the first
    argument, so it does not need the full list of all subgraphs in the
    parent config.  Stripping the subgraphs list dramatically reduces
    the serialized message size when 90 subgraph tasks are embedded in
    a Celery canvas chain (1.5 MB → ~100 KB), preventing Redis
    connection resets on large chord fan-outs.
    """
    config_dict = cfg.to_dict()
    config_dict.pop("subgraphs", None)
    return config_dict


def _get_subgraph_embed_tasks(cfg) -> list:
    """Build the header for the Step 1 subgraph embedding chord.

    Returns a list of Celery task signatures (one per subgraph).
    The chord's callback (``step_1b_finalize_subgraph_embeds``) aggregates
    results and passes the config dict downstream so Step 2 can proceed.

    For countries without subgraphs, returns an empty list — canvas.py
    skips the chord and chains step_1 directly to step_2.
    """
    from pipeline.tasks import _embed_subgraph

    if not (cfg.has_subgraphs and cfg.subgraphs):
        return []

    config_dict = _lightweight_config_dict(cfg)
    return [
        _embed_subgraph.si(sg.to_dict(), config_dict)
        for sg in cfg.subgraphs
    ]


def _get_subgraph_nle_tasks(cfg) -> list:
    """Build the header for the Step 5 subgraph NLE training chord.

    Returns a list of Celery task signatures (one per subgraph).
    The chord's callback (``step_5b_finalize_subgraph_nle``) aggregates
    results and passes the config dict downstream so Step 6 can proceed.

    For countries without subgraphs, returns an empty list — canvas.py
    skips the chord and chains step_5 directly to step_6.
    """
    from pipeline.tasks import _train_subgraph_gv_nle

    if not (cfg.has_subgraphs and cfg.subgraphs):
        return []

    config_dict = _lightweight_config_dict(cfg)
    return [
        _train_subgraph_gv_nle.si(sg.to_dict(), config_dict)
        for sg in cfg.subgraphs
    ]


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

    config_dict = _lightweight_config_dict(cfg)

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
# Planet init (formerly steps 0a–0m) is now a management command
# (``core/management/commands/init_planet.py``) invoked as a Docker
# entrypoint step on the backend container. The wrappers below are kept
# as thin facades so any in-process caller (e.g. legacy API endpoints,
# tests) still has a function to call. They run ``init_planet``
# synchronously — there is no Celery canvas for planet init any more.
def run_planet_initialization(
    planet_pbf_path: Optional[str] = None,
    extract_continents: bool = True,
) -> str:
    """Run planet initialization synchronously via the ``init_planet`` command.

    Returns a synthetic pipeline_run_id (a PlanetSnapshot row is created by
    ``init_planet`` itself). The ``extract_continents`` flag maps to the
    command's ``--skip-continents`` inverse.
    """
    from django.core.management import call_command

    kwargs = {"planet_pbf": planet_pbf_path} if planet_pbf_path else {}
    if not extract_continents:
        kwargs["skip_continents"] = True
    call_command("init_planet", **kwargs)
    # Return a stable identifier — callers (e.g. PipelineRun rows) treat
    # this as opaque. Use today's PlanetSnapshot date string if available.
    from core.models import PlanetSnapshot
    today = datetime.now(timezone.utc).strftime("%Y_%m_%d")
    snap = PlanetSnapshot.objects.filter(snapshot_date_str=today).first()
    return str(snap.id) if snap else today


def run_continent_initialization(
    continent_slug: str,
    planet_pbf_path: Optional[str] = None,
) -> str:
    """Extract a single continent PBF from the planet.

    Planet init no longer has per-continent Celery tasks — continent
    extraction is one step (``extract_continents``) inside ``init_planet``.
    This facade runs ``init_planet --step extract_continents`` so callers
    that just want a continent re-extract still have an entry point.
    """
    from django.core.management import call_command

    kwargs = {"step": "extract_continents"}
    if planet_pbf_path:
        kwargs["planet_pbf"] = planet_pbf_path
    call_command("init_planet", **kwargs)
    return continent_slug

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
    from core.models import PipelineRun
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
    from core.models import PipelineRun

    _log(logger, "info",
        "Eager mode detected — executing steps synchronously",
        country=cfg.iso,
        pipeline_run_id=cfg.pipeline_run_id,
    )
    tasks_list = [
        steps[1], steps[2], steps[3], steps[4], steps[5],
        steps[5.7], steps[5.8], steps[6],
    ]
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

    def _run_sync_subgraphs(step_name, subgraph_tasks, callback_task, config):
        """Run subgraph tasks synchronously in eager mode, then call the callback."""
        if not subgraph_tasks:
            return config
        results = []
        for sg_task in subgraph_tasks:
            # .si() signatures are immutable — call with no extra args
            sg_result = sg_task.apply()
            results.append(sg_result.result)
        # Call the callback with (aggregated_results, config_dict)
        cb_result = callback_task(results, config)
        return cb_result if cb_result else config

    try:
        # Step 1: embed + entropy — Step 1 generates subgraph PBFs during
        # preprocess_snapshot, but the Step 1b chord was already built with
        # the stale has_subgraphs flag.  Subgraph embeddings are generated
        # by Step 1 itself (via EmbeddingService.run) for countries with
        # subgraphs, so Step 1b is redundant in the eager path.
        step_name = _STEP_NAMES.get(1, 'step_1')
        config_dict = _run_sync_step(step_name, tasks_list[0], config_dict)
        # Step 2: harvest
        step_name = _STEP_NAMES.get(2, 'step_2')
        config_dict = _run_sync_step(step_name, tasks_list[1], config_dict)
        # Step 3: IGEA
        step_name = _STEP_NAMES.get(3, 'step_3')
        config_dict = _run_sync_step(step_name, tasks_list[2], config_dict)
        # Step 4: USLP — Step 4 now self-dispatches per-subgraph USLP
        # internally (via rehydration) when subgraphs are available, so
        # Step 4b is not needed in the eager path.  This prevents double
        # USLP execution (country-level + per-subgraph) that the old
        # eager path had when has_subgraphs=True.
        step_name = _STEP_NAMES.get(4, 'step_4')
        config_dict = _run_sync_step(step_name, tasks_list[3], config_dict)
        # Step 5: train GV-NLE — Step 5 now self-dispatches per-subgraph
        # NLE training internally (via rehydration) when subgraphs are
        # available, so Step 5b is not needed in the eager path.  This
        # prevents double NLE training that the old eager path had when
        # has_subgraphs=True.
        step_name = _STEP_NAMES.get(5, 'step_5')
        config_dict = _run_sync_step(step_name, tasks_list[4], config_dict)
        # Step 5c: graph & spectral analysis (non-fatal — failures logged,
        # pipeline continues).  Step 5c is new (GRAPH_SPECTRAL_TEMPORAL_PLAN.md).
        config_dict = _run_sync_step('graph_spectral_analysis', tasks_list[5], config_dict)
        # Step 5d: temporal drift (conditional — only runs when >=2 snapshots
        # exist for this country; the task body no-ops otherwise).
        config_dict = _run_sync_step('temporal_drift', tasks_list[6], config_dict)
        # Step 6: mark search ready
        step_name = _STEP_NAMES.get(6, 'step_6')
        config_dict = _run_sync_step(step_name, tasks_list[7], config_dict)

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

    Steps 1 and 4 use chords for parallel subgraph processing when
    subgraphs exist.  The chord callbacks aggregate results and pass the
    config dict downstream so the chain can continue.

    Step 5 does NOT use a chord — it self-dispatches per-subgraph NLE
    training inline (via rehydration from DB) because GPU training is
    serialized by the GpuSlotLock anyway.  A redundant Step 5b chord here
    would cause every subgraph to be trained twice (once by step_5's
    self-dispatch, once by the chord header).

    For small territories (no subgraphs), the subgraph chords are skipped
    and the chain runs step_1 → step_2 → step_3 → step_4 → step_5 → step_6
    directly.
    """
    from celery import chain
    from core.models import PipelineRun

    # Use lightweight config (subgraphs stripped) for chord callbacks and
    # subgraph tasks to keep the serialized Redis message small.  The full
    # config_dict with subgraphs is only needed by step_1 itself (which
    # rehydrates subgraphs from DB anyway).
    config_dict = _lightweight_config_dict(cfg)

    # ── Step 1 chord: subgraph embeddings ──
    # Step 1 does NOT self-dispatch subgraph embeddings — the chord is the
    # only mechanism that generates per-subgraph pickles.
    embed_header = _get_subgraph_embed_tasks(cfg)
    if embed_header:
        embed_callback = steps[1.5].s(config_dict)  # step_1b_finalize_subgraph_embeds
        step1_chord = chord(embed_header, embed_callback)
    else:
        step1_chord = None  # no subgraphs — step_1 returns env directly

    # ── Step 4 chord: subgraph USLP ──
    # When subgraphs are present at canvas build time, use a chord to fan
    # out parallel _run_subgraph_uslp tasks per subgraph, with step_4b as
    # the callback that aggregates results.
    #
    # When NO subgraphs are present (small territory or fresh DB), skip the
    # chord entirely and chain step_4 directly.  step_4 rehydrates subgraphs
    # from DB and self-dispatches per-subgraph USLP inline.  step_4b is a
    # no-op in this case (no subgraph results to aggregate).
    #
    # Using a chord with a single header task (the old approach) is
    # vulnerable to a stale-TaskResult race on re-runs: the patched backend's
    # fallback_chord_unlock polling can see the previous run's SUCCESS
    # TaskResult and fire the callback before step_4 finishes, causing
    # Step 4 and Step 5 to run concurrently on the GPU → CUDA OOM.
    if cfg.has_subgraphs and cfg.subgraphs:
        uslp_header = _get_subgraph_uslp_tasks(cfg)
        uslp_callback = steps[4.5].s(config_dict)  # step_4b_finalize_subgraph_uslp
        uslp_chord = chord(uslp_header, uslp_callback)
    else:
        uslp_chord = None  # no subgraphs — step_4 runs inline, step_4b skipped

    # ── Step 5: NO chord ──
    # step_5 self-dispatches per-subgraph NLE training inline (via
    # rehydration from DB).  A Step 5b chord here would re-train every
    # subgraph a second time.  GPU training is serialized by GpuSlotLock
    # so the chord provides no parallelism benefit.  See the comment in
    # step_5_nle.py's rehydrated branch.

    # Build chain — all tasks call _push_update internally,
    # which now sends via Redis ChannelLayer (cross-process).
    # When a chord is present, it replaces the bare step in the chain so
    # the chain waits for all subgraph tasks to complete before proceeding.
    canvas_parts = [
        steps[1].s(config_dict),
    ]
    if step1_chord is not None:
        canvas_parts.append(step1_chord)
    canvas_parts.extend([
        steps[2].s(),
        steps[3].s(),
    ])
    if uslp_chord is not None:
        canvas_parts.append(uslp_chord)
    else:
        # No subgraphs at canvas build time — step_4 runs inline
        # (rehydrates subgraphs from DB if generated during Step 1).
        # step_4b is skipped (no-op without subgraph results).
        canvas_parts.append(steps[4].s())
    canvas_parts.append(steps[5].s())
    # Step 5c (graph & spectral analysis) + Step 5d (temporal drift) run
    # after Step 5 and before Step 6.  Both are non-fatal / conditional —
    # their task bodies log warnings and no-op when prerequisites are missing
    # (e.g. <2 snapshots for 5d).  See GRAPH_SPECTRAL_TEMPORAL_PLAN.md.
    canvas_parts.extend([
        steps[5.7].s(),   # step_5c_graph_spectral_analysis
        steps[5.8].s(),   # step_5d_temporal_drift
    ])
    canvas_parts.append(steps[6].s())

    canvas = chain(*canvas_parts)

    # Clean up stale TaskResult rows from any previous run with the same
    # pipeline_run_id (re-runs).  The patched Celery backend uses
    # fallback_chord_unlock polling, which checks TaskResult rows to detect
    # chord header completion.  Stale SUCCESS rows from a previous run can
    # cause the chord callback to fire prematurely, leading to concurrent
    # GPU usage and CUDA OOM.
    try:
        from django_celery_results.models import TaskResult
        deleted_count, _ = TaskResult.objects.filter(
            task_id=cfg.pipeline_run_id
        ).delete()
        if deleted_count:
            _log(logger, "info",
                "Cleaned up stale TaskResult rows before dispatch",
                country=cfg.iso,
                deleted=deleted_count,
                pipeline_run_id=cfg.pipeline_run_id,
            )
    except Exception:
        pass  # non-fatal — dispatch proceeds regardless

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
    from core.models import PipelineRun
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
    stage: float,
    snapshot_date: Optional[str] = None,
) -> str:
    """Run a single pipeline stage independently (for debugging / resume).

    Creates a PipelineRun record (pipeline_type="single_stage") so the run
    is tracked in the DB and visible in the frontend, consistent with
    ``run_worldkg_pipeline``.
    """
    from pipeline.envelopes import CountryEnvelope
    from core.models import PipelineRun

    cfg = CountryEnvelope.from_db(iso, snapshot_date=snapshot_date)
    steps = _get_step_tasks()
    task = steps.get(stage)
    if not task:
        raise ValueError(
            f"Invalid stage: {stage}. Must be one of "
            f"{sorted(steps)}."
        )

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
