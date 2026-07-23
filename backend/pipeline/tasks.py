"""
WorldKG Pipeline v2 — Celery Tasks

Task definitions are split across the ``pipeline.tasks`` package.
This file exists for backward compatibility — it re-exports from the package.

See ``pipeline/tasks/`` for individual step implementations.
"""

from __future__ import annotations

from pipeline.tasks import *  # noqa: F401, F403
curl http://localhost:8000/api/worldkg-pipeline/status/679c28d1-171b-4727-b126-46e452668b10/

curl -X POST http://localhost:8000/api/worldkg-pipeline-v2/start/ \
  -H "Content-Type: application/json" \
  -d '{"country_name": "Belize"}'