"""Unit tests for ISSUE-ann-gv-tags-flip.

Covers the code-side scope items that need no database:

- the ``+ 0`` → indexable-ANN flip: ``_distance_annotation_sql`` returns
  the indexable form by default and the `+ 0` exact-scan form under
  ``exact=true``;
- the post-scoring diversity dedupe: grouping by (normalized name,
  geohash P5 cell), keep-best by final_score, unnamed/geom-less results
  never grouped;
- the ``--verify`` self-probe mode on ``create_osmentity_vector_indexes``:
  scoped leaf discovery, found/missing counting, and ``--strict``
  exit-below-threshold.

No DB access — ``connections['vectors']`` is stubbed the same way as
``test_per_leaf_index_lifecycle.py``.
"""

from io import StringIO

import geohash2
import pytest

from django.core.management import call_command
from django.core.management.base import CommandError

from worldkg_nca.views.search import (
    _diversity_dedupe,
    _diversity_key,
    _distance_annotation_sql,
    _normalize_result_name,
)


# --------------------------------------------------------------------------- #
# Connection stubs (same pattern as test_per_leaf_index_lifecycle.py)
# --------------------------------------------------------------------------- #

class StubCursor:
    """Captures executed SQL and returns canned result sets FIFO."""

    def __init__(self, results=None):
        self.executed = []
        self.results = list(results or [])
        self._next = 0

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        if self._next < len(self.results):
            rows = self.results[self._next]
            self._next += 1
            return rows
        return []

    def fetchone(self):
        if self._next < len(self.results):
            row = self.results[self._next]
            self._next += 1
            return row
        return (False,)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class StubConnection:
    def __init__(self, results=None):
        self._cursor = StubCursor(results)
        self._autocommit = False

    def cursor(self):
        return self._cursor

    def get_autocommit(self):
        return self._autocommit

    def set_autocommit(self, value):
        self._autocommit = value


class StubConnections:
    def __init__(self, conn):
        self._conn = conn

    def __getitem__(self, alias):
        assert alias == "vectors", f"unexpected DB alias {alias!r}"
        return self._conn


@pytest.fixture
def stub_vectors(monkeypatch):
    """Replace ``connections['vectors']`` with a stub for the vectors DB."""
    conn = StubConnection()
    stub = StubConnections(conn)
    import worldkg_nca.management.commands.create_osmentity_vector_indexes as create_cmd
    monkeypatch.setattr(create_cmd, "connections", stub)
    return conn


# --------------------------------------------------------------------------- #
# The + 0 → indexable ANN flip
# --------------------------------------------------------------------------- #

class TestDistanceAnnotationSql:

    @pytest.mark.unit
    def test_default_is_indexable(self):
        sql = _distance_annotation_sql(False)
        assert sql == "gv_tags_embedding <=> %s::vector"
        assert "+ 0" not in sql

    @pytest.mark.unit
    def test_exact_restores_exact_scan_form(self):
        sql = _distance_annotation_sql(True)
        assert sql == "(gv_tags_embedding <=> %s::vector) + 0"


# --------------------------------------------------------------------------- #
# Diversity dedupe helpers
# --------------------------------------------------------------------------- #

def _result(name, lat, lon, score, osm_id=1):
    return {
        "osm_type": "node",
        "osm_id": osm_id,
        "tags": {"name": name} if name is not None else {},
        "wkg_class": "wkgs:Amenity",
        "geom": {"lat": lat, "lon": lon} if lat is not None else None,
        "scores": {"final_score": score},
    }


class TestNormalizeResultName:

    @pytest.mark.unit
    def test_collapses_case_punct_whitespace(self):
        assert _normalize_result_name("Joe's  CAFÉ!") == "joes cafe"
        assert _normalize_result_name("  St.   Mary  ") == "st mary"

    @pytest.mark.unit
    def test_empty_and_none(self):
        assert _normalize_result_name(None) == ""
        assert _normalize_result_name("") == ""


class TestDiversityKey:

    @pytest.mark.unit
    def test_unnamed_is_never_grouped(self):
        assert _diversity_key(_result(None, 53.35, -6.26, 1.0)) is None

    @pytest.mark.unit
    def test_geomless_is_never_grouped(self):
        assert _diversity_key(_result("Cafe", None, None, 1.0)) is None
        assert _diversity_key(
            {"tags": {"name": "Cafe"}, "geom": None, "scores": {"final_score": 1.0}}
        ) is None


# Fixture coords derived from a real P5 cell so the grouping assertions
# are boundary-safe: decode the cell containing a Dublin anchor, then
# place three points near its centre and the fourth ~33 km away.
_CENTER = tuple(
    float(v) for v in geohash2.decode(geohash2.encode(53.3498, -6.2603, precision=5))
)
_A = (_CENTER[0], _CENTER[1])
_B = (_CENTER[0] + 0.005, _CENTER[1] + 0.005)
_C = (_CENTER[0] - 0.005, _CENTER[1] - 0.005)
_D = (_CENTER[0] + 0.3, _CENTER[1])


class TestDiversityDedupe:

    @pytest.mark.unit
    def test_same_name_same_cell_keeps_best(self):
        assert geohash2.encode(*_A, precision=5) == geohash2.encode(*_B, precision=5)
        assert geohash2.encode(*_A, precision=5) == geohash2.encode(*_C, precision=5)
        assert geohash2.encode(*_A, precision=5) != geohash2.encode(*_D, precision=5)

        results = [
            _result("Vaquería", *_A, 3.0, osm_id=1),
            _result("vaqueria", *_B, 5.0, osm_id=2),
            _result("Vaquería", *_C, 4.0, osm_id=3),
            _result("Vaquería", *_D, 2.0, osm_id=4),
        ]
        deduped = _diversity_dedupe(results)
        assert len(deduped) == 2
        # Best-in-cell wins; original sort order is preserved.
        assert [r["osm_id"] for r in deduped] == [2, 4]

    @pytest.mark.unit
    def test_unnamed_and_geomless_always_survive(self):
        results = [
            _result("Cafe", *_A, 5.0, osm_id=1),
            _result("Cafe", *_B, 4.0, osm_id=2),
            _result(None, *_A, 3.0, osm_id=3),
            _result("Cafe", None, None, 1.0, osm_id=4),
        ]
        deduped = _diversity_dedupe(results)
        assert [r["osm_id"] for r in deduped] == [1, 3, 4]

    @pytest.mark.unit
    def test_no_groups_returns_input(self):
        results = [
            _result("Alpha", *_A, 3.0, osm_id=1),
            _result("Beta", *_B, 2.0, osm_id=2),
        ]
        assert _diversity_dedupe(results) == results


# --------------------------------------------------------------------------- #
# --verify self-probe mode
# --------------------------------------------------------------------------- #

class TestVerifyMode:

    @pytest.mark.unit
    def test_scoped_verify_reports_found_and_missing(self, stub_vectors):
        stub_vectors._cursor.results = [
            # _hnsw_leaf_columns: one HNSW'd column on the MX leaf
            [("embeddings_2025_12_31_mx", "gv_tags_embedding")],
            # TABLESAMPLE: two anchors
            [("node", 1, "[0.1,0.2]"), ("way", 2, "[0.3,0.4]")],
            # probe 1: reachable — anchor or a distance-0 twin on top
            (0.0,),
            # probe 2: cluster unreachable — nearest row is far away
            (0.04,),
        ]
        out, err = StringIO(), StringIO()
        call_command(
            "create_osmentity_vector_indexes",
            verify=True, verify_sample=2,
            country="mx", snapshot="2025_12_31",
            stdout=out, stderr=err,
        )
        assert "1/2 reachable" in out.getvalue()
        # Below-threshold leaf lines go to stderr, with the min distance.
        assert "way/2 d=0.0400" in err.getvalue()
        # The leaf-name WHERE must be parameterized, not interpolated.
        assert any(
            "t.relname = %s" in sql and params == ["embeddings_2025_12_31_mx"]
            for sql, params in stub_vectors._cursor.executed
        )

    @pytest.mark.unit
    def test_strict_exits_below_threshold(self, stub_vectors):
        stub_vectors._cursor.results = [
            [("embeddings_2025_12_31_mx", "gv_tags_embedding")],
            [("node", 1, "[0.1]"), ("way", 2, "[0.2]")],
            (0.0,),                 # reachable
            (0.3,),                 # missed → 1/2 = 50% < 99%
        ]
        with pytest.raises(CommandError):
            call_command(
                "create_osmentity_vector_indexes",
                verify=True, verify_sample=2, strict=True,
                country="mx", snapshot="2025_12_31",
                stdout=StringIO(),
            )

    @pytest.mark.unit
    def test_empty_sample_falls_back_to_random_order(self, stub_vectors):
        stub_vectors._cursor.results = [
            [("embeddings_2025_12_31_cu", "gv_tags_embedding")],
            [],                     # TABLESAMPLE returned nothing
            [("node", 5, "[0.5]")],  # random() fallback sample
            (0.0,),                 # reachable
        ]
        out = StringIO()
        call_command(
            "create_osmentity_vector_indexes",
            verify=True, verify_sample=1,
            country="cu", snapshot="2025_12_31",
            stdout=out,
        )
        sqls = [sql for sql, _ in stub_vectors._cursor.executed]
        assert any("ORDER BY random()" in s for s in sqls)
        assert "1/1 reachable" in out.getvalue()
