"""Pipeline logging — single owner of the full logging stack.

Two tracks:

  1. DB-backed structured logs — ``PipelineLogger`` writes queryable
     ``PipelineLogEntry`` rows (shard-ready) via ``PipelineLogger._write``.
  2. Human-readable per-run ``.log`` files + console — the module-level
     ``logger`` (``logging.getLogger("pipeline")``) drives a console
     ``StreamHandler`` (attached once at import) and a per-run
     ``WatchedFileHandler`` attached by ``setup_pipeline_run_logger``.

Console handler attachment is wired by the Celery ``setup_logging`` signal
(see ``celery_app.py``); per-run file handler attachment is wired by the
Celery ``task_prerun`` signal. Neither signal lives here — this module
only provides the helpers they call.
"""

from __future__ import annotations

import logging
import traceback as tb_module
from pathlib import Path
from typing import Optional

logger = logging.getLogger("pipeline")
logger.setLevel(logging.INFO)

# Cross-process cache: run_id -> log path (so all workers agree on the filename).
_log_file_registry: dict[str, str] = {}


def _ensure_console_handler() -> None:
    """Attach the console StreamHandler to the pipeline logger if absent.

    Idempotent. Called at import time below AND by the Celery
    ``setup_logging`` signal (workers may strip handlers during fork).
    """
    has_console = any(
        isinstance(h, logging.StreamHandler)
        and getattr(h.stream, "name", "") in ("<stdout>", "<stderr>")
        for h in logger.handlers
    )
    if not has_console:
        ch = logging.StreamHandler()
        ch.setLevel(logging.INFO)
        ch.setFormatter(
            logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
        )
        logger.addHandler(ch)


# Attach the console handler once at import (covers non-Celery / test paths).
_ensure_console_handler()


def _remove_file_handlers(log: logging.Logger) -> None:
    """Remove + close all file handlers (baseFilename attr) from a logger."""
    for h in list(log.handlers):
        if hasattr(h, "baseFilename"):
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
                ts = run.created_at.strftime("%Y-%m-%d_%H-%M-%S")
        except Exception:
            pass
    if not ts:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")

    log_file = log_dir / f"pipeline_{country_tag}_{run_short}_{ts}.log"
    if pipeline_run_id not in _log_file_registry:
        _log_file_registry[pipeline_run_id] = str(log_file)
    return log_file


def setup_pipeline_run_logger(pipeline_run_id: str, country_iso: str = "") -> str:
    """Attach a per-run WatchedFileHandler to the pipeline + root loggers.

    Idempotent per run: re-attaching for the same run is a no-op. Attaching for
    a new run removes the previous run's file handlers first (prevents
    cross-run contamination). Returns the absolute log file path.

    DB-backed logging is owned by the ``@pipeline_step`` decorator via
    ``PipelineLogger``; this is the human-readable companion track.
    """
    from logging.handlers import WatchedFileHandler

    log_file_str = str(_run_log_path(pipeline_run_id, country_iso))

    # Ensure console handler is present (Celery workers may strip it on fork).
    _ensure_console_handler()

    # Already attached for this file? No-op.
    if any(hasattr(h, "baseFilename") and h.baseFilename == log_file_str for h in logger.handlers):
        return log_file_str

    # Remove previous run's file handler(s) to prevent cross-run contamination.
    _remove_file_handlers(logger)

    # WatchedFileHandler: cross-process safe (re-opens on external rotation,
    # no RotatingFileHandler locking issues).
    fh = WatchedFileHandler(log_file_str)
    fh.setLevel(logging.INFO)
    fh.setFormatter(
        logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    )
    logger.addHandler(fh)

    # Stop pipeline -> root propagation: otherwise each pipeline message is
    # written by both the pipeline handler and the root handler below (double).
    logger.propagate = False
    logger.info("Pipeline run log initialized: %s", log_file_str)

    # Also capture Celery/Django messages (root logger) into the same file.
    # Set root to INFO so that module-level loggers (vector_storage_service,
    # embedding_service, etc.) that propagate to root are captured — not
    # just the "pipeline" logger.  Without this, root stays at WARNING
    # (Python default) and all INFO-level upsert/enrichment logs are
    # silently dropped after a fresh container start.
    _root = logging.getLogger()
    _root.setLevel(logging.INFO)
    _remove_file_handlers(_root)
    root_fh = WatchedFileHandler(log_file_str)
    root_fh.setLevel(logging.INFO)
    root_fh.setFormatter(
        logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    )
    _root.addHandler(root_fh)

    return log_file_str


class PipelineLogger:
    """Structured logger that writes to PipelineLogEntry + Python logging.

    Usage in @pipeline_step decorator:
        plog = PipelineLogger(pipeline_run_id, country_code, continent)
        plog.step_start(step_name, step_index, task_id)
        ...
        plog.step_complete(step_name, step_index, duration_ms, metrics)
    """

    def __init__(
        self,
        pipeline_run_id: Optional[str] = None,
        country_code: str = "",
        continent: str = "",
    ):
        self.pipeline_run_id = pipeline_run_id
        self.country_code = country_code
        self.continent = continent

    def _write(
        self,
        level: str,
        message: str,
        step_name: Optional[str] = None,
        step_index: Optional[float] = None,
        task_id: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> None:
        """Write a PipelineLogEntry row + emit to Python logging."""
        # 1. DB write (structured, queryable, shard-ready)
        try:
            from orchestration.models import PipelineLogEntry, PipelineRun
            # Only set the run FK if the PipelineRun actually exists.
            # When running steps directly (e.g., via manage.py shell without
            # a PipelineRun record), the FK would violate the constraint.
            run_fk = None
            if self.pipeline_run_id:
                run_fk = PipelineRun.objects.filter(
                    id=self.pipeline_run_id
                ).values_list("id", flat=True).first()
            PipelineLogEntry.objects.create(
                # FK (column run_id) — enables CASCADE delete + related_name
                # queries (run.log_entries). Only set when the run exists.
                run_id=run_fk,
                # Denormalized UUID — enables shard routing without a JOIN.
                # Kept even when the run doesn't exist, for traceability.
                pipeline_run_id=self.pipeline_run_id,
                country_code=self.country_code or None,
                continent=self.continent or None,
                step_name=step_name,
                step_index=step_index,
                level=level,
                message=message,
                metadata=metadata or {},
                task_id=task_id,
            )
        except Exception as exc:
            # Never let logging crash the pipeline
            logger.warning("PipelineLogEntry write failed: %s", exc)

        # 2. Python logging (console + optional file handler in dev)
        log_fn = {
            "DEBUG": logger.debug,
            "INFO": logger.info,
            "WARNING": logger.warning,
            "ERROR": logger.error,
            "CRITICAL": logger.critical,
        }.get(level, logger.info)
        log_fn(message)

    def step_start(
        self,
        step_name: str,
        step_index: float,
        task_id: Optional[str] = None,
    ) -> None:
        self._write(
            level="INFO",
            message=f"Step {step_index}: {step_name} started",
            step_name=step_name,
            step_index=step_index,
            task_id=task_id,
        )

    def step_complete(
        self,
        step_name: str,
        step_index: float,
        duration_ms: float,
        task_id: Optional[str] = None,
        metrics: Optional[dict] = None,
    ) -> None:
        self._write(
            level="INFO",
            message=f"Step {step_index}: {step_name} complete ({duration_ms:.0f}ms)",
            step_name=step_name,
            step_index=step_index,
            task_id=task_id,
            metadata={
                "duration_ms": duration_ms,
                **(metrics or {}),
            },
        )

    def step_error(
        self,
        step_name: str,
        step_index: float,
        exc: BaseException,
        task_id: Optional[str] = None,
    ) -> None:
        self._write(
            level="ERROR",
            message=f"Step {step_index}: {step_name} failed: {exc}",
            step_name=step_name,
            step_index=step_index,
            task_id=task_id,
            metadata={
                "error_type": type(exc).__name__,
                "traceback": tb_module.format_exc(),
            },
        )

    def warning(
        self,
        message: str,
        step_name: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> None:
        self._write(level="WARNING", message=message, step_name=step_name, metadata=metadata)

    def info(
        self,
        message: str,
        step_name: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> None:
        self._write(level="INFO", message=message, step_name=step_name, metadata=metadata)
