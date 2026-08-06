"""WorldKG Pipeline v2 — Celery app entry point (``-A pipeline.celery_app``)."""

from __future__ import annotations

import logging
import os
from pathlib import Path

# --- Celery app (defined inline to avoid circular imports) ---
try:
    from celery import Celery, Task as CeleryTask
    CELERY_AVAILABLE = True
except ImportError:
    Celery = None  # type: ignore
    CeleryTask = None  # type: ignore
    CELERY_AVAILABLE = False

if CELERY_AVAILABLE:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "backend.settings")
    celery_app = Celery("worldkg_pipeline")
    celery_app.config_from_object("django.conf:settings", namespace="CELERY")
    celery_app.autodiscover_tasks()
    from celery import Task as CeleryTask
    _TaskBase = CeleryTask
else:
    celery_app = None  # type: ignore
    _TaskBase = object

# --- Pipeline logger (console + per-run file) ---
# DB-backed structured logs live in pipeline_logger.py (PipelineLogEntry);
# this logger drives the human-readable per-run .log file + console output.
logger = logging.getLogger("pipeline")
logger.setLevel(logging.INFO)

_log_file_registry: dict[str, str] = {}  # run_id -> log path (cross-process cache)

if not logger.handlers:
    _console = logging.StreamHandler()
    _console.setLevel(logging.INFO)
    _console.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    logger.addHandler(_console)


def _remove_file_handlers(log: logging.Logger) -> None:
    """Remove + close all file handlers (baseFilename attr) from a logger."""
    for h in list(log.handlers):
        if hasattr(h, 'baseFilename'):
            log.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass


def _run_log_path(pipeline_run_id: str, country_iso: str) -> Path:
    """Deterministic per-run log path.

    Uses ``PipelineRun.created_at`` (auto_now_add, UTC, always available right
    after ``objects.create()``) so the backend (canvas.py) and all Celery
    workers resolve to the SAME filename. ``queued_at`` is NOT used because it
    is NULL until after ``apply_async()``, which caused a local-time fallback
    that produced a different filename than the worker's UTC-based one.
    """
    from datetime import datetime, timezone
    log_dir = Path(__file__).resolve().parent.parent / "logs" / "pipeline"
    log_dir.mkdir(parents=True, exist_ok=True)

    run_short = (pipeline_run_id or "unknown")[:8]
    country_tag = (country_iso or "XX").upper()

    ts = None
    if pipeline_run_id:
        try:
            from orchestration.models import PipelineRun
            run = PipelineRun.objects.filter(id=pipeline_run_id).only("created_at").first()
            if run and run.created_at:
                ts = run.created_at.strftime('%Y-%m-%d_%H-%M-%S')
        except Exception:
            pass
    if not ts:
        ts = datetime.now(timezone.utc).strftime('%Y-%m-%d_%H-%M-%S')

    log_file = log_dir / f"pipeline_{country_tag}_{run_short}_{ts}.log"
    if pipeline_run_id not in _log_file_registry:
        _log_file_registry[pipeline_run_id] = str(log_file)
    return log_file


def setup_pipeline_run_logger(pipeline_run_id: str, country_iso: str = "") -> str:
    """Attach a per-run WatchedFileHandler to the pipeline + root loggers.

    Idempotent per run: re-attaching for the same run is a no-op. Attaching for
    a new run removes the previous run's file handlers first (prevents cross-run
    contamination). Returns the absolute log file path.

    DB-backed logging is owned by the @pipeline_step decorator via
    PipelineLogger; this is the human-readable companion track.
    """
    from logging.handlers import WatchedFileHandler

    log_file_str = str(_run_log_path(pipeline_run_id, country_iso))

    # Ensure console handler is present (Celery workers may strip it)
    has_console = any(
        isinstance(h, logging.StreamHandler)
        and getattr(h.stream, 'name', '') in ('<stdout>', '<stderr>')
        for h in logger.handlers
    )
    if not has_console:
        ch = logging.StreamHandler()
        ch.setLevel(logging.INFO)
        ch.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        logger.addHandler(ch)

    # Already attached for this file? No-op.
    if any(hasattr(h, 'baseFilename') and h.baseFilename == log_file_str for h in logger.handlers):
        return log_file_str

    # Remove previous run's file handler(s) to prevent cross-run contamination.
    _remove_file_handlers(logger)

    # WatchedFileHandler: cross-process safe (re-opens on external rotation,
    # no RotatingFileHandler locking issues).
    fh = WatchedFileHandler(log_file_str)
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
    logger.addHandler(fh)

    # Stop pipeline -> root propagation: otherwise each pipeline message is
    # written by both the pipeline handler and the root handler below (double).
    logger.propagate = False
    logger.info("Pipeline run log initialized: %s", log_file_str)

    # Also capture Celery/Django messages (root logger) into the same file.
    _root = logging.getLogger()
    _remove_file_handlers(_root)
    root_fh = WatchedFileHandler(log_file_str)
    root_fh.setLevel(logging.INFO)
    root_fh.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
    _root.addHandler(root_fh)

    return log_file_str


class PipelineTask(_TaskBase):
    """Base class for pipeline Celery tasks (celery.Task when Celery is available,
    plain mixin for eager/sync mode otherwise)."""

    def setup_pipeline_context(self, config_dict: dict) -> dict:
        """Validate the config dict and attach the per-run file handler."""
        if not isinstance(config_dict, dict):
            raise TypeError(
                f"Expected dict for config_dict, got {type(config_dict).__name__}"
            )
        for key in ("iso", "name", "continent", "slug"):
            if key not in config_dict:
                raise ValueError(f"Missing required config key: {key}")
        run_id = config_dict.get("pipeline_run_id", "")
        if run_id:
            setup_pipeline_run_logger(run_id, config_dict.get("iso", ""))
        return config_dict
