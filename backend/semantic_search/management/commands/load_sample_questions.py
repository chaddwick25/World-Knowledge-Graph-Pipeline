"""Seed curated country-level sample questions from the CSV manifest.

The manifest lives at ``semantic_search/data/sample_questions.csv``
(country_code, question, template) — the same 15-country × ~6-question
set the frontend used to bundle. Idempotent: replaces curated rows for
the countries present in the file.
"""

import csv

from django.core.management.base import BaseCommand

from semantic_search.models import SampleQuestion


class Command(BaseCommand):
    help = (
        "Seed curated country-level sample questions from "
        "semantic_search/data/sample_questions.csv."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--csv", default="semantic_search/data/sample_questions.csv",
            help="CSV manifest path (country_code,question,template).",
        )
        parser.add_argument(
            "--verify", action="store_true",
            help="After seeding, run every curated question through the real "
            "parser+executor and delete any that return no results "
            "(criterion: each question has results).",
        )

    def handle(self, *args, **options):
        path = options["csv"]
        rows = []
        with open(path, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                country = (row.get("country_code") or "").strip().upper()
                question = (row.get("question") or "").strip()
                template = (row.get("template") or "").strip()
                if not country or not question:
                    continue
                rows.append(SampleQuestion(
                    country_code=country,
                    subdivision_qid="",
                    question=question,
                    template=template,
                    source="curated",
                ))

        countries = sorted({r.country_code for r in rows})
        SampleQuestion.objects.filter(
            country_code__in=countries, subdivision_qid="",
            source="curated",
        ).delete()
        SampleQuestion.objects.bulk_create(rows)

        if options["verify"]:
            from semantic_search.services.sample_question_service import (
                SampleQuestionService,
            )
            stored = list(SampleQuestion.objects.filter(
                country_code__in=countries, subdivision_qid="",
                source="curated",
            ))
            dead = []
            for r in stored:
                check = SampleQuestionService._verify_question(
                    r.question, r.country_code, r.snapshot_date or None,
                )
                if not check["ok"]:
                    dead.append(r)
            SampleQuestion.objects.filter(
                id__in=[r.id for r in dead],
            ).delete()
            self.stdout.write(self.style.SUCCESS(
                f"Seeded {len(stored) - len(dead)}/{len(stored)} curated "
                f"questions (deleted {len(dead)} with no results)."
            ))
            return

        self.stdout.write(self.style.SUCCESS(
            f"Seeded {len(rows)} curated sample questions "
            f"({len(countries)} countries: {', '.join(countries)})"
        ))
