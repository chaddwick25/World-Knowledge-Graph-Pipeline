"""The @pipeline_step decorator — factors the Celery seam.

The seam (context setup, logging, WS push, serialization) is the same
5 lines copy-pasted in every task. This decorator collapses it into
one layer that wraps the task body.

Usage:
    @celery_app.task(bind=True, base=PipelineTask, name="step_0c_...")
    @pipeline_step("prebuild_structure", PlanetEnvelope, 0.6)
    def step_0c_prebuild_structure(self, env: PlanetEnvelope) -> PlanetEnvelope:
        call_command("prebuild_worldkg_structure")
        return env

The decorated function's signature becomes ``def step_X(self, env: E) -> E``
— a pure transform ``env → env'``. The task body is the data plane;
the decorator is the control plane.

Responsibility split (post-refactor — uses Celery primitives):

  Decorator (data-plane boundary + start-phase):
    - config validation (dict type + required keys)
    - envelope reconstruction (dict → env) / serialization (env → dict)
    - step-start: PipelineLogEntry, PipelineRun.start_stage, WS in_progress
    - register ``_invocations[task_id]`` so the hooks can finish the job

  Celery signals (start-phase primitive):
    ``task_prerun`` → attaches per-run file logger (setup_pipeline_run_logger)

  PipelineTask hooks (completion/failure — Celery primitives):
    - ``on_success`` → step-complete log + metrics, PipelineRun.complete_stage,
      WS completed
    - ``on_failure`` → step-error log, PipelineRun.mark_failed, WS failed
    - ``after_return`` → defensive cleanup

The wrapper calls ``on_success`` / ``on_failure`` directly so they fire in
the direct-call eager path (``task(config)``) that bypasses Celery's trace.
The hooks are idempotent via ``_invocations`` — Celery's trace calls them
again after ``run`` returns, but the first caller already popped the entry.
"""

from __future__ import annotations

import functools
import logging
import time
from typing import Callable, Optional, Type, TypeVar

from django.utils import timezone

logger = logging.getLogger("pipeline")

E = TypeVar("E")


def _resolve_run_id(env) -> Optional[str]:
    """Extract pipeline_run_id from an envelope or its nested state."""
    run_id = getattr(env, "pipeline_run_id", None)
    if run_id:
        return run_id
    state = getattr(env, "state", None)
    return getattr(state, "pipeline_run_id", None)


def _extract_metrics(env) -> dict:
    """Extract step-level metrics from the envelope for PipelineRun."""
    metrics = {}
    if hasattr(env, "igea_accepted"):
        metrics["igea_accepted"] = env.igea_accepted
    if hasattr(env, "has_subgraphs"):
        metrics["has_subgraphs"] = env.has_subgraphs
    if hasattr(env, "subgraphs"):
        metrics["subgraph_count"] = len(env.subgraphs) if env.subgraphs else 0
    return metrics


def _extract_pending_assets(env) -> list:
    """Collect artifact descriptors the task body stashed on the envelope.

    Task bodies append dicts to ``env.state.pending_assets`` (transient — not
    serialized by ``to_dict()``). Each descriptor may carry:

        asset_type, asset_name, stage_name, storage_type, storage_path,
        record_count, file_size_bytes, metadata, parent_asset_id

    The ``@pipeline_step`` on_success hook turns these into ``PipelineAsset``
    rows (Phase 1/2 of TEMPORAL_SHARDING_ARTIFACT_PLAN.md). Returns ``[]`` when
    the task body produced no artifacts — emission is opt-in and backward
    compatible.
    """
    state = getattr(env, "state", None)
    pending = getattr(state, "pending_assets", None)
    if pending is None:
        pending = getattr(env, "pending_assets", None)
    if not pending:
        return []
    return [d for d in pending if isinstance(d, dict)]


def _emit_pipeline_assets(inv: dict, task_id) -> None:
    """Emit one ``PipelineAsset`` row per descriptor stashed in ``inv``.

    Called from the ``on_success`` hook (idempotent — the hook pops
    ``_invocations`` first). Never raises: artifact emission is observability,
    not on the critical path of the pipeline. Records the producing
    ``task_id`` in ``metadata`` so ``TaskResult`` (django-celery-results) can
    be joined to ``PipelineAsset``.
    """
    descriptors = inv.get("pending_assets") or []
    run_id = inv.get("run_id")
    if not descriptors or not run_id:
        return
    try:
        from core.models import PipelineAsset, PipelineRun
    except Exception as exc:  # pragma: no cover - import-time safety
        logger.warning("PipelineAsset import failed, skipping emission: %s", exc)
        return

    run = PipelineRun.objects.filter(id=run_id).first()
    if run is None:
        return

    continent = inv.get("continent") or ""
    step_name = inv.get("step_name", "")
    valid_types = {c for c, _ in PipelineAsset.AssetType.choices}
    valid_statuses = {c for c, _ in PipelineAsset.AssetStatus.choices}

    for desc in descriptors:
        try:
            asset_type = desc.get("asset_type")
            if asset_type not in valid_types:
                logger.warning(
                    "Skipping PipelineAsset emit: invalid asset_type=%r (step=%s)",
                    asset_type, step_name,
                )
                continue
            storage_path = desc.get("storage_path")
            if not storage_path:
                logger.warning(
                    "Skipping PipelineAsset emit: missing storage_path (step=%s, type=%s)",
                    step_name, asset_type,
                )
                continue
            meta = dict(desc.get("metadata") or {})
            if task_id:
                meta["task_id"] = task_id
            asset = PipelineAsset(
                pipeline_run=run,
                asset_type=asset_type,
                asset_name=desc.get("asset_name") or f"{step_name}_{asset_type}",
                stage_name=desc.get("stage_name") or step_name,
                storage_type=desc.get("storage_type") or "filesystem",
                storage_path=storage_path,
                status=desc.get("status") or PipelineAsset.AssetStatus.COMPLETED,
                file_size_bytes=desc.get("file_size_bytes"),
                record_count=desc.get("record_count"),
                continent=continent or None,
                completed_at=timezone.now() if (desc.get("status") or PipelineAsset.AssetStatus.COMPLETED) in (
                    PipelineAsset.AssetStatus.COMPLETED,
                ) else None,
                metadata=meta,
            )
            parent_id = desc.get("parent_asset_id")
            if parent_id:
                asset.parent_asset_id = parent_id
            asset.save()
        except Exception as exc:  # pragma: no cover - per-descriptor safety
            logger.warning("PipelineAsset emit failed for one descriptor: %s", exc)


def _update_run_stage(
    run_id: Optional[str],
    step_name: str,
    started: bool = False,
    completed: bool = False,
    failed: bool = False,
    metrics: Optional[dict] = None,
) -> None:
    """Update PipelineRun stage tracking. Never raises."""
    if not run_id:
        return
    try:
        from core.models import PipelineRun
        run = PipelineRun.objects.filter(id=run_id).first()
        if not run:
            return
        if started:
            run.start_stage(step_name)
        elif completed:
            run.complete_stage(step_name, metrics)
        elif failed:
            run.mark_failed(f"Step {step_name} failed")
    except Exception as exc:
        logger.warning("PipelineRun stage update failed: %s", exc)


def pipeline_step(
    step_name: str,
    envelope_cls: Type[E],
    step_index: float,
) -> Callable[[Callable[..., E]], Callable[..., dict]]:
    """Decorator that factors the Celery seam (context/log/WS/serialize).

    Args:
        step_name: Canonical step name (must match app_state_service STEP_NAMES).
        envelope_cls: The envelope class (PlanetEnvelope or CountryEnvelope).
        step_index: Numeric step index for logging (e.g., 0.6, 1.0, 4.0).

    Returns:
        A decorator that wraps a ``def task(self, env) -> env`` function
        into a ``def task(self, config_dict) -> dict`` Celery task.
    """

    def decorator(func: Callable[..., E]) -> Callable[..., dict]:
        @functools.wraps(func)
        def wrapper(self, config_dict: dict) -> dict:
            # 1. Validate config dict (data-plane concern — lives here, not on
            #    the Celery Task base class). File logger setup is handled by
            #    the task_prerun signal (Celery primitive) in celery_app.py.
            if not isinstance(config_dict, dict):
                raise TypeError(
                    f"Expected dict for config_dict, got {type(config_dict).__name__}"
                )
            for key in ("iso", "name", "continent", "slug"):
                if key not in config_dict:
                    raise ValueError(f"Missing required config key: {key}")

            # 2. Reconstruct envelope from dict
            env = envelope_cls.from_dict(config_dict)

            # 3. Extract context
            run_id = _resolve_run_id(env)
            iso = getattr(env, "iso", None) or ""
            continent = getattr(env, "continent", None) or ""
            task_id = getattr(self.request, "id", None)

            # 4. Initialize DB-backed logger
            from pipeline.pipeline_logger import PipelineLogger
            plog = PipelineLogger(
                pipeline_run_id=run_id,
                country_code=iso,
                continent=continent,
            )

            # 5. Start-phase: step-start log, PipelineRun start, WS in_progress
            plog.step_start(step_name, step_index, task_id)
            _update_run_stage(run_id, step_name, started=True)
            from pipeline.tasks.helper import _push_update
            _push_update(
                pipeline_run_id=run_id,
                name=step_name,
                status="in_progress",
                message=f"Step {step_index}: {step_name}...",
                pct=10,
            )

            # 6. Register invocation so on_success/on_failure hooks can finish
            self._invocations[task_id] = {
                "start": time.monotonic(),
                "plog": plog,
                "step_name": step_name,
                "step_index": step_index,
                "run_id": run_id,
                "continent": continent,
                "iso": iso,
            }

            # 7. Data plane — run the task body (pure transform env → env')
            try:
                result_env = func(self, env)
            except Exception as exc:
                # on_failure hook: PipelineLogEntry, PipelineRun, WS push
                # (idempotent — Celery's trace will call it again, no-op)
                self.on_failure(exc, task_id, (config_dict,), {})
                raise

            # 8. Stash metrics + artifact descriptors for on_success, serialize
            self._invocations[task_id]["metrics"] = _extract_metrics(result_env)
            self._invocations[task_id]["pending_assets"] = _extract_pending_assets(result_env)
            retval = result_env.to_dict()

            # 9. on_success hook: step-complete log + metrics, PipelineRun, WS push
            # (idempotent — Celery's trace will call it again, no-op)
            self.on_success(retval, task_id, (config_dict,), {})
            return retval

        return wrapper

    return decorator
