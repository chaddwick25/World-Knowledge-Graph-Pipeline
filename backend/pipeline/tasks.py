"""
WorldKG Pipeline v2 — Celery Tasks (backward compat re-export)

Task definitions are split across the ``pipeline.tasks`` package.
This file exists for backward compatibility — it re-exports from the package.

See ``pipeline/tasks/`` for individual step implementations.
"""

from __future__ import annotations

from pipeline.tasks import *  # noqa: F401, F403
