"""Tests for QueryCorrectionService — misspelled-amenity correction
(Kuhn's Template).

Covers:
- pg_trgm similarity tier ("resturant" → "restaurant")
- fuzzystrmatch levenshtein tier (short words: "caff" → "cafe")
- Exact/identity pass and plural tolerance
- Junk rejection ("zzqxw") and open-vocabulary safety ("italian food" is
  never force-corrected)
- Executor integration: _resolve_amenity_tag returns the corrected tag
- Vocabulary cache behavior
- Extension requirements (pg_trgm + fuzzystrmatch on the vectors DB)

Test data uses the live-Docker test DBs (per conftest.py) with
country_code='BZ', snapshot '2025_12_31' and osm_ids in the 6xxxxx range,
deleted in teardown. Requires the pg_trgm and fuzzystrmatch extensions on
test_vector_db (migrations 0017/0019 create them on the live DBs; create
once on the test DBs, see AGENTS.md):

  docker compose exec postgres-vectors psql -U vector_user -d test_vector_db \
    -c "CREATE EXTENSION IF NOT EXISTS pg_trgm; CREATE EXTENSION IF NOT EXISTS fuzzystrmatch;"

Run inside Docker:
  docker compose exec backend python -m pytest tests/unit/test_query_correction_service.py -v --reuse-db
"""

import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "backend.settings")
import django

django.setup()

import pytest
from django.db import connections

from semantic_search.services.query_correction_service import (
    MIN_SCORE,
    QueryCorrectionService,
)

pytestmark = pytest.mark.django_db(
    transaction=False, databases=["default", "vectors"],
)

TEST_CC = "BZ"
SNAP = "2025_12_31"

# Own row range (6xxxxx) — distinct from test_mapqa_executor's 100001-100010.
_ENTITY_ROWS = [
    ("Correction Test Grill", "restaurant", 17.5000, -88.1800, 600001),
    ("Correction Test Beans", "cafe", 17.5001, -88.1801, 600002),
    ("Correction Test Tap", "bar", 17.5002, -88.1802, 600003),
    ("Correction Test Fuel", "fuel", 17.5003, -88.1803, 600004),
    ("Correction Test Dentistry", "dentist", 17.5004, -88.1804, 600005),
    ("Correction Test Grocer", "supermarket", 17.5005, -88.1805, 600006),
]
_SEEDED_OSM_IDS = [row[4] for row in _ENTITY_ROWS]


@pytest.fixture(scope="module")
def seed_entities(django_db_setup, django_db_blocker):
    """Seed a controlled BZ amenity set on the test vectors DB.

    Depends on ``django_db_setup`` so writes go to the TEST databases
    (module-scoped fixture without it would hit the real DBs — see the
    data-loss hazard note in AGENTS.md).
    """
    from django.contrib.gis.geos import Point
    from worldkg_nca.models import OsmEntity

    QueryCorrectionService.clear_cache()
    with django_db_blocker.unblock():
        OsmEntity.objects.using("vectors").filter(
            osm_id__in=_SEEDED_OSM_IDS
        ).delete()
        for name, amenity, lat, lon, osm_id in _ENTITY_ROWS:
            OsmEntity.objects.using("vectors").create(
                osm_type="node",
                osm_id=osm_id,
                snapshot_id=SNAP,
                country_code=TEST_CC,
                tags={"name": name, "amenity": amenity},
                geom=Point(lon, lat, srid=4326),
            )
    yield
    QueryCorrectionService.clear_cache()
    with django_db_blocker.unblock():
        OsmEntity.objects.using("vectors").filter(
            osm_id__in=_SEEDED_OSM_IDS
        ).delete()


# ── Extension requirements ──────────────────────────────────────────

class TestExtensions:
    def test_pg_trgm_and_fuzzystrmatch_enabled(self):
        """The two correction tiers require both extensions on the vectors DB."""
        with connections["vectors"].cursor() as cur:
            cur.execute(
                "SELECT extname FROM pg_extension "
                "WHERE extname IN ('pg_trgm', 'fuzzystrmatch')"
            )
            present = {row[0] for row in cur.fetchall()}
        assert "pg_trgm" in present, "pg_trgm missing on test_vector_db"
        assert "fuzzystrmatch" in present, "fuzzystrmatch missing on test_vector_db"


# ── Vocabulary ──────────────────────────────────────────────────────

class TestVocabulary:
    def test_vocabulary_contains_seeded_tags(self, seed_entities):
        vocab = QueryCorrectionService.amenity_vocabulary(SNAP, TEST_CC)
        for tag in ("restaurant", "cafe", "bar", "fuel", "dentist", "supermarket"):
            assert tag in vocab, f"seeded tag {tag!r} missing from vocabulary"

    def test_vocabulary_cached(self, seed_entities):
        QueryCorrectionService.clear_cache()
        first = QueryCorrectionService.amenity_vocabulary(SNAP, TEST_CC)
        assert QueryCorrectionService._vocab_cache.get((SNAP, TEST_CC))
        second = QueryCorrectionService.amenity_vocabulary(SNAP, TEST_CC)
        assert first == second
        QueryCorrectionService.clear_cache()
        assert not QueryCorrectionService._vocab_cache


# ── Correction tiers ────────────────────────────────────────────────

class TestCorrection:
    @pytest.mark.parametrize("phrase,expected,method", [
        # pg_trgm tier — single/double typo in a long word
        ("resturant", "restaurant", "pg_trgm"),
        ("resstaurant", "restaurant", "pg_trgm"),
        ("supermaket", "supermarket", "pg_trgm"),
        # plural tolerance (executor singularizes upstream; belt and braces)
        ("restaurants", "restaurant", "pg_trgm"),
        # fuzzystrmatch tier — short words defeat trigram overlap
        ("caff", "cafe", "fuzzystrmatch"),
        ("dentistt", "dentist", "pg_trgm"),
        # exact pass — canonical tag is its own correction
        ("restaurant", "restaurant", "exact"),
        ("bar", "bar", "exact"),
    ])
    def test_correct_amenity(self, seed_entities, phrase, expected, method):
        result = QueryCorrectionService.correct_amenity(
            phrase, country_code=TEST_CC, snapshot_id=SNAP,
        )
        assert result is not None, f"no correction for {phrase!r}"
        tag, score, got_method = result
        assert tag == expected, f"{phrase!r} → {tag!r}, expected {expected!r}"
        assert got_method == method, (
            f"{phrase!r} corrected via {got_method}, expected {method}"
        )
        assert MIN_SCORE <= score <= 1.0

    @pytest.mark.parametrize("phrase", [
        "zzqxw",       # junk — no tag is close
        "italian food",  # open-vocabulary cuisine phrase — must not be forced
        "school",      # not in the seeded vocabulary → stays None (caller falls through)
    ])
    def test_no_false_correction(self, seed_entities, phrase):
        assert QueryCorrectionService.correct_amenity(
            phrase, country_code=TEST_CC, snapshot_id=SNAP,
        ) is None

    def test_empty_phrase(self, seed_entities):
        assert QueryCorrectionService.correct_amenity("") is None
        assert QueryCorrectionService.correct_amenity(None) is None

    def test_case_insensitive(self, seed_entities):
        result = QueryCorrectionService.correct_amenity(
            "Resturant", country_code=TEST_CC, snapshot_id=SNAP,
        )
        assert result[0] == "restaurant"


# ── Executor integration (Kuhn's Template) ─────────────────────────

class TestExecutorIntegration:
    def test_resolve_amenity_tag_corrects_misspelling(self, seed_entities):
        """The executor's amenity resolution must return the corrected tag,
        not None, for a misspelled phrase (the Kuhn's Template break case)."""
        from semantic_search.services.query_executor_service import (
            QueryExecutorService,
        )
        tag = QueryExecutorService._resolve_amenity_tag(
            "resturant", country_code=TEST_CC, snapshot_date=SNAP,
        )
        assert tag == "restaurant"

    def test_resolve_amenity_tag_still_none_for_junk(self, seed_entities):
        """Junk phrases must still fall through to the caller's fallbacks."""
        from semantic_search.services.query_executor_service import (
            QueryExecutorService,
        )
        tag = QueryExecutorService._resolve_amenity_tag(
            "zzqxw", country_code=TEST_CC, snapshot_date=SNAP,
        )
        assert tag is None
