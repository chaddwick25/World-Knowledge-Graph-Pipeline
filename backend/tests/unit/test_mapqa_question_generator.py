"""Unit tests for the MapQA self-supervised question generator.

Tests generator invariants ([MAPQA_TEMPLATE_COVERAGE_EXPANSION_PLAN.md] §8):
  - #1 count answer equals an independent ST_DWithin recount
  - #2 distance answer equals a haversine recompute (±1%)
  - #5 bearing answer matches bearing→cardinal recompute
  - No empty answers (regression vs the raw dataset's empty-answer rows)
  - Determinism (same seed + same snapshot → identical questions)
  - Idempotency (re-running does not grow the CSV)
  - Name sanitization (quotes/commas stripped, apostrophes kept)
  - Train split consumes self_supervised rows; loader tolerates legacy CSVs

Needs the Docker test databases (vector + postgis extensions created per
04-testing.md §5.2). A controlled set of OsmEntity rows is seeded into the
test vectors DB so the tests are hermetic (no dependency on production data).

Run inside Docker:
  docker compose exec backend python -m pytest tests/unit/test_mapqa_question_generator.py -v --reuse-db
"""

import csv
import os
import random
import tempfile
from pathlib import Path

import pytest

COUNTRY = "BZ"
SNAPSHOT = "2025_12_31"

# Controlled entity set around Belize City — clustered so radius/bearing/
# adjacency ground truth is computable, plus far-flung entities for the
# 0.5–200 km distance gate.
ENTITY_ROWS = [
    # (name, amenity, lat, lon, osm_id)
    ("Belize Bank", "bank", 17.5045, -88.1856, 100001),
    ("Cafe de Belice", "cafe", 17.5047, -88.1860, 100002),
    ("Belize City Restaurant", "restaurant", 17.5060, -88.1865, 100003),
    ("St. John's Cathedral", "place_of_worship", 17.4931, -88.1877, 100004),
    ("Chicken Drop Bar", "bar", 17.5000, -88.1900, 100005),
    ("Maya Island Air", "airport", 17.5071, -88.1921, 100006),
    ("San Pedro Pharmacy", "pharmacy", 17.9242, -87.9719, 100007),
    ("Caye Caulker Guesthouse", "hotel", 17.7400, -88.0290, 100008),
    ("Placencia Airport", "airport", 16.5370, -88.3610, 100009),
    ("Dangriga Clinic", "clinic", 16.9697, -88.2163, 100010),
]


@pytest.fixture(scope="module")
def generator():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "backend.settings")
    import django
    django.setup()
    from semantic_search.services.mapqa_question_generator import (
        MapQAQuestionGenerator,
    )
    return MapQAQuestionGenerator(seed=42)


@pytest.fixture(scope="module")
def seed_entities(django_db_setup, django_db_blocker):
    """Seed the test vectors DB with a controlled BZ entity set.

    Depends on ``django_db_setup`` so the test databases exist and the
    ``vectors``/``default`` connections are redirected BEFORE any write —
    without it, module-scoped fixtures run before test-DB setup and would
    write to the REAL databases (a real data-loss hazard, see the BZ restore
    note in AGENTS.md). Idempotent: prior rows for (BZ, SNAPSHOT) are removed
    first so re-runs (--reuse-db) stay deterministic. Also creates a
    CountryPipelineProfile so the generation command can resolve --country BZ
    against the test DB.
    """
    from django.contrib.gis.geos import Point
    from worldkg_nca.models import OsmEntity

    with django_db_blocker.unblock():
        OsmEntity.objects.using("vectors").filter(
            country_code=COUNTRY, snapshot_id=SNAPSHOT
        ).delete()
        for name, amenity, lat, lon, osm_id in ENTITY_ROWS:
            OsmEntity.objects.using("vectors").create(
                osm_type="node",
                osm_id=osm_id,
                snapshot_id=SNAPSHOT,
                country_code=COUNTRY,
                tags={"name": name, "amenity": amenity},
                geom=Point(lon, lat, srid=4326),
            )

        from core.models.country_profile import CountryPipelineProfile
        CountryPipelineProfile.objects.using("default").filter(
            iso2=COUNTRY
        ).delete()
        CountryPipelineProfile.objects.using("default").create(
            iso2=COUNTRY,
            iso3="BLZ",
            canonical_name="Belize",
            canonical_slug="belize",
            embedding_slug="belize",
            embedding_root_path="belize",
        )
    yield


@pytest.mark.django_db(transaction=False, databases=["default", "vectors"])
class TestGroundTruth:
    """Ground-truth answers must match independent recomputations."""

    def test_radius_count_ground_truth(self, generator, seed_entities):
        """#1 answer equals an independent ST_DWithin recount."""
        from django.db import connections
        rows = generator._sample_template_1(
            COUNTRY, SNAPSHOT, budget=5, rng=random.Random(1)
        )
        assert len(rows) > 0, "no #1 rows generated for seeded BZ"
        for row in rows:
            meta = row["meta"]
            with connections["vectors"].cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*) FROM semantic_search_osmentity
                    WHERE snapshot_id = %s AND country_code = %s
                      AND tags->>'amenity' = %s
                      AND geom IS NOT NULL
                      AND ST_DWithin(geom::geography,
                                     ST_MakePoint(%s, %s)::geography, %s)
                    """,
                    [SNAPSHOT, COUNTRY, meta["amenity"],
                     meta["anchor_lon"], meta["anchor_lat"],
                     float(meta["radius_m"])],
                )
                recount = cur.fetchone()[0]
            assert int(row["answer"]) == recount, (
                f"count mismatch for {row['question']!r}: "
                f"generated {row['answer']}, recount {recount}"
            )
            assert int(row["answer"]) >= 1

    def test_distance_ground_truth(self, generator, seed_entities):
        """#2 answer equals haversine recompute (±1%)."""
        rows = generator._sample_template_2(
            COUNTRY, SNAPSHOT, budget=5, rng=random.Random(2)
        )
        assert len(rows) > 0, "no #2 rows generated for seeded BZ"
        for row in rows:
            meta = row["meta"]
            recompute = generator.haversine_km(
                meta["a_lat"], meta["a_lon"], meta["b_lat"], meta["b_lon"]
            )
            assert abs(float(row["answer"]) - recompute) <= 0.01 * recompute, (
                f"distance mismatch for {row['question']!r}: "
                f"generated {row['answer']}, recompute {recompute}"
            )

    def test_bearing_cardinal(self, generator, seed_entities):
        """#5 direction matches bearing→cardinal recompute for known pairs."""
        # Known synthetic pairs (bearing 0° = north, 90° = east, ...)
        known = [
            (0.0, 0.0, 0.001, 0.0, "north"),
            (0.0, 0.0, 0.0, 0.001, "east"),
            (0.0, 0.0, -0.001, 0.0, "south"),
            (0.0, 0.0, 0.0, -0.001, "west"),
        ]
        for lat1, lon1, lat2, lon2, expected in known:
            theta = generator.bearing_degrees(lat1, lon1, lat2, lon2)
            assert generator.cardinal_from_bearing(theta) == expected

        # Generated rows must match their meta recomputation
        rows = generator._sample_template_5a(
            COUNTRY, SNAPSHOT, budget=5, rng=random.Random(3)
        )
        assert len(rows) > 0, "no #5a rows generated for seeded BZ"
        for row in rows:
            meta = row["meta"]
            theta = generator.bearing_degrees(
                meta["x_lat"], meta["x_lon"], meta["y_lat"], meta["y_lon"]
            )
            assert row["answer"] == generator.cardinal_from_bearing(theta), (
                f"cardinal mismatch for {row['question']!r}"
            )
            assert row["answer"] in (
                "north", "northeast", "east", "southeast",
                "south", "southwest", "west", "northwest",
            )

    def test_no_empty_answers(self, generator, seed_entities):
        """Generator never emits an empty answer (raw-dataset bug regression)."""
        rows = generator.generate(
            country_code=COUNTRY, snapshot_id=SNAPSHOT, per_class=5
        )
        assert rows, "no rows generated for seeded BZ"
        for row in rows:
            assert row["Answer"] not in (None, "", "None"), (
                f"empty answer for {row['MapQA question']!r}"
            )
            # Every row has the extended-schema columns populated
            assert row["Source"] == "self_supervised"
            assert row["Snapshot_date"] == SNAPSHOT
            assert row["Country_code"] == COUNTRY
            assert row["Ground_truth_table"] == "semantic_search_osmentity"
            assert row["Region"] == "self_supervised"


@pytest.mark.django_db(transaction=False, databases=["default", "vectors"])
class TestDeterminismAndIdempotency:
    def test_determinism(self, generator, seed_entities):
        """Same seed + same snapshot → identical questions."""
        r1 = generator.generate(country_code=COUNTRY, snapshot_id=SNAPSHOT,
                                per_class=5)
        r2 = generator.generate(country_code=COUNTRY, snapshot_id=SNAPSHOT,
                                per_class=5)
        q1 = [(r["MapQA question"], r["Answer"]) for r in r1]
        q2 = [(r["MapQA question"], r["Answer"]) for r in r2]
        assert q1 == q2
        assert len(r1) == len(r2)

    def test_idempotency(self, seed_entities):
        """Re-running for the same (country, snapshot, template) does not
        grow the CSV (the command replaces its own prior batch)."""
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "backend.settings")
        import django
        django.setup()
        from django.core.management import call_command

        with tempfile.TemporaryDirectory() as tmp:
            out = str(Path(tmp) / "nl_qa.csv")
            kwargs = dict(
                country=COUNTRY, snapshot=SNAPSHOT, per_class=3,
                paraphrase_per_seed=0, output=out, verbosity=0,
            )
            call_command("generate_mapqa_training_data", **kwargs)
            first = self._read_rows(out)
            assert len(first) > 0
            call_command("generate_mapqa_training_data", **kwargs)
            second = self._read_rows(out)
            assert len(second) == len(first), (
                f"CSV grew on re-run: {len(first)} → {len(second)}"
            )
            # Content is identical (deterministic regeneration)
            assert sorted(r["MapQA question"] for r in second) == sorted(
                r["MapQA question"] for r in first
            )
            # Hand-curated rows (Source != self_supervised) are preserved
            self._write_legacy_rows(out)
            call_command("generate_mapqa_training_data", **kwargs)
            third = self._read_rows(out)
            augmented = [r for r in third if r.get("Source") == "hand-curated"]
            assert len(augmented) == 2, "hand-curated rows must survive re-runs"
            self_sup = [r for r in third if r.get("Source") == "self_supervised"]
            assert len(self_sup) == len(first)

    @staticmethod
    def _read_rows(path):
        with open(path, "r", encoding="utf-8", newline="") as f:
            return list(csv.DictReader(f))

    @staticmethod
    def _write_legacy_rows(path):
        """Append legacy-schema (7-column) hand-curated rows to the CSV."""
        legacy_header = (
            "Macro-template,Concept transformation,What metric it produces,"
            "MapQA question,Question type,Region,Answer\n"
        )
        legacy_rows = [
            "PLACE-ATTRIBUTE-QUERY (#8),transform,metric,italian food near a bus station,natural_language,augmented,restaurant\n",
            "FILTER-AGGREGATE-MEASURE (#1),transform,metric,cafes near a school,natural_language,augmented,restaurant\n",
        ]
        with open(path, "a", encoding="utf-8", newline="") as f:
            f.write(legacy_header)
            f.writelines(legacy_rows)


class TestNameSanitization:
    def test_strips_quotes_and_commas(self):
        from semantic_search.services.mapqa_question_generator import (
            sanitize_name,
        )
        assert sanitize_name('Foo, "Bar" Baz') == "Foo Bar Baz"
        assert sanitize_name('  "Quoted Name"  ') == "Quoted Name"

    def test_keeps_apostrophes(self):
        """Apostrophes are CSV-safe and keep names geocode-able."""
        from semantic_search.services.mapqa_question_generator import (
            sanitize_name,
        )
        assert sanitize_name("Surfdog's Java Hot") == "Surfdog's Java Hot"

    def test_collapses_whitespace(self):
        from semantic_search.services.mapqa_question_generator import (
            sanitize_name,
        )
        assert sanitize_name("  St.  John's   Church ") == "St. John's Church"


class TestTrainSplitAndLoader:
    """train_mapqa_parser must consume self_supervised rows and tolerate
    both CSV schemas ([MAPQA_TEMPLATE_COVERAGE_EXPANSION_PLAN.md] §5)."""

    def test_split_includes_self_supervised(self):
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "backend.settings")
        import django
        django.setup()
        from semantic_search.management.commands.train_mapqa_parser import (
            Command,
        )
        rows = [
            {"Region": "california_full", "Macro-template": "X"},
            {"Region": "augmented", "Macro-template": "X"},
            {"Region": "self_supervised", "Macro-template": "X"},
            {"Region": "self_supervised_test", "Macro-template": "X"},
            {"Region": "illinois_test", "Macro-template": "X"},
        ]
        train, test, holdout = Command._split_rows(rows)
        assert len(train) == 3  # california + augmented + self_supervised
        assert len(test) == 1
        assert len(holdout) == 1
        assert holdout[0]["Region"] == "self_supervised_test"

    def test_loader_tolerates_legacy_schema(self):
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "backend.settings")
        import django
        django.setup()
        from semantic_search.management.commands.train_mapqa_parser import (
            Command,
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy.csv"
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(
                    "Macro-template,Concept transformation,What metric it "
                    "produces,MapQA question,Question type,Region,Answer\n"
                    "T1,t,m,q1,nl,california_full,a1\n"
                    "T2,t,m,q2,nl,augmented,a2\n"
                    "T3,t,m,q3,nl,self_supervised,a3\n"
                )
            rows = Command._load_csv(path)
            by_region = {r["Region"]: r for r in rows}
            assert by_region["california_full"]["Source"] == "mapqa-llm"
            assert by_region["augmented"]["Source"] == "hand-curated"
            assert by_region["self_supervised"]["Source"] == "self_supervised"
            for row in rows:
                assert "Pipeline_run_id" in row
                assert "Snapshot_date" in row
                assert "Ground_truth_table" in row
                assert "Country_code" in row
