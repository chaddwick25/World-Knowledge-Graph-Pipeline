"""
WorldKG Pipeline v2 — Celery App Definition
This file is the Celery application entry point (called via -A pipeline.celery_app).
"""

from __future__ import annotations
import logging
import os
from pathlib import Path

# --- Celery App Definition ---
# NOTE: This file is imported by Celery CLI via \`-A pipeline.celery_app\`.
# The app, PipelineTask base class, and logging helpers must be defined
# INLINE here (not imported from self) to avoid circular imports.

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

"""Logger — consolidated per-run log files + terminal output.

The pipeline logger writes to both:
  1. A per-run file (one file per pipeline_run_id, all steps append)
  2. The terminal / stdout (Celery worker output)

All pipeline modules import ``get_pipeline_logger()`` and use it directly.
"""
logger = logging.getLogger("pipeline")
logger.setLevel(logging.INFO)

# Track the log file per run ID — ensures all Celery tasks in the same
# run append to the same file, even across different worker processes.
_log_file_registry: dict[str, str] = {}

# Console handler (stdout/stderr) — attached once at module level.
if not logger.handlers:
    _console_handler = logging.StreamHandler()
    _console_handler.setLevel(logging.INFO)
    _console_handler.setFormatter(
        logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    )
    logger.addHandler(_console_handler)

# TODO: refactor this 
def _run_log_path(pipeline_run_id: str, country_iso: str) -> Path:
    """Determine a deterministic log file path for a given run ID.

    Uses the ``PipelineRun.queued_at`` timestamp stored in the DB at dispatch
    time, so ALL Celery worker processes (each in its own memory space) resolve
    to the SAME human-readable filename.

    Falls back to ``datetime.now()`` if the DB record is not yet available
    (e.g., during the dispatch call itself).
    """
    from datetime import datetime
    log_dir = Path(__file__).resolve().parent.parent / "logs" / "pipeline"
    log_dir.mkdir(parents=True, exist_ok=True)

    run_short = (pipeline_run_id or "unknown")[:8]
    country_tag = (country_iso or "XX").upper()

    # Use the queued_at timestamp from PipelineRun so all workers agree
    ts = None
    if pipeline_run_id:
        try:
            from orchestration.models import PipelineRun
            run = PipelineRun.objects.filter(id=pipeline_run_id).only("queued_at").first()
            if run and run.queued_at:
                ts = run.queued_at.strftime('%Y-%m-%d_%H-%M-%S')
        except Exception:
            pass

    if not ts:
        ts = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')

    log_file = log_dir / f"pipeline_{country_tag}_{run_short}_{ts}.log"

    # Track in local registry for early return in setup_pipeline_run_logger
    if pipeline_run_id not in _log_file_registry:
        _log_file_registry[pipeline_run_id] = str(log_file)

    return log_file


def setup_pipeline_run_logger(pipeline_run_id: str, country_iso: str = "") -> str:
    """Attach a file handler to the pipeline logger for a specific run.

    Idempotent per ``pipeline_run_id`` — only creates the file handler once.
    Subsequent calls re-use the same log file. This ensures all steps of a
    single pipeline run (which may execute in different Celery worker processes)
    share one consolidated log file.

    Also ensures the console handler is present so all log messages appear
    in the terminal as well.

    Returns the absolute path to the log file.
    """
    log_file = _run_log_path(pipeline_run_id, country_iso)
    log_file_str = str(log_file)

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

    # If we already have a file handler for this exact file, skip
    for h in logger.handlers:
        # Check by class name to avoid importing logging.handlers for cross-process refs
        if hasattr(h, 'baseFilename'):
            if h.baseFilename == log_file_str:
                return log_file_str

    # Attach a new file handler for this run's file.
    # Use WatchedFileHandler (not RotatingFileHandler) because multiple Celery
    # worker processes may write to the same log file concurrently.
    # WatchedFileHandler re-opens the file if it's rotated externally, and
    # avoids the cross-process locking issues of RotatingFileHandler.
    from logging.handlers import WatchedFileHandler
    file_handler = WatchedFileHandler(log_file_str)
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(
        logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    )
    logger.addHandler(file_handler)

    logger.info("Pipeline run log initialized: %s", log_file_str)

    # Also capture Celery's own log messages (task received/succeeded/failed)
    # into the same file. Celery logs through the root logger, not 'pipeline'.
    _root = logging.getLogger()
    _already = False
    for h in _root.handlers:
        if hasattr(h, 'baseFilename') and h.baseFilename == log_file_str:
            _already = True
            break
    if not _already:
        from logging.handlers import WatchedFileHandler
        root_fh = WatchedFileHandler(log_file_str)
        root_fh.setLevel(logging.INFO)
        root_fh.setFormatter(
            logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        )
        _root.addHandler(root_fh)

    return log_file_str


class PipelineTask(_TaskBase):
    """Base class for pipeline Celery tasks.
    
    When Celery is available, inherits from celery.Task to provide
    the required task protocol (bind, apply_async, etc.).
    When Celery is unavailable, acts as a plain mixin for eager/sync mode.
    """
    # Celery Task protocol stubs (used when CELERY_AVAILABLE=False)
    # When Celery IS available, @celery_app.task(base=PipelineTask)
    # dynamically creates a proper Task subclass via Celery's metaclass.
    # The 'bind' issue below only occurs if PipelineTask is NOT a Task subclass.
    # We handle this by conditionally inheriting.
    # Make this an abstract Task when Celery is available
    # This allows @celery_app.task(base=PipelineTask) to work correctly
    # by providing the Task superclass with the 'bind' method.
    
    def setup_pipeline_context(self, config_dict: dict) -> dict:
        if not isinstance(config_dict, dict):
            raise TypeError(
                f"Expected dict for config_dict, got {type(config_dict).__name__}"
            )
        required = ["iso", "name", "continent", "slug"]
        for key in required:
            if key not in config_dict:
                raise ValueError(f"Missing required config key: {key}")
        # Ensure a per-run file handler is attached (idempotent)
        run_id = config_dict.get("pipeline_run_id", "")
        iso = config_dict.get("iso", "")
        if run_id:
            setup_pipeline_run_logger(
                pipeline_run_id=run_id,
                country_iso=iso,
            )
        return config_dict


