"""Terminal end-to-end demo of the research orchestrator.

Streams the same SSE events the frontend sees to stdout: plan →
question × N → (follow_up / replan / round) → summary_delta → done.

Usage::

    python manage.py research_demo --prompt "How well is Belize City served by public transit?"
    python manage.py research_demo --prompt "Overview the restaurants and shops in Belmopan" \\
        --country BZ --snapshot-date 2025_12_31

Defaults: prompt = "How well is Belize City served by public transit?"
(the default PLACE_REPORT recipe), country = BZ, snapshot = the latest
SnapshotJob date. Exit code 1 when the loop produces no summary.

Restored 2026-09-30 (removed as dead code in 84e1459 while the docs —
rule 13, the MVP plan — still documented it): it is the terminal
verification path for the research loop and now prints the recipe, the
coverage ledger, and the continuation rounds alongside the events.
"""

import json
import sys
import uuid

from django.core.management.base import BaseCommand

from semantic_search.services.research_service import ResearchOrchestratorService


class Command(BaseCommand):
    help = (
        "Run the research orchestrator end-to-end and print its SSE events "
        "(decompose → per-question answers → follow-ups → final summary)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--prompt",
            default="How well is Belize City served by public transit?",
            help="Research prompt to decompose (default: Belize place report).",
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
            # Accept both call forms: dict (the loop's event_callback
            # contract — same normalization as the SSE view) and kwargs.
            if isinstance(event, dict):
                payload = {k: v for k, v in event.items() if k != "event"}
                event = event.get("event") or "message"
            line = {"event": event, **payload}
            self.stdout.write(
                "→ " + json.dumps(line, ensure_ascii=False, default=str)[:600]
            )

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
        if result.get("coverage"):
            self.stdout.write(self.style.WARNING("COVERAGE"))
            for c in result["coverage"]:
                flags = []
                if c.get("class_mismatch"):
                    flags.append("class_mismatch")
                if c.get("degenerate_distances"):
                    flags.append("degenerate_distances")
                self.stdout.write(
                    f"  {c['slot']}: {c['status']} "
                    f"({c['result_count']} results) "
                    f"{' '.join(flags)} — {c['question'][:70]}"
                )
            if result.get("rounds", 1) > 1:
                self.stdout.write(
                    self.style.WARNING(f"ROUNDS: {result['rounds']}")
                )
            if result.get("recipe"):
                self.stdout.write(f"RECIPE: {result['recipe']}")

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
