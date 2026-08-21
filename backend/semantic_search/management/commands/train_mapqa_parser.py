"""
Train the MapQA TF-IDF parser and serialize model artifacts.

Implements MAPQA_PARSER_BUILD_ORDER.md §4-6 and MAPQA_TO_EXECUTION_PLAN.md §1.2.
Trains three classifiers on the MapQA dataset:

  1. Template classifier  — TF-IDF + MultinomialNB (5 macro-templates)
  2. Concept extractor     — TF-IDF + OneVsRest Logistic Regression (multi-label,
                             7 core spatial concept types)
  3. Role assigner         — TF-IDF + Logistic Regression (multi-class,
                             6 functional roles)

The TF-IDF vectorizer is fit on the California training split ONLY (fixing
the §4.3 leakage caveat from the notebook). Illinois is held out as a
zero-shot test set.

If the training CSV (mapqa_template_mapping.csv) does not exist, it is
auto-generated from the raw MapQA dataset mounted at
{MAPQA_PARSER_DATA_DIR}/raw/MapQA-dataset-main/llm/. This makes the command
self-contained for Docker startup (init_planet step).

Artifacts are written to {MAPQA_PARSER_DATA_DIR}/artifacts/:
  vectorizer.pkl, template_classifier.pkl, label_encoder.pkl,
  concept_extractor.pkl, role_assigner.pkl, role_encoder.pkl,
  template_specs.json, amenity_vocab.json, metrics.json

Usage:
    python manage.py train_mapqa_parser
"""

import csv
import json
import logging
import pickle
import re
import shutil
import sys
from collections import Counter
from pathlib import Path

import numpy as np
from django.conf import settings
from django.core.management.base import BaseCommand
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import classification_report
from sklearn.multiclass import OneVsRestClassifier
from sklearn.naive_bayes import MultinomialNB
from sklearn.preprocessing import LabelEncoder

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────

# Core spatial concept space C ([SPATIAL_AGENT:§A.1])
CONCEPT_TYPES = [
    "LOCATION", "OBJECT", "FIELD", "EVENT",
    "NETWORK", "AMOUNT", "PROPORTION",
]

# Functional role space R ([SPATIAL_AGENT:§B.1])
ROLE_TYPES = [
    "EXTENT", "TEXTENT", "SUB_COND", "COND", "SUPPORT", "MEASURE",
]

# Template → concept type → role mapping ([SPATIAL_AGENT:§B.4] — Table 5)
TEMPLATE_ROLE_MAP = {
    "FILTER-AGGREGATE-MEASURE (#1)": {
        "OBJECT": "SUB_COND", "AMOUNT": "COND",
        "LOCATION": "SUPPORT", "FIELD": "MEASURE",
    },
    "GEOCODE-BATCH-COMPARE (#4)": {
        "OBJECT": "SUB_COND", "LOCATION": "SUPPORT",
        "AMOUNT": "MEASURE",
    },
    "PLACE-ATTRIBUTE-QUERY (#8)": {
        "OBJECT": "SUB_COND", "FIELD": "SUPPORT",
        "LOCATION": "MEASURE",
    },
    "LOCATION-BEARING-CLASSIFY (#5)": {
        "LOCATION": "SUB_COND", "AMOUNT": "MEASURE",
    },
    "OBJECT-FIELD-MEASURE (#2)": {
        "LOCATION": "SUB_COND", "AMOUNT": "MEASURE",
    },
}

# The 5 DAG skeletons — nodes ordered by role precedence.
# Each node specifies a role and the concept type it binds to.
# The MEASURE node is terminal (produces the answer).
# TODO: move this to a yaml file
# TODO: Generate the metrics for distribution of concepts across templates
TEMPLATE_SPECS = {
    "FILTER-AGGREGATE-MEASURE (#1)": {
        "nodes": [
            {"role": "SUB_COND", "concept_type": "OBJECT",
             "operator": "place_search"},
            {"role": "COND", "concept_type": "AMOUNT",
             "operator": "radius_filter"},
            {"role": "SUPPORT", "concept_type": "LOCATION",
             "operator": "geocode"},
            {"role": "MEASURE", "concept_type": "AMOUNT",
             "operator": "count"},
        ],
    },
    "GEOCODE-BATCH-COMPARE (#4)": {
        "nodes": [
            {"role": "SUB_COND", "concept_type": "OBJECT",
             "operator": "place_search"},
            {"role": "SUPPORT", "concept_type": "LOCATION",
             "operator": "geocode"},
            {"role": "MEASURE", "concept_type": "AMOUNT",
             "operator": "rank_by_distance"},
        ],
    },
    "PLACE-ATTRIBUTE-QUERY (#8)": {
        "nodes": [
            {"role": "SUB_COND", "concept_type": "OBJECT",
             "operator": "place_search"},
            {"role": "SUPPORT", "concept_type": "FIELD",
             "operator": "attribute_lookup"},
            {"role": "MEASURE", "concept_type": "LOCATION",
             "operator": "report"},
        ],
    },
    "LOCATION-BEARING-CLASSIFY (#5)": {
        "nodes": [
            {"role": "SUB_COND", "concept_type": "LOCATION",
             "operator": "geocode_a"},
            {"role": "COND", "concept_type": "LOCATION",
             "operator": "geocode_b"},
            {"role": "SUPPORT", "concept_type": "AMOUNT",
             "operator": "bearing"},
            {"role": "MEASURE", "concept_type": "AMOUNT",
             "operator": "bearing_to_direction"},
        ],
    },
    "OBJECT-FIELD-MEASURE (#2)": {
        "nodes": [
            {"role": "SUB_COND", "concept_type": "LOCATION",
             "operator": "geocode_a"},
            {"role": "COND", "concept_type": "LOCATION",
             "operator": "geocode_b"},
            {"role": "SUPPORT", "concept_type": "AMOUNT",
             "operator": "haversine"},
            {"role": "MEASURE", "concept_type": "AMOUNT",
             "operator": "report_distance"},
        ],
    },
}

# Signal-phrase vocabulary for concept detection heuristics
# TODO: do some EDA to see if these are good enough or maybe they can be factorized
# TODO: Double check the paper to gain any insights
# TODO: Figure how to leverage WorldKG's arifacts(Classes, Predicted Links) (soft edges) as signals or let a LLM agent handle that
AMOUNT_PATTERNS = [re.compile(r"\d+\s*m\b", re.I), re.compile(r"\d+\s*km\b", re.I)]
OBJECT_SIGNALS = {"bar", "restaurant", "cafe", "hotel", "school", "hospital",
                  "shop", "amenity", "bar", "pub", "bank", "pharmacy",
                  "park", "church", "library", "cinema", "theatre",
                  "gas", "fuel", "parking", "toilet", "atm"}
LOCATION_SIGNALS = {"near", "of", "from", "to", "around", "by", "beside",
                    "next to", "close to"}
FIELD_SIGNALS = {"amenity", "available", "present", "attribute", "type"}
DIRECTION_SIGNALS = {"north", "south", "east", "west", "northeast",
                     "northwest", "southeast", "southwest",
                     "n", "s", "e", "w", "ne", "nw", "se", "sw"}

# ── Template mapping ──────────────────────────────────────────────────────
# Each entry: (macro_template, concept_transformation, metric_produced)
# Sourced from docs/plans/GEO_SPATIAL_AGENT_CELERY_PLAN_V3.md §1.5 and §3.5
TEMPLATE_MAP = {
    "amenities_dataset": (
        "PLACE-ATTRIBUTE-QUERY (#8)",
        "OBJECT → POI details → amenity attribute → MEASURE",
        "Place amenity type (e.g. studio, fast_food)",
    ),
    "nearest_amenity": (
        "GEOCODE-BATCH-COMPARE (#4)",
        "OBJECT(anchor) + SUB_COND(amenity_type) → SUPPORT(nearest) → MEASURE",
        "Nearest entity of a given type around an anchor",
    ),
    "amenities_around": (
        "FILTER-AGGREGATE-MEASURE (#1)",
        "OBJECT(anchor) + SUB_COND(radius) → SUPPORT(within_radius) → MEASURE",
        "List of amenities within a radius",
    ),
    "amenities-around-specific": (
        "FILTER-AGGREGATE-MEASURE (#1)",
        "SUB_COND(amenity_type + radius) → SUPPORT(within_radius) → MEASURE",
        "List of specific amenity type within a radius",
    ),
    "adjacent": (
        "PLACE-ATTRIBUTE-QUERY (#8)",
        "OBJECT(anchor) + SUB_COND(amenity_type) → SUPPORT(adjacency) → MEASURE",
        "Adjacent entity of a given type",
    ),
    "compare-closer": (
        "GEOCODE-BATCH-COMPARE (#4)",
        "LOCATIONs → coordinates → SUPPORT(distance) × 2 → MEASURE(argmin)",
        "Which candidate is closest to the anchor",
    ),
    "direction_nearest": (
        "LOCATION-BEARING-CLASSIFY (#5)",
        "SUB_COND(direction) + SUPPORT(nearest in direction) → MEASURE",
        "Nearest entity in a cardinal direction from anchor",
    ),
    "distance": (
        "OBJECT-FIELD-MEASURE (#2)",
        "OBJECT(a) + OBJECT(b) → FIELD(haversine distance) → MEASURE",
        "Distance in meters between two entities",
    ),
    "intersection": (
        "GEOCODE-BATCH-COMPARE (#4)",
        "SUPPORT(geocode road intersection) + SUPPORT(nearest) → MEASURE",
        "Nearest entity to a road intersection point",
    ),
}

# MapQA dataset regions
_DATASET_REGIONS = ["california_full", "illinois_test"]


class Command(BaseCommand):
    help = "Train the MapQA TF-IDF parser and serialize model artifacts."

    def add_arguments(self, parser):
        parser.add_argument(
            "--csv-path",
            default=None,
            help="Path to mapqa_template_mapping.csv "
                 "(default: {MAPQA_PARSER_DATA_DIR}/training_data/)",
        )
        parser.add_argument(
            "--amenities-path",
            default=None,
            help="Path to amenities.csv (default: auto-detect from raw/)",
        )
        parser.add_argument(
            "--no-concept-models",
            action="store_true",
            default=False,
            help="Skip training concept extractor and role assigner "
                 "(template classifier only).",
        )

    def handle(self, *args, **opts):
        data_dir = Path(settings.MAPQA_PARSER_DATA_DIR)
        training_dir = data_dir / "training_data"
        training_dir.mkdir(parents=True, exist_ok=True)
        csv_path = Path(opts["csv_path"]) if opts["csv_path"] else (
            training_dir / "mapqa_template_mapping.csv"
        )
        artifacts_dir = data_dir / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        # Auto-generate training CSV from raw dataset if missing
        if not csv_path.exists():
            self.stdout.write(
                f"Training CSV not found at {csv_path} — "
                f"attempting to generate from raw dataset..."
            )
            raw_dir = data_dir / "raw" / "MapQA-dataset-main" / "llm"
            if not raw_dir.exists():
                self.stderr.write(self.style.ERROR(
                    f"Raw MapQA dataset not found at {raw_dir}. "
                    f"Mount the dataset via MAPQA_DATASET_DIR env var."
                ))
                return
            self._generate_training_csv(raw_dir, csv_path)
            self.stdout.write(f"Generated {csv_path}")

        # Copy bundled natural_language_qa_pairs.csv if not present
        nl_csv = training_dir / "natural_language_qa_pairs.csv"
        if not nl_csv.exists():
            bundled_nl = Path(__file__).resolve().parent.parent.parent / "data" / "natural_language_qa_pairs.csv"
            if bundled_nl.exists():
                shutil.copy2(bundled_nl, nl_csv)
                self.stdout.write(f"Copied bundled {bundled_nl.name} to {nl_csv}")

        self.stdout.write(f"Loading training data from {csv_path}...")
        rows = self._load_csv(csv_path)
        self.stdout.write(f"  {len(rows)} rows loaded")

        # Load supplementary natural-language QA pairs (cuisine descriptors,
        # colloquial amenity names) to improve concept extractor coverage
        # for phrases not in the OSM amenity vocabulary.
        # ([SPATIAL_AGENT:§3.2] — semantic domain vs spatial domain grounding)
        nl_csv = data_dir / "training_data" / "natural_language_qa_pairs.csv"
        if nl_csv.exists():
            nl_rows = self._load_csv(nl_csv)
            self.stdout.write(f"  Natural-language augmentation: {len(nl_rows)} rows")
            # Add to training set (Region="augmented" → treated as train)
            rows.extend(nl_rows)

        # Split: California + augmented = train, Illinois = zero-shot test
        train_rows = [r for r in rows if r["Region"] in ("california_full", "augmented")]
        test_rows = [r for r in rows if r["Region"] == "illinois_test"]
        self.stdout.write(f"  Train (CA + augmented): {len(train_rows)}")
        self.stdout.write(f"  Illinois test:          {len(test_rows)}")

        train_questions = [r["MapQA question"] for r in train_rows]
        train_templates = [r["Macro-template"] for r in train_rows]
        test_questions = [r["MapQA question"] for r in test_rows]
        test_templates = [r["Macro-template"] for r in test_rows]

        # ── Stage 1: TF-IDF + MultinomialNB (template classifier) ──────────
        self.stdout.write("\n=== Stage 1: Template classifier ===")
        # Fit vectorizer on TRAIN ONLY (fixes §4.3 leakage caveat)
        vectorizer = TfidfVectorizer(
            ngram_range=(1, 2),
            min_df=2,
            sublinear_tf=True,
            lowercase=True,
        )
        X_train = vectorizer.fit_transform(train_questions)
        X_test = vectorizer.transform(test_questions)

        label_encoder = LabelEncoder()
        y_train = label_encoder.fit_transform(train_templates)
        y_test = label_encoder.transform(test_templates)

        classifier = MultinomialNB(alpha=0.1)
        classifier.fit(X_train, y_train)

        train_acc = classifier.score(X_train, y_train)
        test_acc = classifier.score(X_test, y_test)
        self.stdout.write(f"  Train accuracy: {train_acc:.4f}")
        self.stdout.write(f"  Test  accuracy: {test_acc:.4f}")

        y_pred = classifier.predict(X_test)
        report = classification_report(
            y_test, y_pred,
            target_names=label_encoder.classes_,
            output_dict=True,
            zero_division=0,
        )
        self.stdout.write(f"  Test F1-macro: {report['macro avg']['f1-score']:.4f}")
        misclassified = int((y_pred != y_test).sum())
        self.stdout.write(f"  Misclassified: {misclassified}/{len(y_test)}")

        # ── Stage 2: Concept extractor (multi-label) ────────────────────────
        concept_extractor = None
        concept_report = {}
        role_assigner = None
        role_encoder = None
        role_report = {}

        if not opts["no_concept_models"]:
            self.stdout.write("\n=== Stage 2: Concept extractor ===")
            concept_labels_train = self._derive_concept_labels(train_rows)
            concept_labels_test = self._derive_concept_labels(test_rows)

            concept_extractor = OneVsRestClassifier(
                LogisticRegression(
                    class_weight="balanced",
                    solver="liblinear",
                    penalty="l2",
                    C=1.0,
                )
            )
            concept_extractor.fit(X_train, concept_labels_train)

            concept_pred = concept_extractor.predict(X_test)
            concept_report = classification_report(
                np.array(concept_labels_test),
                concept_pred,
                target_names=CONCEPT_TYPES,
                output_dict=True,
                zero_division=0,
            )
            self.stdout.write(
                f"  Concept F1-macro: {concept_report['macro avg']['f1-score']:.4f}"
            )

            # ── Stage 3: Role assigner (multi-class) ────────────────────────
            self.stdout.write("\n=== Stage 3: Role assigner ===")
            role_questions, role_labels = self._build_role_training_data(
                train_rows, concept_labels_train
            )
            X_roles = vectorizer.transform(role_questions)
            role_encoder = LabelEncoder()
            y_roles = role_encoder.fit_transform(role_labels)

            role_assigner = LogisticRegression(
                class_weight="balanced",
                solver="liblinear",
                penalty="l2",
                C=1.0,
            )
            role_assigner.fit(X_roles, y_roles)

            # Evaluate role assigner on test
            role_questions_test, role_labels_test = self._build_role_training_data(
                test_rows, concept_labels_test
            )
            X_roles_test = vectorizer.transform(role_questions_test)
            y_roles_test = role_encoder.transform(role_labels_test)
            role_pred = role_assigner.predict(X_roles_test)
            role_report = classification_report(
                y_roles_test,
                role_pred,
                target_names=role_encoder.classes_,
                output_dict=True,
                zero_division=0,
            )
            self.stdout.write(
                f"  Role F1-macro: {role_report['macro avg']['f1-score']:.4f}"
            )

        # ── Amenity vocabulary ──────────────────────────────────────────────
        amenity_vocab = self._load_amenity_vocab(
            opts["amenities_path"], data_dir
        )
        self.stdout.write(f"\nAmenity vocabulary: {len(amenity_vocab)} values")

        # ── Serialize artifacts ─────────────────────────────────────────────
        self.stdout.write(f"\nSerializing artifacts to {artifacts_dir}/")
        self._save(artifacts_dir / "vectorizer.pkl", vectorizer)
        self._save(artifacts_dir / "template_classifier.pkl", classifier)
        self._save(artifacts_dir / "label_encoder.pkl", label_encoder)
        self._save_json(artifacts_dir / "template_specs.json", TEMPLATE_SPECS)
        self._save_json(artifacts_dir / "amenity_vocab.json", amenity_vocab)

        if concept_extractor is not None:
            self._save(artifacts_dir / "concept_extractor.pkl", concept_extractor)
        if role_assigner is not None:
            self._save(artifacts_dir / "role_assigner.pkl", role_assigner)
            self._save(artifacts_dir / "role_encoder.pkl", role_encoder)

        # Metrics
        metrics = {
            "train_accuracy": round(train_acc, 4),
            "test_accuracy": round(test_acc, 4),
            "test_f1_macro": round(report["macro avg"]["f1-score"], 4),
            "test_misclassified": misclassified,
            "train_samples": len(train_rows),
            "test_samples": len(test_rows),
            "per_class": {
                label_encoder.classes_[i]: {
                    "precision": round(report[label_encoder.classes_[i]]["precision"], 4),
                    "recall": round(report[label_encoder.classes_[i]]["recall"], 4),
                    "f1": round(report[label_encoder.classes_[i]]["f1-score"], 4),
                    "n": int(report[label_encoder.classes_[i]]["support"]),
                }
                for i in range(len(label_encoder.classes_))
            },
            "concept_f1_macro": round(
                concept_report.get("macro avg", {}).get("f1-score", 0), 4
            ) if concept_report else None,
            "role_f1_macro": round(
                role_report.get("macro avg", {}).get("f1-score", 0), 4
            ) if role_report else None,
            "amenity_vocab_size": len(amenity_vocab),
        }
        self._save_json(artifacts_dir / "metrics.json", metrics)

        self.stdout.write(self.style.SUCCESS(
            f"\nDone. Artifacts saved to {artifacts_dir}/\n"
            f"  Template accuracy: {test_acc:.4f} "
            f"({misclassified}/{len(test_rows)} errors)\n"
            f"  F1-macro: {report['macro avg']['f1-score']:.4f}"
        ))

    # ── Data loading ────────────────────────────────────────────────────────

    @staticmethod
    def _load_csv(csv_path: Path) -> list:
        with open(csv_path, "r", encoding="utf-8", newline="") as f:
            return list(csv.DictReader(f))

    # ── Training CSV generation (from raw MapQA dataset) ───────────────────

    @classmethod
    def _generate_training_csv(cls, raw_llm_dir: Path, output_path: Path) -> None:
        """Generate mapqa_template_mapping.csv from the raw MapQA dataset.

        Reads all QA CSV files from california_full + illinois_test splits,
        maps each question to its Spatial-Agent macro-template, and writes
        the result. Env-driven and self-contained for Docker startup.
        """
        rows = []
        unmapped_types = set()

        for region in _DATASET_REGIONS:
            qa_dir = raw_llm_dir / region / "question-answer"
            if not qa_dir.exists():
                logger.warning("MapQA %s/question-answer not found, skipping", region)
                continue
            for csv_file in sorted(qa_dir.glob("*.csv")):
                question_type = csv_file.stem
                if question_type in TEMPLATE_MAP:
                    macro_template, concept_transform, metric = TEMPLATE_MAP[question_type]
                else:
                    alt_key = question_type.replace("_dataset", "")
                    if alt_key in TEMPLATE_MAP:
                        macro_template, concept_transform, metric = TEMPLATE_MAP[alt_key]
                    else:
                        unmapped_types.add(question_type)
                        macro_template = "UNKNOWN"
                        concept_transform = "UNKNOWN"
                        metric = "UNKNOWN"

                questions = cls._read_dataset_questions(csv_file)
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

        if unmapped_types:
            logger.warning("Unmapped MapQA question types: %s", unmapped_types)

        fieldnames = [
            "Macro-template", "Concept transformation", "What metric it produces",
            "MapQA question", "Question type", "Region", "Answer",
        ]
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

        self_stdout = f"Wrote {len(rows)} rows to {output_path}"
        logger.info(self_stdout)

    @staticmethod
    def _read_dataset_questions(csv_path: Path) -> list:
        """Read a MapQA dataset CSV and return list of (question, answer) tuples.

        Handles both header formats:
          - ID,Question,Answer  (most files)
          - Question,Answer     (distance_dataset.csv — malformed header)
        """
        questions = []
        with open(csv_path, "r", encoding="utf-8", newline="") as f:
            reader = csv.reader(f)
            header = next(reader)
            if len(header) == 2 and header[0].strip() == "Question":
                for row in reader:
                    if len(row) >= 3:
                        questions.append((row[1].strip(), row[2].strip()))
                    elif len(row) == 2:
                        questions.append((row[0].strip(), row[1].strip()))
            else:
                for row in reader:
                    if len(row) >= 3:
                        questions.append((row[1].strip(), row[2].strip()))
                    elif len(row) == 2:
                        questions.append((row[0].strip(), row[1].strip()))
        return questions

    @staticmethod
    def _load_amenity_vocab(amenities_path, data_dir: Path) -> list:
        """Load amenity vocabulary from amenities.csv.

        Search order:
          1. Explicit --amenities-path argument
          2. Canonical copy in training_data/ (works inside Docker)
          3. Raw dataset symlink (works on host, may be broken in Docker)
        """
        if amenities_path:
            p = Path(amenities_path)
        else:
            # Try canonical copy first (Docker-safe)
            p = data_dir / "training_data" / "amenities.csv"
            if not p.exists():
                # Fall back to raw dataset symlink (host-only)
                p = data_dir / "raw" / "MapQA-dataset-main" / "llm" / "california_full" / "amenities.csv"
        if not p.exists():
            logger.warning("amenities.csv not found at %s, using fallback vocab", p)
            return sorted(OBJECT_SIGNALS)
        vocab = []
        with open(p, "r", encoding="utf-8") as f:
            # Format: "amenity" header, then one value per line (quoted)
            for line in f:
                val = line.strip().strip('"').strip()
                if val and val.lower() != "amenity" and val.lower() != "null":
                    vocab.append(val)
        # Also include fallback signals
        for s in OBJECT_SIGNALS:
            if s not in vocab:
                vocab.append(s)
        return sorted(set(vocab))

    # ── Concept label derivation ────────────────────────────────────────────

    @staticmethod
    def _derive_concept_labels(rows: list) -> list:
        """Derive multi-label concept annotations from question text.

        For each question, determine which concept types from C are present
        based on lexical signals. This is a heuristic approximation of the
        ground-truth annotations — sufficient for training the concept
        extractor.
        """
        labels = []
        for row in rows:
            q_lower = row["MapQA question"].lower()
            template = row["Macro-template"]
            concepts = set()

            # AMOUNT: radius or distance
            if any(p.search(q_lower) for p in AMOUNT_PATTERNS):
                concepts.add("AMOUNT")
            if "how far" in q_lower or "distance" in q_lower:
                concepts.add("AMOUNT")

            # OBJECT: amenity type
            for obj_sig in OBJECT_SIGNALS:
                if obj_sig in q_lower:
                    concepts.add("OBJECT")
                    break

            # LOCATION: entity names (proper nouns near prepositions)
            if any(sig in q_lower for sig in LOCATION_SIGNALS):
                concepts.add("LOCATION")
            # Compare-closer and distance always have 2+ locations
            if "closer" in q_lower or "how far" in q_lower:
                concepts.add("LOCATION")

            # FIELD: attribute queries
            if any(sig in q_lower for sig in FIELD_SIGNALS):
                concepts.add("FIELD")

            # Template-specific guarantees
            if template == "LOCATION-BEARING-CLASSIFY (#5)":
                concepts.add("LOCATION")
                concepts.add("AMOUNT")  # bearing angle
            if template == "OBJECT-FIELD-MEASURE (#2)":
                concepts.add("LOCATION")
                concepts.add("AMOUNT")  # distance

            # Ensure at least one concept
            if not concepts:
                concepts.add("LOCATION")

            # Convert to binary vector matching CONCEPT_TYPES order
            label_vec = [1 if ct in concepts else 0 for ct in CONCEPT_TYPES]
            labels.append(label_vec)
        return labels

    @staticmethod
    def _build_role_training_data(rows: list, concept_labels: list) -> tuple:
        """Build (question+concept_marker, role) pairs for role training."""
        role_questions = []
        role_labels = []
        for i, row in enumerate(rows):
            template = row["Macro-template"]
            concepts = concept_labels[i]
            role_map = TEMPLATE_ROLE_MAP.get(template, {})
            for j, present in enumerate(concepts):
                if present:
                    ctype = CONCEPT_TYPES[j]
                    role = role_map.get(ctype, "SUPPORT")
                    combined = f"{row['MapQA question']} [CONCEPT:{ctype}]"
                    role_questions.append(combined)
                    role_labels.append(role)
        return role_questions, role_labels

    # ── Serialization helpers ──────────────────────────────────────────────

    @staticmethod
    def _save(path: Path, obj):
        with open(path, "wb") as f:
            pickle.dump(obj, f)

    @staticmethod
    def _save_json(path: Path, obj):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, ensure_ascii=False)
