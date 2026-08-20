***REMOVED***!/usr/bin/env python3
"""
Map MapQA dataset questions to Spatial-Agent macro-template categories.

Reads all MapQA CSV files (california_full + illinois_test), maps each
question to its macro-template / concept transformation / metric, and writes
the result to a CSV at the repo root.

Output columns:
    Macro-template          - the template name from [SPATIAL_AGENT:Appendix E]
    Concept transformation  - the concept-to-concept transformation chain
    What metric it produces - the output metric type
    MapQA question          - the actual question text from the dataset
    Question type           - the MapQA question type (filename stem)
    Region                  - california_full | illinois_test
    Answer                  - the ground-truth answer from the dataset

Usage:
    python scripts/map_mapqa_to_templates.py
"""

import csv
import os
import sys
from pathlib import Path

***REMOVED*** ─── Mapping: MapQA question type → macro-template metadata ──────────────
***REMOVED*** Sourced from docs/plans/GEO_SPATIAL_AGENT_CELERY_PLAN_V3.md §1.5 and §3.5
***REMOVED***
***REMOVED*** Each entry: (macro_template, concept_transformation, metric_produced)

TEMPLATE_MAP = {
    "amenities_dataset": (
        "PLACE-ATTRIBUTE-QUERY (***REMOVED***8)",
        "OBJECT → POI details → amenity attribute → MEASURE",
        "Place amenity type (e.g. studio, fast_food)",
    ),
    "nearest_amenity": (
        "GEOCODE-BATCH-COMPARE (***REMOVED***4)",
        "OBJECT(anchor) + SUB_COND(amenity_type) → SUPPORT(nearest) → MEASURE",
        "Nearest entity of a given type around an anchor",
    ),
    "amenities_around": (
        "FILTER-AGGREGATE-MEASURE (***REMOVED***1)",
        "OBJECT(anchor) + SUB_COND(radius) → SUPPORT(within_radius) → MEASURE",
        "List of amenities within a radius",
    ),
    "amenities-around-specific": (
        "FILTER-AGGREGATE-MEASURE (***REMOVED***1)",
        "SUB_COND(amenity_type + radius) → SUPPORT(within_radius) → MEASURE",
        "List of specific amenity type within a radius",
    ),
    "adjacent": (
        "PLACE-ATTRIBUTE-QUERY (***REMOVED***8)",
        "OBJECT(anchor) + SUB_COND(amenity_type) → SUPPORT(adjacency) → MEASURE",
        "Adjacent entity of a given type",
    ),
    "compare-closer": (
        "GEOCODE-BATCH-COMPARE (***REMOVED***4)",
        "LOCATIONs → coordinates → SUPPORT(distance) × 2 → MEASURE(argmin)",
        "Which candidate is closest to the anchor",
    ),
    "direction_nearest": (
        "LOCATION-BEARING-CLASSIFY (***REMOVED***5)",
        "SUB_COND(direction) + SUPPORT(nearest in direction) → MEASURE",
        "Nearest entity in a cardinal direction from anchor",
    ),
    "distance": (
        "OBJECT-FIELD-MEASURE (***REMOVED***2)",
        "OBJECT(a) + OBJECT(b) → FIELD(haversine distance) → MEASURE",
        "Distance in meters between two entities",
    ),
    "intersection": (
        "GEOCODE-BATCH-COMPARE (***REMOVED***4)",
        "SUPPORT(geocode road intersection) + SUPPORT(nearest) → MEASURE",
        "Nearest entity to a road intersection point",
    ),
}

***REMOVED*** ─── File discovery ───────────────────────────────────────────────────────

***REMOVED*** Canonical paths (see backend/data/mapqa_parser/ layout):
***REMOVED***   raw/         — symlink to docs/Schematics/MapQA-dataset-main/
***REMOVED***   training_data/ — generated CSVs (mapqa_template_mapping.csv)
***REMOVED***   artifacts/   — trained model pickles (created by train_mapqa_parser)
MAPQA_LLM_DIR = Path("backend/data/mapqa_parser/raw/MapQA-dataset-main/llm")
REGIONS = ["california_full", "illinois_test"]
OUTPUT_CSV = Path("backend/data/mapqa_parser/training_data/mapqa_template_mapping.csv")


def discover_csv_files(base_dir: Path):
    """Return list of (region, question_type, filepath) for all CSV files."""
    results = []
    for region in REGIONS:
        qa_dir = base_dir / region / "question-answer"
        if not qa_dir.exists():
            print(f"WARNING: {qa_dir} does not exist, skipping", file=sys.stderr)
            continue
        for csv_path in sorted(qa_dir.glob("*.csv")):
            ***REMOVED*** Normalize the question type from the filename
            ***REMOVED*** e.g. "amenities_dataset.csv" → "amenities_dataset"
            ***REMOVED***      "amenities-around-specific.csv" → "amenities-around-specific"
            question_type = csv_path.stem
            results.append((region, question_type, csv_path))
    return results


***REMOVED*** ─── CSV reading ──────────────────────────────────────────────────────────

def read_questions(csv_path: Path):
    """Read a MapQA CSV and return list of (question, answer) tuples.

    Handles both header formats:
      - ID,Question,Answer  (most files)
      - Question,Answer     (distance_dataset.csv — malformed header, rows
        actually have 3 fields: ID, Question, Answer)
    """
    questions = []
    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        ***REMOVED*** Detect the distance_dataset.csv case: header says Question,Answer
        ***REMOVED*** but rows have 3 fields (ID, Question, Answer)
        if len(header) == 2 and header[0].strip() == "Question":
            ***REMOVED*** Malformed header — rows are actually (ID, Question, Answer)
            for row in reader:
                if len(row) >= 3:
                    questions.append((row[1].strip(), row[2].strip()))
                elif len(row) == 2:
                    questions.append((row[0].strip(), row[1].strip()))
        else:
            ***REMOVED*** Normal case: ID,Question,Answer (or similar)
            for row in reader:
                if len(row) >= 3:
                    questions.append((row[1].strip(), row[2].strip()))
                elif len(row) == 2:
                    questions.append((row[0].strip(), row[1].strip()))
    return questions


***REMOVED*** ─── Main ─────────────────────────────────────────────────────────────────

def main():
    repo_root = Path(__file__).resolve().parent.parent
    mapqa_dir = repo_root / MAPQA_LLM_DIR
    output_path = repo_root / OUTPUT_CSV

    if not mapqa_dir.exists():
        print(f"ERROR: MapQA directory not found at {mapqa_dir}", file=sys.stderr)
        sys.exit(1)

    files = discover_csv_files(mapqa_dir)
    if not files:
        print("ERROR: No CSV files found", file=sys.stderr)
        sys.exit(1)

    print(f"Found {len(files)} CSV files across {len(REGIONS)} regions")

    rows = []
    unmapped_types = set()

    for region, question_type, csv_path in files:
        ***REMOVED*** Look up the template mapping
        if question_type in TEMPLATE_MAP:
            macro_template, concept_transform, metric = TEMPLATE_MAP[question_type]
        else:
            ***REMOVED*** Try without _dataset suffix
            alt_key = question_type.replace("_dataset", "")
            if alt_key in TEMPLATE_MAP:
                macro_template, concept_transform, metric = TEMPLATE_MAP[alt_key]
            else:
                unmapped_types.add(question_type)
                macro_template = "UNKNOWN"
                concept_transform = "UNKNOWN"
                metric = "UNKNOWN"

        questions = read_questions(csv_path)
        for question, answer in questions:
            rows.append({
                "Macro-template": macro_template,
                "Concept transformation": concept_transform,
                "What metric it produces": metric,
                "MapQA question": question,
                "Question type": question_type,
                "Region": region,
                "Answer": answer,
            })

        print(f"  {region}/{question_type}: {len(questions)} questions")

    if unmapped_types:
        print(f"\nWARNING: Unmapped question types: {unmapped_types}", file=sys.stderr)

    ***REMOVED*** Write output CSV
    fieldnames = [
        "Macro-template",
        "Concept transformation",
        "What metric it produces",
        "MapQA question",
        "Question type",
        "Region",
        "Answer",
    ]

    with open(output_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nWrote {len(rows)} rows to {output_path}")
    print(f"\nSummary by macro-template:")
    from collections import Counter
    template_counts = Counter(r["Macro-template"] for r in rows)
    for template, count in sorted(template_counts.items(), key=lambda x: -x[1]):
        print(f"  {count:5d}  {template}")


if __name__ == "__main__":
    main()
