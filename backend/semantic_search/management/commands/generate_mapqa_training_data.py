"""
generate_mapqa_training_data — self-supervised MapQA training-data generation.

Generates question–answer pairs for the 5 existing MapQA macro-templates
(#1, #2, #4, #5, #8) from ``OsmEntity`` ground truth in already-processed
countries (no graph/factor-table dependency — see
docs/plans/MAPQA_TEMPLATE_COVERAGE_EXPANSION_PLAN.md).

Answers are computed exactly from PostGIS at generation time and appended to
``training_data/natural_language_qa_pairs.csv`` (source-controlled), keyed by
``Source=self_supervised``. Re-running for the same
(Country_code, Snapshot_date, Macro-template) replaces the previous batch
(idempotent — the CSV never grows on re-runs). The CSV is rewritten
atomically (temp file + rename).

Usage:
    python manage.py generate_mapqa_training_data --country BZ
    python manage.py generate_mapqa_training_data --country BZ,JM,IE --per-class 250
    python manage.py generate_mapqa_training_data --all-processed --dry-run
    python manage.py generate_mapqa_training_data --all-processed --retrain
"""

import csv
import os
import tempfile
from collections import Counter
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

from semantic_search.services.mapqa_paraphraser import MapQAParaphraser
from semantic_search.services.mapqa_question_generator import (
    TEMPLATE_BY_NUMBER,
    MapQAQuestionGenerator,
)
from worldkg_nca.snapshot_utils import get_latest_snapshot_id

CSV_FIELD_NAMES = [
    "Macro-template", "Concept transformation", "What metric it produces",
    "MapQA question", "Question type", "Region", "Answer",
    "Source", "Pipeline_run_id", "Snapshot_date", "Ground_truth_table",
    "Country_code",
]


class Command(BaseCommand):
    help = "Generate self-supervised MapQA training data from OsmEntity ground truth."

    def add_arguments(self, parser):
        parser.add_argument(
            "--country", default=None,
            help="Comma-separated ISO 3166-1 alpha-2 codes "
                 "(e.g. 'BZ,JM,IE'). Mutually exclusive with --all-processed.",
        )
        parser.add_argument(
            "--all-processed", action="store_true",
            help="Generate for every country with a COMPLETED pipeline run.",
        )
        parser.add_argument(
            "--templates", default="1,2,4,5,8",
            help="Comma-separated template numbers (default: 1,2,4,5,8).",
        )
        parser.add_argument(
            "--per-class", type=int, default=250,
            help="Seed budget per (template × country) (default: 250).",
        )
        parser.add_argument(
            "--max-per-class", type=int, default=1500,
            help="Global cap on generated rows per template across all "
                 "countries (class-balance guard, default: 1500).",
        )
        parser.add_argument(
            "--snapshot", default=None,
            help="Snapshot partition key override (YYYY_MM_DD). Default: "
                 "latest snapshot in the vectors DB.",
        )
        parser.add_argument(
            "--seed", type=int, default=42,
            help="RNG seed for deterministic generation (default: 42).",
        )
        parser.add_argument(
            "--paraphrase-per-seed", type=int, default=2,
            help="Max rule-based paraphrases per seed row (default: 2).",
        )
        parser.add_argument(
            "--llm", action="store_true",
            help="Enable the LLM paraphrase tier for this run (overrides "
                 "MAPQA_LLM_AUGMENTATION_ENABLED for one run).",
        )
        parser.add_argument(
            "--output", default=None,
            help="Output CSV path (default: "
                 "{MAPQA_PARSER_DATA_DIR}/training_data/"
                 "natural_language_qa_pairs.csv).",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Generate and print a summary without writing the CSV.",
        )
        parser.add_argument(
            "--retrain", action="store_true",
            help="Run train_mapqa_parser after generation.",
        )

    def handle(self, *args, **opts):
        templates = self._parse_templates(opts["templates"])
        countries = self._resolve_countries(opts["country"], opts["all_processed"])
        if not countries:
            raise CommandError(
                "No countries resolved. Pass --country BZ,JM,IE or --all-processed."
            )

        snapshot_id = opts["snapshot"] or get_latest_snapshot_id()
        if not snapshot_id:
            raise CommandError(
                "No snapshot_id available (vectors DB has no backfilled "
                "snapshot). Pass --snapshot YYYY_MM_DD explicitly."
            )

        per_class = max(1, opts["per_class"])
        max_per_class = max(per_class, opts["max_per_class"])

        llm_enabled = opts["llm"] or bool(
            getattr(settings, "MAPQA_LLM_AUGMENTATION_ENABLED", False)
        )
        paraphraser = MapQAParaphraser(llm_enabled=llm_enabled)

        generator = MapQAQuestionGenerator(seed=opts["seed"])
        all_rows = []
        for country in countries:
            pipeline_run_id = self._pipeline_run_id(country)
            self.stdout.write(
                f"Generating for {country} (snapshot {snapshot_id}, "
                f"run {pipeline_run_id or '—'})..."
            )
            rows = generator.generate(
                country_code=country,
                snapshot_id=snapshot_id,
                templates=templates,
                per_class=per_class,
                pipeline_run_id=pipeline_run_id,
                paraphraser=paraphraser,
                paraphrase_per_seed=opts["paraphrase_per_seed"],
            )
            self.stdout.write(f"  {len(rows)} rows")
            all_rows.extend(rows)

        if not all_rows:
            self.stdout.write(self.style.WARNING(
                "No rows generated (no eligible entities/amenities?)."
            ))
            return

        # Class-balance guard: cap generated rows per template across countries.
        all_rows = self._cap_per_template(all_rows, max_per_class, opts["seed"])
        # Stratified 20% per-template holdout → self_supervised_test.
        generator.split_holdout(all_rows, holdout_frac=0.2)

        self._print_summary(all_rows, templates)

        if opts["dry_run"]:
            self.stdout.write(self.style.WARNING(
                "Dry run — CSV not written."
            ))
            return

        output_path = Path(opts["output"]) if opts["output"] else Path(
            settings.MAPQA_PARSER_DATA_DIR
        ) / "training_data" / "natural_language_qa_pairs.csv"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        self._merge_into_csv(output_path, all_rows)
        self.stdout.write(self.style.SUCCESS(
            f"Wrote {len(all_rows)} rows to {output_path}"
        ))

        if opts["retrain"]:
            self.stdout.write("\n=== Retraining parser ===")
            call_command("train_mapqa_parser")

    # ── Resolution helpers ─────────────────────────────────────────────────

    def _parse_templates(self, raw: str) -> tuple:
        template_nums = []
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                num = int(part)
            except ValueError:
                raise CommandError(f"Invalid template number: {part!r}")
            if num not in TEMPLATE_BY_NUMBER:
                raise CommandError(
                    f"Template #{num} not supported (use 1,2,4,5,8)."
                )
            template_nums.append(num)
        if not template_nums:
            raise CommandError("No templates selected.")
        return tuple(template_nums)

    def _resolve_countries(self, country_arg: str, all_processed: bool) -> list:
        """Resolve ISO codes → countries with data, in a stable order."""
        from core.models.country_profile import CountryPipelineProfile
        from core.models.pipeline import PipelineRun

        if all_processed and country_arg:
            raise CommandError("--country and --all-processed are mutually exclusive.")

        codes = []
        if all_processed:
            completed = (
                PipelineRun.objects.filter(status="COMPLETED")
                .values_list("country_code", flat=True)
            )
            codes = sorted({c.strip().upper() for c in completed if c})
        elif country_arg:
            codes = [c.strip().upper() for c in country_arg.split(",") if c.strip()]

        resolved = []
        for code in codes:
            profile = (
                CountryPipelineProfile.objects.filter(iso2=code).first()
                or CountryPipelineProfile.objects.filter(iso3=code).first()
            )
            if profile is None:
                self.stdout.write(self.style.WARNING(
                    f"  Skipping {code}: no CountryPipelineProfile row."
                ))
                continue
            iso2 = (profile.iso2 or profile.iso3 or code).upper()
            if iso2 not in resolved:
                resolved.append(iso2)
        return resolved

    @staticmethod
    def _pipeline_run_id(country_code: str) -> str:
        """Latest COMPLETED run id for a country (empty if none)."""
        from core.models.pipeline import PipelineRun
        run = (
            PipelineRun.objects.filter(
                country_code__iexact=country_code, status="COMPLETED"
            )
            .order_by("-created_at")
            .first()
        )
        return str(run.id) if run else ""

    # ── Cap + split ────────────────────────────────────────────────────────

    @staticmethod
    def _cap_per_template(rows: list, max_per_class: int, seed: int) -> list:
        """Trim generated rows per template to ``max_per_class``.

        Seed rows (Question type != self_supervised_paraphrase) are kept
        first so paraphrase variants never crowd out ground-truth seeds;
        deterministic shuffle makes the trim reproducible.
        """
        import random
        by_template = {}
        for row in rows:
            by_template.setdefault(row["Macro-template"], []).append(row)
        capped = []
        for template, template_rows in by_template.items():
            if len(template_rows) <= max_per_class:
                capped.extend(template_rows)
                continue
            seeds = [r for r in template_rows
                     if r["Question type"] != "self_supervised_paraphrase"]
            paras = [r for r in template_rows
                     if r["Question type"] == "self_supervised_paraphrase"]
            rng = random.Random(f"cap:{seed}:{template}")
            rng.shuffle(paras)
            kept = seeds + paras[:max(0, max_per_class - len(seeds))]
            capped.extend(kept)
        return capped

    # ── CSV merge (idempotent + atomic) ────────────────────────────────────

    @classmethod
    def _merge_into_csv(cls, csv_path: Path, new_rows: list) -> None:
        """Replace prior self-supervised batches for the affected
        (Country_code, Snapshot_date, Macro-template) keys, then append.

        Hand-curated rows (Source != self_supervised) and self-supervised
        rows for other countries/snapshots/templates are preserved.
        """
        existing = []
        if csv_path.exists():
            with open(csv_path, "r", encoding="utf-8", newline="") as f:
                existing = [cls._with_defaults(r) for r in csv.DictReader(f)]

        replaced_keys = {
            (r.get("Country_code"), r.get("Snapshot_date"),
             r.get("Macro-template"))
            for r in new_rows
        }
        kept = [
            r for r in existing
            if not (r.get("Source") == "self_supervised"
                    and (r.get("Country_code"), r.get("Snapshot_date"),
                         r.get("Macro-template")) in replaced_keys)
        ]
        merged = kept + new_rows

        fd, tmp_path = tempfile.mkstemp(
            dir=str(csv_path.parent), prefix=csv_path.name, suffix=".tmp"
        )
        os.close(fd)
        try:
            with open(tmp_path, "w", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=CSV_FIELD_NAMES,
                                        restval="")
                writer.writeheader()
                writer.writerows(merged)
            os.replace(tmp_path, csv_path)
        except Exception:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

    @staticmethod
    def _with_defaults(row: dict) -> dict:
        """Fill extended-schema defaults for legacy rows in an existing CSV.

        ``setdefault`` is NOT sufficient: DictReader yields ``None`` for
        columns missing from a shorter legacy row, so the key exists with a
        falsy value and the default is skipped.
        """
        row = dict(row)
        region = (row.get("Region") or "").strip()
        if region == "augmented":
            default_source = "hand-curated"
        elif region in ("self_supervised", "self_supervised_test"):
            default_source = "self_supervised"
        else:
            default_source = "mapqa-llm"
        if not row.get("Source"):
            row["Source"] = default_source
        if not row.get("Pipeline_run_id"):
            row["Pipeline_run_id"] = ""
        if not row.get("Snapshot_date"):
            row["Snapshot_date"] = ""
        if not row.get("Ground_truth_table"):
            row["Ground_truth_table"] = ""
        if not row.get("Country_code"):
            row["Country_code"] = ""
        return row

    # ── Output ─────────────────────────────────────────────────────────────

    def _print_summary(self, rows: list, templates: tuple) -> None:
        by_template = Counter(r["Macro-template"] for r in rows)
        by_region = Counter(r["Region"] for r in rows)
        by_type = Counter(r["Question type"] for r in rows)
        self.stdout.write("\n=== Generation summary ===")
        for num in templates:
            template = TEMPLATE_BY_NUMBER[num]
            self.stdout.write(f"  {template:38s} {by_template.get(template, 0):5d}")
        self.stdout.write(f"  {'TOTAL':38s} {len(rows):5d}")
        self.stdout.write(
            f"  Regions: {dict(by_region)}"
        )
        self.stdout.write(f"  Question types: {dict(by_type)}")
