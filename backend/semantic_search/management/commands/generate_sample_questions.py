"""Pre-generate subdivision-scoped sample questions.

Selects the named entities whose geom falls inside the subdivision
polygon and builds questions from the amenities that actually exist near
them (inside the polygon) — see SampleQuestionService. Deterministic for
a given snapshot/DB state, so generation is stable across runs.

Modes::

    # One subdivision
    python manage.py generate_sample_questions --country JM --subdivision Q875881
    # All subdivisions of a country (with a populated bbox) + the
    # country-wide set
    python manage.py generate_sample_questions --country BZ
    # Every subdivision with a bbox across all countries (idempotent)
    python manage.py generate_sample_questions --all
    # Only the country-wide set (already included in --country/--all)
    python manage.py generate_sample_questions --cross-subdivision --country BZ

A batch run (--country/--all) generates BOTH the per-subdivision rows
and the country-wide (cross-subdivision) set under the same criteria —
one invocation produces complete coverage. Sparse subdivisions (fewer
than --min-entities named entities) are SKIPPED — the country's curated
fallback serves them. Exit code 1 when any subdivision hard-fails
(no bbox / generation failure); sparse skips do not fail the run.
"""

import sys

from django.core.management.base import BaseCommand

from core.models import SubgraphProfile
from semantic_search.services.sample_question_service import (
    SampleQuestionService,
)


class Command(BaseCommand):
    help = "Pre-generate sample questions for country subdivisions."

    def add_arguments(self, parser):
        parser.add_argument(
            "--country", default=None, help="ISO-2 code — generate for all "
            "subdivisions of this country with a bbox.",
        )
        parser.add_argument(
            "--subdivision", default=None, help="Wikidata QID (e.g. Q875881).",
        )
        parser.add_argument(
            "--all", action="store_true",
            help="Generate for every subdivision with a bbox, all countries.",
        )
        parser.add_argument(
            "--cross-subdivision", action="store_true",
            help="Generate ONLY the country-wide set (spans subdivisions). "
            "Batch runs (--country/--all) already include it.",
        )
        parser.add_argument(
            "--verify-only", action="store_true",
            help="Verify existing generated rows (execute-and-keep): delete "
            "questions the real parser+executor returns no results for.",
        )
        parser.add_argument(
            "--snapshot-date", default=None, help="YYYY_MM_DD (default: "
            "the country's snapshot, else latest).",
        )
        parser.add_argument(
            "--per-anchor", type=int, default=2,
            help="Max questions per anchor (default 2).",
        )
        parser.add_argument(
            "--min-entities", type=int, default=2,
            help="Skip subdivisions with fewer named entities (default 2).",
        )

    def handle(self, *args, **options):
        if options["verify_only"]:
            self._verify_only(options)
            return
        if options["cross_subdivision"]:
            self._generate_cross_subdivision(options)
            return
        modes = sum(1 for m in (
            options["country"], options["subdivision"], options["all"],
        ) if m)
        if modes != 1:
            self.stderr.write(self.style.ERROR(
                "Exactly one of --country, --subdivision, or --all is required."
            ))
            sys.exit(2)

        if options["subdivision"]:
            self._generate_one(
                options["country"], options["subdivision"], options,
            )
            return
        self._generate_batch(options)

    # ── Cross-subdivision (country-wide, spans subdivisions) ─────────────

    def _generate_cross_subdivision(self, options):
        countries = [options["country"].upper()] if options["country"] else None
        if countries is None:
            from django.db.models import Count
            from core.models import SubgraphProfile
            countries = [
                r["country_profile__iso2"]
                for r in SubgraphProfile.objects
                .exclude(country_profile__iso2__isnull=True)
                .values("country_profile__iso2")
                .annotate(n=Count("id"))
                .order_by("country_profile__iso2")
            ]
        total_generated = 0
        errors = 0
        for iso in countries:
            result = SampleQuestionService.generate_cross_subdivision(
                iso, snapshot_date=options["snapshot_date"],
            )
            if result.get("error"):
                self.stderr.write(self.style.ERROR(f"  ✗ {iso}: {result['error']}"))
                errors += 1
                continue
            if result.get("skipped"):
                self.stdout.write(self.style.WARNING(
                    f"  - {iso}: skipped ({result['reason']})"
                ))
                continue
            total_generated += result["generated"]
            self.stdout.write(self.style.SUCCESS(
                f"  {iso}: generated {result['generated']} "
                f"(verified {result['verified']}, dropped {result['dropped']})"
            ))
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"CROSS-SUBDIVISION COVERAGE: {total_generated} questions "
            f"across {len(countries)} country(ies)"
        ))
        if errors:
            sys.exit(1)

    # ── Verify-only sweep (execute-and-keep enforcement) ─────────────────

    def _verify_only(self, options):
        from semantic_search.models import SampleQuestion

        qs = SampleQuestion.objects.filter(source="generated")
        if options["country"]:
            qs = qs.filter(country_code__iexact=options["country"])
        rows = list(qs)
        if not rows:
            self.stdout.write("No generated rows to verify.")
            return
        dead = []
        for r in rows:
            check = SampleQuestionService._verify_question(
                r.question, r.country_code, r.snapshot_date or None,
            )
            if not check["ok"]:
                dead.append(r)
        SampleQuestion.objects.filter(id__in=[r.id for r in dead]).delete()
        self.stdout.write(self.style.SUCCESS(
            f"Verified {len(rows) - len(dead)}/{len(rows)} generated rows "
            f"(deleted {len(dead)} with no results)."
        ))
        if dead:
            self.stdout.write(self.style.WARNING(
                "Deleted: " + "; ".join(
                    f"{r.country_code}/{r.subdivision_qid or 'country'}: "
                    f"{r.question[:50]}" for r in dead[:8]
                )
            ))

    # ── Single subdivision ───────────────────────────────────────────────

    def _generate_one(self, country, qid, options):
        result = SampleQuestionService.generate_subdivision(
            country, qid,
            snapshot_date=options["snapshot_date"],
            per_anchor=options["per_anchor"],
            min_entities=options["min_entities"],
        )
        if result.get("error"):
            self.stderr.write(self.style.ERROR(
                f"{country}/{qid}: {result['error']}"
            ))
            sys.exit(1)
        if result.get("skipped"):
            self.stdout.write(self.style.WARNING(
                f"Skipped {country}/{qid}: {result['reason']}"
            ))
            return
        self.stdout.write(self.style.SUCCESS(
            f"Generated {result['generated']} questions for "
            f"{country}/{qid} (snapshot {result['snapshot_date'] or 'latest'})"
        ))

    # ── Batch ────────────────────────────────────────────────────────────

    def _generate_batch(self, options):
        qs = SubgraphProfile.objects.filter(
            wikidata_id__isnull=False,
        ).exclude(wikidata_id='')
        if options["country"]:
            qs = qs.filter(country_profile__iso2__iexact=options["country"])
        # Only subdivisions whose bbox can scope a search.
        from django.db.models import Q as _Q
        qs = qs.filter(
            _Q(bbox_min_lon__isnull=False) & _Q(bbox_max_lat__isnull=False),
        ).order_by("country_profile__iso2", "name")

        rows = list(qs)
        if not rows:
            self.stderr.write(self.style.ERROR(
                "No subdivisions with a bbox to generate for "
                f"{options['country'] or 'all countries'}. "
                "Run backfill_subdivision_bboxes first."
            ))
            sys.exit(1)

        per_country = {}
        errors = []
        for sg in rows:
            iso = sg.country_profile.iso2 or ""
            try:
                result = SampleQuestionService.generate_subdivision(
                    iso, sg.wikidata_id,
                    snapshot_date=options["snapshot_date"],
                    per_anchor=options["per_anchor"],
                    min_entities=options["min_entities"],
                )
            except Exception as exc:  # noqa: BLE001 — one bad subdivision must not kill the batch
                result = {"error": str(exc)}
            if result.get("error"):
                errors.append((sg.name, iso, result["error"]))
                self.stderr.write(self.style.ERROR(
                    f"  ✗ {sg.name} ({iso}): {result['error']}"
                ))
                continue
            bucket = per_country.setdefault(iso, {"generated": 0, "skipped": 0})
            if result.get("skipped"):
                bucket["skipped"] += 1
                self.stdout.write(self.style.WARNING(
                    f"  - {sg.name} ({iso}): skipped ({result['reason']})"
                ))
            else:
                bucket["generated"] += 1

        # Coverage report
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("COVERAGE"))
        total_generated = 0
        for iso in sorted(per_country):
            b = per_country[iso]
            total_generated += b["generated"]
            self.stdout.write(
                f"  {iso}: generated {b['generated']} / "
                f"skipped {b['skipped']} (sparse)"
            )
        self.stdout.write(self.style.SUCCESS(
            f"Subdivisions with generated questions: {total_generated}/{len(rows)}"
        ))

        # The country-wide set gets the same treatment — a batch run
        # produces complete coverage (subdivisions + cross-subdivision)
        # in one invocation; --cross-subdivision alone refreshes only
        # the country-wide rows.
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("COUNTRY-WIDE"))
        for iso in sorted(per_country):
            result = SampleQuestionService.generate_cross_subdivision(
                iso, snapshot_date=options["snapshot_date"],
            )
            if result.get("skipped"):
                self.stdout.write(self.style.WARNING(
                    f"  - {iso}: skipped ({result['reason']})"
                ))
            elif result.get("error"):
                self.stdout.write(self.style.WARNING(
                    f"  - {iso}: {result['error']}"
                ))
            else:
                self.stdout.write(
                    f"  {iso}: generated {result['generated']} "
                    f"(verified {result['verified']}, "
                    f"dropped {result['dropped']})"
                )

        if errors:
            self.stdout.write(self.style.ERROR(
                f"{len(errors)} subdivision(s) failed: "
                + "; ".join(f"{n} ({iso}): {e}" for n, iso, e in errors[:5])
            ))
            sys.exit(1)
