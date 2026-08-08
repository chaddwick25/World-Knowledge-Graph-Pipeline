"""WorldKG Pipeline v3 — Celery app entry point (``-A pipeline.celery_app``).

This module owns ONLY the Celery app + ``PipelineTask`` (lifecycle hooks) +
``pipeline_task`` decorator + Celery signal connections. All logging
configuration (console handler, per-run file handler, DB-backed
``PipelineLogEntry`` writes) lives in ``pipeline/pipeline_logger.py`` and is
wired by the Celery primitives ``setup_logging`` (console) and
``task_prerun`` (per-run file).

App creation is delegated to ``pipeline/celery_factory.py`` — a parameterized
factory. Celery is a hard dependency (pinned in ``requirements.txt``), so
there is no import guard or availability flag. Task files use the
``@pipeline_task`` decorator (defined below) as a clean wrapper around
``@celery_app.task(...)``.
"""

from __future__ import annotations
from pipeline.celery_factory import create_celery_app

# --- Celery app (parameterized factory call) ---
celery_app, _TaskBase = create_celery_app(
    name="worldkg_pipeline",
    settings_module="backend.settings",
)

# Logging helpers live in pipeline_logger.py (single owner of the logging
# stack). Imported here only so the task_prerun signal below can call it.
from pipeline.pipeline_logger import setup_pipeline_run_logger  # noqa: E402


# --- pipeline_task: thin wrapper around @celery_app.task --------------------
def pipeline_task(*args, **kwargs):
    """Drop-in replacement for ``@celery_app.task(...)``.

    Eliminates the ``if CELERY_AVAILABLE:`` guard boilerplate that was
    copy-pasted in every task file::

        @pipeline_task(bind=True, base=PipelineTask, name="step_2_...")
        @pipeline_step("harvest_wikidata", CountryEnvelope, 2.0)
        def step_2_harvest_wikidata(self, env):
            ...
    """
    def decorator(func):
        return celery_app.task(*args, **kwargs)(func)
    return decorator


class PipelineTask(_TaskBase):
    """Base class for pipeline Celery tasks.

    The control plane is owned by Celery primitives:

      Signals (start-phase):
        ``task_prerun`` → attaches the per-run file logger (setup_pipeline_run_logger)

      Task lifecycle hooks (completion/failure):
        ``on_success`` → step-complete PipelineLogEntry, PipelineRun.complete_stage,
          WS completed
        ``on_failure`` → step-error PipelineLogEntry, PipelineRun.mark_failed,
          WS failed
        ``after_return`` → defensive cleanup of _invocations

    The ``@pipeline_step`` decorator owns only the data-plane boundary:
    config validation, envelope reconstruction/serialization, step-start DB
    log, PipelineRun.start_stage, WS in_progress, and registration of the
    ``_invocations`` entry that the hooks consume.

    The hooks are idempotent via ``_invocations``: the decorator's wrapper
    calls them directly (so they fire in the direct-call eager path that
    bypasses Celery's trace), and Celery's trace calls them again after
    ``run`` returns — the first caller pops the entry and does the work, the
    second is a no-op.
    """

    # Per-invocation state: task_id -> {start, plog, step_name, step_index, run_id, metrics}
    _invocations: dict = {}

    # ── Celery lifecycle hooks (control plane) ──────────────────────────
    def on_success(self, retval, task_id, args, kwargs):
        """Step-complete: PipelineLogEntry, PipelineRun stage, WS push.

        Called by Celery's trace after ``run`` returns AND by the
        ``@pipeline_step`` wrapper directly (direct-call eager path).
        Idempotent — first caller pops ``_invocations[task_id]``.
        """
        inv = self._invocations.pop(task_id, None)
        if not inv:
            return
        import time
        from pipeline.task_decorator import _update_run_stage, _emit_pipeline_assets
        from pipeline.tasks.helper import _push_update
        duration_ms = (time.monotonic() - inv["start"]) * 1000
        inv["plog"].step_complete(
            inv["step_name"], inv["step_index"], duration_ms, task_id,
            inv.get("metrics"),
        )
        _update_run_stage(
            inv["run_id"], inv["step_name"], completed=True,
            metrics=inv.get("metrics"),
        )
        # Phase 1/2: emit PipelineAsset rows for descriptors the task body
        # stashed on the envelope (links task_id via metadata → TaskResult).
        _emit_pipeline_assets(inv, task_id)
        _push_update(
            pipeline_run_id=inv["run_id"], name=inv["step_name"],
            status="completed",
            message=f"Step {inv['step_index']}: {inv['step_name']} complete.",
            pct=100,
        )

    def on_failure(self, exc, task_id, args, kwargs, einfo=None):
        """Step-failure: PipelineLogEntry, PipelineRun stage, WS push.

        Also transitions any ``SnapshotJob`` linked to the run to FAILED
        (TEMPORAL_SNAPSHOT_REFACTOR.md Phase D1 — DB ground truth).

        Called by Celery's trace on exception AND by the ``@pipeline_step``
        wrapper directly (direct-call eager path). Idempotent.
        """
        inv = self._invocations.pop(task_id, None)
        if not inv:
            return
        from pipeline.task_decorator import _update_run_stage
        from pipeline.tasks.helper import _push_update
        inv["plog"].step_error(
            inv["step_name"], inv["step_index"], exc, task_id,
        )
        _update_run_stage(inv["run_id"], inv["step_name"], failed=True)
        _push_update(
            pipeline_run_id=inv["run_id"], name=inv["step_name"],
            status="failed",
            message=f"Step {inv['step_index']}: {inv['step_name']} failed: {exc}",
            pct=100,
        )
        # Mark linked SnapshotJob as FAILED (DB ground truth)
        if inv.get("run_id"):
            try:
                from orchestration.models import PipelineRun, SnapshotJob
                run = PipelineRun.objects.filter(id=inv["run_id"]).first()
                if run is not None:
                    for job in SnapshotJob.objects.filter(pipeline_run=run):
                        job.mark_failed(str(exc))
            except Exception:
                pass

    def after_return(self, status, retval, task_id, args, kwargs, einfo=None):
        """Defensive cleanup — pop any leaked ``_invocations`` entry."""
        self._invocations.pop(task_id, None)


# ── Celery signals: logging configuration via primitives ────────────────────
from celery.signals import setup_logging, task_prerun  # noqa: E402
from pipeline.pipeline_logger import _ensure_console_handler  # noqa: E402


@setup_logging.connect
def _on_celery_setup_logging(**kwargs):
    """Console handler for the pipeline logger.

    Per Celery 4.4 docs: connecting ``setup_logging`` makes Celery skip its
    own logging config, so we own it. We re-attach the console handler
    here because Celery workers may strip handlers during fork.
    ``_ensure_console_handler`` is idempotent.
    """
    _ensure_console_handler()


@task_prerun.connect
def _setup_pipeline_file_logger(task_id, task, args, kwargs, **extra):
    """Attach per-run file logger before task execution.

    Searches positional + keyword args for a config dict containing
    ``pipeline_run_id``. Handles all task signatures:
      - @pipeline_step tasks: args=(config_dict,)
      - _embed_subgraph: args=(subgraph_dict, parent_config)
      - _finalize_planet_init_chain: args=(prev_result_dict, run_id_str)
    Skips tasks with no config dict (file logger already set up by a
    parent task or canvas dispatch; setup_pipeline_run_logger is
    idempotent anyway).
    """
    config = None
    if args:
        for a in args:
            if isinstance(a, dict) and "pipeline_run_id" in a:
                config = a
                break
    if not config:
        config = kwargs.get("config_dict")
    if not isinstance(config, dict) or not config.get("pipeline_run_id"):
        return
    setup_pipeline_run_logger(config["pipeline_run_id"], config.get("iso", ""))
