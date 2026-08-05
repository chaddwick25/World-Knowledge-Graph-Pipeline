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
"""

from __future__ import annotations

import functools
import logging
from typing import Callable, Type, TypeVar

logger = logging.getLogger("pipeline")

E = TypeVar("E")


def _log_step_start(step_name: str, step_index: float, env) -> None:
    """Log the start of a pipeline step."""
    run_id = getattr(env, "pipeline_run_id", None) or getattr(
        getattr(env, "state", None), "pipeline_run_id", None
    )
    iso = getattr(env, "iso", None) or ""
    logger.info("Step %s: %s [%s run_id=%s]", step_index, step_name, iso, run_id)


def _log_step_complete(step_name: str, step_index: float, env) -> None:
    """Log the completion of a pipeline step."""
    run_id = getattr(env, "pipeline_run_id", None) or getattr(
        getattr(env, "state", None), "pipeline_run_id", None
    )
    iso = getattr(env, "iso", None) or ""
    logger.info("Step %s complete: %s [%s run_id=%s]", step_index, step_name, iso, run_id)


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
            # 1. Context setup (logger, validation)
            config_dict = self.setup_pipeline_context(config_dict)

            # 2. Reconstruct envelope from dict
            env = envelope_cls.from_dict(config_dict)

            # 3. Log start
            _log_step_start(step_name, step_index, env)

            # 4. WS push: in_progress
            from pipeline.tasks.helper import _push_update
            run_id = getattr(env, "pipeline_run_id", None) or getattr(
                getattr(env, "state", None), "pipeline_run_id", None
            )
            _push_update(
                pipeline_run_id=run_id,
                name=step_name,
                status="in_progress",
                message=f"Step {step_index}: {step_name}...",
                pct=10,
            )

            # 5. Run the task body (data plane)
            result_env = func(self, env)

            # 6. WS push: completed
            _push_update(
                pipeline_run_id=run_id,
                name=step_name,
                status="completed",
                message=f"Step {step_index}: {step_name} complete.",
                pct=100,
            )

            # 7. Log complete
            _log_step_complete(step_name, step_index, result_env)

            # 8. Serialize for the next task in the chain
            return result_env.to_dict()

        return wrapper

    return decorator
