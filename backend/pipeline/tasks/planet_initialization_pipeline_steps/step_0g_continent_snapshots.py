"""
Celery task: Step 0.6 (Phase 1) — Extract Continent Snapshots from Flat Continent PBFs.

This task is Phase 1 of the temporal sharding system. It:
1. Takes the flat continent PBFs produced by Step 0.5
2. Uses ``osmium time-filter`` to create yearly point-in-time snapshots
3. Saves to ``continents/{YYYY_12_31}/{continent}.pbf``

It runs as Step 0.6 in the planet initialization canvas, AFTER
Step 0.5 (continent extraction) and BEFORE Step 0.7 (prebuild_structure).
"""

from __future__ import annotations

import logging
from pipeline.tasks.helper import _log, _push_update
from pipeline.envelopes import PlanetEnvelope
from pipeline.task_decorator import pipeline_step
from pipeline.celery_app import (
    pipeline_task,
    PipelineTask,
)

logger = logging.getLogger("pipeline")

@pipeline_task(
    bind=True,
    base=PipelineTask,
    name="step_0g_extract_continent_snapshots",
    max_retries=1,
    default_retry_delay=300,
)
@pipeline_step("continent_snapshots", PlanetEnvelope, 0.55)
def step_0g_extract_continent_snapshots(self, env: PlanetEnvelope) -> PlanetEnvelope:
    """Step 0.6 (Phase 1): Create continent snapshots (2021–2025).

    Uses ``osmium time-filter`` to create yearly point-in-time snapshots
    from the flat continent PBFs produced by Step 0.5.

    Idempotent: skips dates and continents that already exist on disk.
    """
    from extraction.services.continent_snapshot_service import (
        ContinentSnapshotService,
    )

    service = ContinentSnapshotService(force=False)
    all_results = service.run()

    # Count totals
    total_dates = 0
    total_continents = 0
    total_success = 0
    for date, results in all_results.items():
        if isinstance(results, dict) and "status" not in results:
            total_dates += 1
            for cont, res in results.items():
                total_continents += 1
                if res.get("success"):
                    total_success += 1
        elif isinstance(results, dict) and results.get("status") == "skipped":
            total_dates += 1
            # A "skipped" date was complete with all continents already done.
            # Parse the actual continent count from the message so 0/0 is avoided.
            msg = results.get("message", "")
            import re
            m = re.search(r"(\d+)/(\d+)", msg)
            if m:
                total_continents += int(m.group(2))
                total_success += int(m.group(1))
            else:
                # Fallback: count existing .pbf files on disk
                from pathlib import Path
                from django.conf import settings
                snapshot_dir = Path(settings.OSM_WIKIDATA_EXTRACTIONS_DIR) / "continents" / date
                existing = len(list(snapshot_dir.glob("*.pbf"))) if snapshot_dir.exists() else 0
                total_continents += existing
                total_success += existing

    _log(
        logger,
        "info",
        f"Phase 1 complete: {total_continents} continents across "
        f"{total_dates} years (success={total_success})",
        pipeline_run_id=env.pipeline_run_id,
    )

    return env
