"""Step 5c run-report writer.

Extracted from ``step_5c_graph_spectral.py`` (monolith split, Phase 5 of
PIPELINE_CONTROL_PLANE_AND_CLEANUP_PLAN).
"""

import json
import logging
import os

from pipeline.tasks.helper import _log

logger = logging.getLogger("pipeline")


def _write_report(report: dict, env) -> None:
    """Write the Step 5c run report as a JSON file.

    Reports are saved to ``{SPECTRAL_REPORT_DIR}/{country}_{snapshot}_{run_id}.json``.
    If ``SPECTRAL_REPORT_DIR`` is not configured, the report is logged
    but not written to disk.
    """
    try:
        from django.conf import settings
        report_dir = getattr(settings, 'SPECTRAL_REPORT_DIR', None)
        if not report_dir:
            # Log the report as structured info
            _log(
                logger, "info",
                "Step 5c: Run report (SPECTRAL_REPORT_DIR not configured)",
                country=env.iso,
                snapshot_date=env.snapshot_date,
                pipeline_run_id=env.pipeline_run_id,
                report=json.dumps(report, indent=2),
            )
            return

        os.makedirs(report_dir, exist_ok=True)
        run_id = env.pipeline_run_id or "norunid"
        filename = f"step5c_{env.iso.lower()}_{env.snapshot_date}_{run_id}.json"
        path = os.path.join(report_dir, filename)
        with open(path, "w") as f:
            json.dump(report, f, indent=2)

        _log(
            logger, "info",
            "Step 5c: Run report written",
            country=env.iso,
            snapshot_date=env.snapshot_date,
            pipeline_run_id=env.pipeline_run_id,
            path=path,
            status=report["status"],
            solver=report.get("stages", {}).get("eigensolve", {}).get("solver", "n/a"),
        )
    except Exception as exc:
        _log(
            logger, "warning",
            "Step 5c: Failed to write run report — non-fatal",
            country=env.iso,
            error=str(exc),
        )
