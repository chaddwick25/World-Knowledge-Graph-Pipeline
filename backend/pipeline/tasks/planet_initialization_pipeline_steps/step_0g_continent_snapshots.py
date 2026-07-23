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
from pipeline.celery_app import (
    celery_app,
    PipelineTask,
    CELERY_AVAILABLE,
)

if CELERY_AVAILABLE:
    
    logger = logging.getLogger("pipeline")
    @celery_app.task(
        bind=True,
        base=PipelineTask,
        name="step_0g_extract_continent_snapshots",
        max_retries=1,
        default_retry_delay=300,
    )
    def step_0g_extract_continent_snapshots(self, config_dict: dict) -> dict:
        """Step 0.6 (Phase 1): Create continent snapshots (2021–2025).

        Uses ``osmium time-filter`` to create yearly point-in-time snapshots
        from the flat continent PBFs produced by Step 0.5.

        Idempotent: skips dates and continents that already exist on disk.
        """
        config_dict = self.setup_pipeline_context(config_dict)
        _log(
            logger,
            "info",
            "Step 0.6 (Phase 1): Creating continent snapshots (2021–2025)",
            pipeline_run_id=config_dict.get("pipeline_run_id", ""),
        )
        _push_update(
            pipeline_run_id=config_dict.get("pipeline_run_id", ""),
            name="continent_snapshots",
            status="in_progress",
            message="Creating yearly continent snapshots...",
            pct=5,
        )

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

        _push_update(
            pipeline_run_id=config_dict.get("pipeline_run_id", ""),
            name="continent_snapshots",
            status="completed",
            message=f"Created {total_success}/{total_continents} snapshots "
                    f"across {total_dates} years",
            pct=100,
        )

        _log(
            logger,
            "info",
            f"Phase 1 complete: {total_continents} continents across "
            f"{total_dates} years (success={total_success})",
            pipeline_run_id=config_dict.get("pipeline_run_id", ""),
        )

        return {
            "snapshots": all_results,
            "total_dates": total_dates,
            "total_continents": total_continents,
            "total_success": total_success,
            "status": "completed",
            # Config pass-through for Celery chain continuity
            "iso": config_dict.get("iso", ""),
            "name": config_dict.get("name", ""),
            "continent": config_dict.get("continent", ""),
            "slug": config_dict.get("slug", ""),
            "pipeline_run_id": config_dict.get("pipeline_run_id", ""),
        }

