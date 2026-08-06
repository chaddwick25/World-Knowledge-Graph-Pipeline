"""DB-backed structured logging for pipeline runs.

Replaces the WatchedFileHandler-based file logger. Each log call writes
a PipelineLogEntry row (queryable, shard-ready) and optionally emits to
the Python logging system for console output in dev mode.
"""

from __future__ import annotations

import logging
import traceback as tb_module
from typing import Optional

logger = logging.getLogger("pipeline")


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
            from orchestration.models import PipelineLogEntry
            PipelineLogEntry.objects.create(
                # FK (column run_id) — enables CASCADE delete + related_name
                # queries (run.log_entries). Set from the denormalized id so
                # the FK stays consistent without an extra lookup.
                run_id=self.pipeline_run_id,
                # Denormalized UUID — enables shard routing without a JOIN.
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
