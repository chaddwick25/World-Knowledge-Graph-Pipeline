"""Terminal end-to-end demo of the research orchestrator.

Streams the same SSE events the frontend sees to stdout: plan →
question × N → (optional tool calls) → summary_delta → done.

Usage::

    python manage.py research_demo --prompt "Plan a 2-day trip to Belize City"
    python manage.py research_demo --prompt "Plan a 2-day trip to Belize City" \\
        --country BZ --snapshot-date 2025_12_31

Defaults: prompt = "Plan a 2-day trip to Belize City", country = BZ,
snapshot = the latest SnapshotJob date. Exit code 1 when the loop
produces no summary.
"""

import json
import sys
import uuid

from django.core.management.base import BaseCommand

from semantic_search.services.research_service import ResearchOrchestratorService


class Command(BaseCommand):
    help = (
        "Run the research orchestrator end-to-end and print its SSE events "
        "(decompose → per-question answers → final summary)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--prompt", default="Plan a 2-day trip to Belize City",
            help="Research prompt to decompose (default: Belize trip plan).",
        )
        parser.add_argument(
            "--country", default="BZ",
            help="ISO-2 country code (default: BZ).",
        )
        parser.add_argument(
            "--snapshot-date", default=None,
            help="Snapshot date YYYY_MM_DD (default: latest SnapshotJob).",
        )

    def handle(self, *args, **options):
        prompt = options["prompt"]
        country = options["country"]
        snapshot_date = options["snapshot_date"] or self._latest_snapshot_date()

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"Research: {prompt!r} | country={country} "
            f"| snapshot={snapshot_date or 'latest'}"
        ))

        def emit(event, **payload):
            line = {"event": event, **payload}
            self.stdout.write("→ " + json.dumps(line, ensure_ascii=False, default=str)[:600])

        # One trace per demo run (console sink shows it; Langfuse sink
        # ingests it). Thread-local: LLM spans attach as children.
        from core.services.trace_service import TraceService

        trace_id = uuid.uuid4().hex
        self.stdout.write(f"trace: {trace_id}")

        with TraceService.run_trace(
            "research", trace_id=trace_id,
            metadata={"country": country, "snapshot_date": snapshot_date},
        ):
            result = ResearchOrchestratorService.plan(
                prompt, country, snapshot_date, event_callback=emit,
            )
        if isinstance(result, dict):
            result.setdefault("trace_id", trace_id)

        self.stdout.write("")
        if result.get("summary"):
            self.stdout.write(self.style.SUCCESS("SUMMARY"))
            self.stdout.write(result["summary"])
        else:
            self.stdout.write(self.style.ERROR(
                "No summary (research LLM unavailable or empty). "
                f"errors={json.dumps(result.get('errors') or [])}"
            ))
            if result.get("errors"):
                sys.exit(1)

        if result.get("errors"):
            self.stdout.write(self.style.WARNING(
                f"Questions with errors: {len(result['errors'])}"
            ))

    @staticmethod
    def _latest_snapshot_date() -> str:
        """Latest SnapshotJob snapshot_date, or None."""
        try:
            from osmsnapshot.models import SnapshotJob
            return (
                SnapshotJob.objects.order_by("-snapshot_date")
                .values_list("snapshot_date", flat=True).first()
            )
        except Exception:  # noqa: BLE001 — the demo must not fail on lookup
            return None
