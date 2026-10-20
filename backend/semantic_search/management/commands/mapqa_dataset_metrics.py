"""Analyze MapQA dataset distribution and write metrics to CSV.

Reads the raw MapQA dataset (llm/ split) and the hand-curated
natural_language_qa_pairs.csv augmentation file, maps each question to
its Spatial-Agent macro-template, and writes a distribution report.

Output CSV columns:
    Template, Core Concept, Execution Path, Region, Source,
    Question Count, Percentage

Usage:
    python manage.py mapqa_dataset_metrics
    python manage.py mapqa_dataset_metrics --output /tmp/mapqa_metrics.csv
"""
import csv
import logging
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)

# ── Template metadata (from README template coverage table) ──────────
TEMPLATE_INFO = {
    "FILTER-AGGREGATE-MEASURE (#1)": {
        "core_concept": "Object, Location",
        "execution_path": "PostGIS ST_DWithin + Distance",
    },
    "GEOCODE-BATCH-COMPARE (#4)": {
        "core_concept": "Object, Neighbourhood",
        "execution_path": "PostGIS distance ordering",
    },
    "PLACE-ATTRIBUTE-QUERY (#8)": {
        "core_concept": "Object, Field",
        "execution_path": "Factor tables (spectral/community) + PostGIS",
    },
    "LOCATION-BEARING-CLASSIFY (#5)": {
        "core_concept": "Location, Neighbourhood",
        "execution_path": "PostGIS ST_DWithin + bearing calc",
    },
    "OBJECT-FIELD-MEASURE (#2)": {
        "core_concept": "Object, Field",
        "execution_path": "PostGIS ST_DWithin (optional)",
    },
    "SPECTRAL-ANALYSIS (#11)": {
        "core_concept": "Network",
        "execution_path": "Factor tables (eigenvalues, lambda_2, gap)",
    },
    "TEMPORAL-DRIFT (#12)": {
        "core_concept": "Event",
        "execution_path": "Factor tables (drift metrics)",
    },
    "COMMUNITY-DETECT (#13)": {
        "core_concept": "Object, Network",
        "execution_path": "Factor tables (Louvain communities)",
    },
    "EVENT-DIFFUSION (#14)": {
        "core_concept": "Network, Event",
        "execution_path": "Factor tables (heat kernel via pgvector)",
    },
}

# ── MapQA dataset file → macro-template mapping ──────────────────────
# Mirrors TEMPLATE_MAP in train_mapqa_parser.py
DATASET_TEMPLATE_MAP = {
    "amenities_dataset": "PLACE-ATTRIBUTE-QUERY (#8)",
    "nearest_amenity": "GEOCODE-BATCH-COMPARE (#4)",
    "amenities_around": "FILTER-AGGREGATE-MEASURE (#1)",
    "amenities-around-specific": "FILTER-AGGREGATE-MEASURE (#1)",
    "adjacent": "PLACE-ATTRIBUTE-QUERY (#8)",
    "compare-closer": "GEOCODE-BATCH-COMPARE (#4)",
    "direction_nearest": "LOCATION-BEARING-CLASSIFY (#5)",
    "distance": "OBJECT-FIELD-MEASURE (#2)",
    "intersection": "GEOCODE-BATCH-COMPARE (#4)",
}

# Templates not present in the raw MapQA dataset — only in augmentations
# or exposed via dedicated API endpoints
AUGMENTED_TEMPLATES = {
    "SPECTRAL-ANALYSIS (#11)",
    "TEMPORAL-DRIFT (#12)",
    "COMMUNITY-DETECT (#13)",
    "EVENT-DIFFUSION (#14)",
}

DATASET_REGIONS = ["california_full", "illinois_test"]


def _read_csv_questions(path: Path) -> int:
    """Count questions in a MapQA dataset CSV file."""
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        count = 0
        for row in reader:
            if len(row) >= 2:
                count += 1
    return count


def _read_json_questions(path: Path) -> int:
    """Count questions in a MapQA dataset JSON file."""
    import json
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return len(data)
    return 0


def _resolve_template(stem: str) -> str:
    """Map a CSV/JSON filename stem to its macro-template."""
    if stem in DATASET_TEMPLATE_MAP:
        return DATASET_TEMPLATE_MAP[stem]
    alt = stem.replace("_dataset", "")
    if alt in DATASET_TEMPLATE_MAP:
        return DATASET_TEMPLATE_MAP[alt]
    return "UNKNOWN"


class Command(BaseCommand):
    help = "Analyze MapQA dataset distribution and write metrics to CSV"

    def add_arguments(self, parser):
        parser.add_argument(
            "--output",
            default="mapqa_dataset_metrics.csv",
            help="Output CSV path (default: mapqa_dataset_metrics.csv)",
        )
        parser.add_argument(
            "--format",
            choices=["csv", "json"],
            default="csv",
            help="Output format (default: csv)",
        )

    def handle(self, *args, **options):
        output_path = Path(options["output"])

        mapqa_dir = getattr(settings, "MAPQA_PARSER_DATA_DIR", None)
        if not mapqa_dir:
            self.stderr.write("MAPQA_PARSER_DATA_DIR not configured")
            return

        raw_dir = Path(mapqa_dir) / "raw" / "MapQA-dataset-main" / "llm"
        if not raw_dir.exists():
            self.stderr.write(f"Raw MapQA dataset not found at {raw_dir}")
            return

        # ── Collect counts ───────────────────────────────────────────
        # rows: (template, region, source, fmt, count)
        rows = []

        for region in DATASET_REGIONS:
            qa_dir = raw_dir / region / "question-answer"
            if not qa_dir.exists():
                logger.warning("Region %s not found, skipping", region)
                continue

            for csv_file in sorted(qa_dir.glob("*.csv")):
                stem = csv_file.stem
                template = _resolve_template(stem)
                count = _read_csv_questions(csv_file)
                rows.append((template, region, "MapQA-llm", "csv", count))

            for json_file in sorted(qa_dir.glob("*.json")):
                stem = json_file.stem
                template = _resolve_template(stem)
                count = _read_json_questions(json_file)
                rows.append((template, region, "MapQA-llm", "json", count))

        # ── Augmentation CSV (hand-curated) ──────────────────────────
        aug_csv = Path(mapqa_dir) / "natural_language_qa_pairs.csv"
        # Fall back to the source-controlled copy
        if not aug_csv.exists():
            aug_csv = (
                Path(settings.BASE_DIR)
                / "semantic_search" / "data"
                / "natural_language_qa_pairs.csv"
            )

        if aug_csv.exists():
            with open(aug_csv, "r", encoding="utf-8", newline="") as f:
                reader = csv.DictReader(f)
                for r in reader:
                    template = r.get("Macro-template", "UNKNOWN")
                    region = r.get("Region", "augmented")
                    rows.append((template, region, "Augmented", "csv", 1))
        else:
            logger.warning("Augmentation CSV not found at %s", aug_csv)

        # ── Aggregate ────────────────────────────────────────────────
        # Key: (template, region, source)
        from collections import defaultdict
        agg = defaultdict(int)
        for template, region, source, fmt, count in rows:
            agg[(template, region, source)] += count

        total = sum(agg.values())

        # ── Build output records ─────────────────────────────────────
        all_templates = list(TEMPLATE_INFO.keys())
        seen_templates = {k[0] for k in agg}
        for t in all_templates:
            if t not in seen_templates:
                agg[(t, "—", "—")] = 0  # zero-count placeholder

        records = []
        for (template, region, source), count in sorted(
            agg.items(), key=lambda x: (all_templates.index(x[0][0])
                                        if x[0][0] in all_templates else 99,
                                        x[0][1], x[0][2])
        ):
            info = TEMPLATE_INFO.get(template, {
                "core_concept": "—",
                "execution_path": "—",
            })
            pct = (count / total * 100) if total > 0 else 0.0
            records.append({
                "Template": template,
                "Core Concept": info["core_concept"],
                "Execution Path": info["execution_path"],
                "Region": region,
                "Source": source,
                "Question Count": count,
                "Percentage": f"{pct:.1f}%",
            })

        # ── Write CSV ────────────────────────────────────────────────
        fieldnames = [
            "Template", "Core Concept", "Execution Path",
            "Region", "Source", "Question Count", "Percentage",
        ]

        with open(output_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(records)

        self.stdout.write(self.style.SUCCESS(
            f"Wrote {len(records)} rows to {output_path} (total questions: {total})"
        ))

        # ── Console summary ──────────────────────────────────────────
        self.stdout.write("")
        self.stdout.write("=== Template Distribution Summary ===")
        template_totals = defaultdict(int)
        for (template, _, _), count in agg.items():
            template_totals[template] += count
        for template in all_templates:
            count = template_totals.get(template, 0)
            pct = (count / total * 100) if total > 0 else 0.0
            marker = " (no data)" if count == 0 else ""
            self.stdout.write(
                f"  {template:40s} {count:6d}  ({pct:5.1f}%){marker}"
            )
        self.stdout.write(f"  {'TOTAL':40s} {total:6d}")
