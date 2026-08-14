"""Unit tests for the per-leaf HNSW index lifecycle.

Implements the test plan from
``docs/plans/PER_LEAF_INDEX_LIFECYCLE_PLAN.md`` §6.1:

- ``drop_osmentity_vector_indexes --country ni --snapshot 2025_12_31``:
  - Drops HNSW on ``embeddings_2025_12_31_ni`` only
  - Does NOT drop HNSW on ``embeddings_2025_12_31_jm``
  - Does NOT drop root or snapshot partitioned indexes
- ``drop_osmentity_vector_indexes`` (no args):
  - Drops ALL HNSW (existing global behavior preserved)
- ``create_osmentity_vector_indexes --country ni --snapshot 2025_12_31``:
  - Creates HNSW only on ``embeddings_2025_12_31_ni``
  - Does NOT create HNSW on other leaves
- ``_should_drop_indexes_during_load`` with ``auto``:
  - Returns ``True`` when ``pg_partitioned_table`` row exists
  - Returns ``False`` when it doesn't (monolith)
  - Returns ``True`` for ``true``/``1``/``yes`` regardless
  - Returns ``False`` for ``false``/``0``/``no`` regardless

These tests do NOT touch the database — ``connections['vectors']`` is
stubbed so the SQL the commands issue is captured and inspected.
"""

from io import StringIO

import pytest

from django.core.management import call_command


# --------------------------------------------------------------------------- #
# Stub infrastructure
# --------------------------------------------------------------------------- #

class StubCursor:
    """Captures executed SQL and returns canned result sets.

    ``self.results`` is a list of rows to return on the *next* call to
    ``fetchall``/``fetchone``; each ``execute`` consumes one entry.  When
    ``results`` is exhausted, ``fetchall`` returns ``[]`` and ``fetchone``
    returns ``(False,)`` (the safe default for the partition-check EXISTS
    query).
    """

    def __init__(self, results=None):
        self.executed = []          # list of (sql, params)
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
    """Mimics the bits of a Django DB connection the commands use."""

    def __init__(self, results=None):
        self._cursor = StubCursor(results)
        self._autocommit = False

    def cursor(self):
        # Each ``with conn.cursor()`` returns a fresh view onto the same
        # captured-execution list.  The commands open multiple cursors
        # (one per CREATE INDEX CONCURRENTLY) — they should all share state.
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
    """Replace ``connections['vectors']`` with a stub for the vectors DB.

    The management commands import ``connections`` at module load time
    (``from django.db import connections``), so we patch the attribute on
    each consumer module.  The step_1_embed helper does a *lazy* import
    inside ``_should_drop_indexes_during_load`` (``from django.db import
    connections``), so we also patch ``django.db.connections`` to cover
    that path.
    """
    conn = StubConnection()
    stub = StubConnections(conn)
    import worldkg_nca.management.commands.drop_osmentity_vector_indexes as drop_cmd
    import worldkg_nca.management.commands.create_osmentity_vector_indexes as create_cmd
    import django.db as django_db
    monkeypatch.setattr(drop_cmd, "connections", stub)
    monkeypatch.setattr(create_cmd, "connections", stub)
    monkeypatch.setattr(django_db, "connections", stub)
    return conn


# --------------------------------------------------------------------------- #
# drop_osmentity_vector_indexes
# --------------------------------------------------------------------------- #

class TestDropPerLeaf:
    """``--country`` + ``--snapshot`` scopes the drop to one leaf."""

    @pytest.mark.unit
    def test_per_leaf_drop_only_targets_specified_leaf(self, stub_vectors, monkeypatch):
        # The leaf-index query returns one HNSW index on the NI leaf.
        stub_vectors._cursor.results = [
            [("idx_embeddings_2025_12_31_ni_gv_tags_embedding_hnsw",)],
        ]
        out = StringIO()
        call_command(
            "drop_osmentity_vector_indexes",
            country="ni", snapshot="2025_12_31",
            stdout=out,
        )
        sqls = [sql for sql, _ in stub_vectors._cursor.executed]
        params = [p for _, p in stub_vectors._cursor.executed]

        # The WHERE clause must pin the leaf by name (parameterised).
        leaf_lookup = any(
            "%s" in sql and p == ["embeddings_2025_12_31_ni"]
            for sql, p in stub_vectors._cursor.executed
        )
        assert leaf_lookup, "per-leaf drop must query the specific leaf table"

        # The DROP INDEX must target the NI index, never JM.
        drop_sqls = [s for s in sqls if s.startswith("DROP INDEX")]
        assert drop_sqls, "expected at least one DROP INDEX"
        assert any("idx_embeddings_2025_12_31_ni" in s for s in drop_sqls)
        assert not any("embeddings_2025_12_31_jm" in s for s in drop_sqls)
        assert not any("semantic_search_osmentity" in s for s in drop_sqls), (
            "per-leaf drop must not touch the root table"
        )

    @pytest.mark.unit
    def test_per_leaf_drop_lowercases_country_code(self, stub_vectors):
        """Country codes are normalised to lowercase for partition names."""
        stub_vectors._cursor.results = [[]]
        out = StringIO()
        call_command(
            "drop_osmentity_vector_indexes",
            country="NI", snapshot="2025_12_31",
            stdout=out,
        )
        # The parameterised lookup must use the lowercased leaf name.
        assert any(
            p == ["embeddings_2025_12_31_ni"]
            for _, p in stub_vectors._cursor.executed
        ), "country code must be lowercased to match the partition name"

    @pytest.mark.unit
    def test_per_leaf_drop_with_no_indexes_is_noop(self, stub_vectors):
        stub_vectors._cursor.results = [[]]
        out = StringIO()
        call_command(
            "drop_osmentity_vector_indexes",
            country="ni", snapshot="2025_12_31",
            stdout=out,
        )
        drop_sqls = [
            s for s, _ in stub_vectors._cursor.executed
            if s.startswith("DROP INDEX")
        ]
        assert drop_sqls == []


class TestDropGlobal:
    """No-args invocation preserves the existing global behavior."""

    @pytest.mark.unit
    def test_global_drop_queries_root_table(self, stub_vectors):
        # Three sequential result sets: root (1 col), snapshot (2 cols), leaf (3 cols).
        stub_vectors._cursor.results = [
            [("osmentity_static_embedding_hnsw_idx",)],
            [],
            [("idx_embeddings_2025_12_31_jm_gv_tags_embedding_hnsw",
              "CREATE INDEX ... ON embeddings_2025_12_31_jm ... hnsw",
              "embeddings_2025_12_31_jm")],
        ]
        out = StringIO()
        call_command("drop_osmentity_vector_indexes", stdout=out)
        sqls = [sql for sql, _ in stub_vectors._cursor.executed]

        # The global path queries the root table by name.
        assert any(
            "semantic_search_osmentity" in s and "pg_indexes" in s
            for s in sqls
        ), "global drop must query the root partitioned table"
        # And it issues DROPs for both root and leaf indexes.
        drop_sqls = [s for s in sqls if s.startswith("DROP INDEX")]
        assert any("osmentity_static_embedding_hnsw_idx" in s for s in drop_sqls)
        assert any("embeddings_2025_12_31_jm" in s for s in drop_sqls)

    @pytest.mark.unit
    def test_only_country_without_snapshot_errors(self, stub_vectors):
        err = StringIO()
        call_command(
            "drop_osmentity_vector_indexes",
            country="ni",
            stdout=StringIO(), stderr=err,
        )
        assert "together" in err.getvalue()


# --------------------------------------------------------------------------- #
# create_osmentity_vector_indexes
# --------------------------------------------------------------------------- #

class TestCreatePerLeaf:
    """``--country`` + ``--snapshot`` scopes creation to one leaf."""

    @pytest.mark.unit
    def test_per_leaf_create_only_targets_specified_leaf(self, stub_vectors):
        # The missing-index query returns two columns to build.
        stub_vectors._cursor.results = [
            [("gv_tags_embedding",), ("gv_nle_embedding",)],
        ]
        out = StringIO()
        call_command(
            "create_osmentity_vector_indexes",
            country="ni", snapshot="2025_12_31",
            stdout=out,
        )
        sqls = [sql for sql, _ in stub_vectors._cursor.executed]

        # The lookup must be parameterised by the leaf name.
        assert any(
            params == ["embeddings_2025_12_31_ni"]
            for _, params in stub_vectors._cursor.executed
        ), "per-leaf create must query the specific leaf table"

        create_sqls = [s for s in sqls if "CREATE INDEX CONCURRENTLY" in s]
        assert len(create_sqls) == 2
        # Both indexes must be on the NI leaf.
        for s in create_sqls:
            assert "embeddings_2025_12_31_ni" in s
            assert "embeddings_2025_12_31_jm" not in s
        # Names follow the idx_{leaf}_{col}_hnsw convention.
        assert any("idx_embeddings_2025_12_31_ni_gv_tags_embedding_hnsw" in s
                   for s in create_sqls)
        assert any("idx_embeddings_2025_12_31_ni_gv_nle_embedding_hnsw" in s
                   for s in create_sqls)

    @pytest.mark.unit
    def test_per_leaf_create_with_no_missing_is_noop(self, stub_vectors):
        stub_vectors._cursor.results = [[]]
        out = StringIO()
        call_command(
            "create_osmentity_vector_indexes",
            country="ni", snapshot="2025_12_31",
            stdout=out,
        )
        create_sqls = [
            s for s, _ in stub_vectors._cursor.executed
            if "CREATE INDEX CONCURRENTLY" in s
        ]
        assert create_sqls == []

    @pytest.mark.unit
    def test_per_leaf_create_sets_autocommit(self, stub_vectors):
        """CONCURRENTLY requires autocommit; the command must toggle it."""
        stub_vectors._cursor.results = [[]]
        call_command(
            "create_osmentity_vector_indexes",
            country="ni", snapshot="2025_12_31",
            stdout=StringIO(),
        )
        # set_autocommit(True) was called, then restored to False.
        assert stub_vectors._autocommit is False


class TestCreateGlobal:
    """No-args invocation preserves the existing global behavior."""

    @pytest.mark.unit
    def test_global_create_scans_all_leaves(self, stub_vectors):
        # Global mode uses a LIKE 'embeddings\_%' scan, not a parameterised
        # lookup.  Return one missing index on JM.
        stub_vectors._cursor.results = [
            [("embeddings_2025_12_31_jm", "gv_tags_embedding")],
        ]
        out = StringIO()
        call_command("create_osmentity_vector_indexes", stdout=out)
        sqls = [sql for sql, _ in stub_vectors._cursor.executed]

        # The global lookup uses LIKE 'embeddings\_%' (no %s parameter).
        assert any(
            "embeddings\\_%" in s and "%s" not in s
            for s in sqls
        ), "global create must scan all leaves via LIKE"
        create_sqls = [s for s in sqls if "CREATE INDEX CONCURRENTLY" in s]
        assert len(create_sqls) == 1
        assert "embeddings_2025_12_31_jm" in create_sqls[0]


# --------------------------------------------------------------------------- #
# _should_drop_indexes_during_load — auto mode runtime check
# --------------------------------------------------------------------------- #

class TestShouldDropIndexesDuringLoad:
    """``auto`` mode does the runtime ``pg_partitioned_table`` check."""

    @pytest.mark.unit
    def test_true_values_always_drop(self, monkeypatch):
        from pipeline.tasks.country_pipeline_steps.step_1_embed import (
            _should_drop_indexes_during_load,
        )
        for val in ("true", "1", "yes", "TRUE", "Yes"):
            monkeypatch.setenv("DROP_INDEXES_DURING_LOAD", val)
            assert _should_drop_indexes_during_load("ni") is True

    @pytest.mark.unit
    def test_false_values_never_drop(self, monkeypatch):
        from pipeline.tasks.country_pipeline_steps.step_1_embed import (
            _should_drop_indexes_during_load,
        )
        for val in ("false", "0", "no", "FALSE", "No"):
            monkeypatch.setenv("DROP_INDEXES_DURING_LOAD", val)
            assert _should_drop_indexes_during_load("ni") is False

    @pytest.mark.unit
    def test_auto_returns_true_when_partitioned(self, stub_vectors, monkeypatch):
        monkeypatch.setenv("DROP_INDEXES_DURING_LOAD", "auto")
        stub_vectors._cursor.results = [[True]]   # EXISTS → true
        from pipeline.tasks.country_pipeline_steps.step_1_embed import (
            _should_drop_indexes_during_load,
        )
        assert _should_drop_indexes_during_load("ni") is True
        # And the check actually ran the partitioned-table query.
        assert any(
            "pg_partitioned_table" in sql
            for sql, _ in stub_vectors._cursor.executed
        )

    @pytest.mark.unit
    def test_auto_returns_false_when_monolith(self, stub_vectors, monkeypatch):
        """On a fresh DB (monolith, not partitioned) auto → no drop."""
        monkeypatch.setenv("DROP_INDEXES_DURING_LOAD", "auto")
        stub_vectors._cursor.results = [[False]]  # EXISTS → false
        from pipeline.tasks.country_pipeline_steps.step_1_embed import (
            _should_drop_indexes_during_load,
        )
        assert _should_drop_indexes_during_load("ni") is False

    @pytest.mark.unit
    def test_auto_defaults_to_false_on_db_error(self, monkeypatch):
        """If the vectors DB is unreachable, auto falls back to no-drop."""
        monkeypatch.setenv("DROP_INDEXES_DURING_LOAD", "auto")

        class BoomCursor(StubCursor):
            def execute(self, sql, params=None):
                raise RuntimeError("vectors DB not reachable")

        class BoomConnection(StubConnection):
            def cursor(self):
                return BoomCursor()

        stub = StubConnections(BoomConnection())
        import django.db as django_db
        monkeypatch.setattr(django_db, "connections", stub)
        from pipeline.tasks.country_pipeline_steps.step_1_embed import (
            _should_drop_indexes_during_load,
        )
        assert _should_drop_indexes_during_load("ni") is False
