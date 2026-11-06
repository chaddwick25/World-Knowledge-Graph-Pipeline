"""Schema parity between the partitioned root and the OsmEntity model.

Regression cover for the drift found by the k80 v2 end-to-end run
(2026-11-05): ``create_country_partitions`` built the partitioned root from a
hand-maintained column list that predated migration 0017, so every partition
created afterwards lacked ``name_romanized``.  The ORM's SELECT then failed
with ``UndefinedColumn``, the transaction aborted, the open server-side cursor
was destroyed, and the error surfaced as
``InvalidCursorName: cursor "_django_curs_..._sync_1" does not exist`` — the
schema bug masquerading as a driver bug.

Tests run against the live Docker DBs (see conftest.py).  They use a scratch
schema and never touch the real ``semantic_search_osmentity``.
"""

import pytest
from django.db import connections

from core.management.commands.create_country_partitions import (
    ROOT_TABLE,
    Command,
)
from worldkg_nca.models import OsmEntity

pytestmark = pytest.mark.django_db(
    transaction=False, databases=["default", "vectors"],
)

SCRATCH = "parity_test"


@pytest.fixture
def scratch(connections_guard=None):
    """A throwaway schema with search_path pointed at it, then cleaned up."""
    connection = connections["vectors"]
    with connection.cursor() as cursor:
        cursor.execute(f"DROP SCHEMA IF EXISTS {SCRATCH} CASCADE")
        cursor.execute(f"CREATE SCHEMA {SCRATCH}")
        cursor.execute(f"SET search_path TO {SCRATCH}, public")
    try:
        yield connection
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SET search_path TO public")
            cursor.execute(f"DROP SCHEMA IF EXISTS {SCRATCH} CASCADE")


def test_model_column_defs_cover_every_model_field():
    """The generated DDL must name every column the ORM selects."""
    definitions = Command._model_column_defs(connections["vectors"])
    named = {definition.split()[0] for definition in definitions}

    assert named == {field.column for field in OsmEntity._meta.local_fields}


def test_model_column_defs_include_name_romanized():
    """The specific column whose absence broke the v2 run."""
    definitions = Command._model_column_defs(connections["vectors"])
    assert any(d.startswith("name_romanized ") for d in definitions)


def test_reconcile_adds_missing_column(scratch):
    """A stale root (pre-0017) is healed in place."""
    command = Command()
    with scratch.cursor() as cursor:
        cursor.execute(
            f"CREATE TABLE {ROOT_TABLE} (id bigint, osm_id bigint)"
        )
        added = command._reconcile_root_columns(cursor)

        assert "name_romanized" in added

        cursor.execute(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s
            """,
            [SCRATCH, ROOT_TABLE],
        )
        present = {row[0] for row in cursor.fetchall()}

    assert {field.column for field in OsmEntity._meta.local_fields} <= present


def test_reconcile_propagates_to_existing_partitions(scratch):
    """ALTER on a partitioned parent must reach partitions already created.

    This is what makes the fix safe for databases that already hold country
    leaves — no rebuild, no data movement.
    """
    command = Command()
    with scratch.cursor() as cursor:
        cursor.execute(
            f"CREATE TABLE {ROOT_TABLE} ("
            "  id bigint NOT NULL,"
            "  snapshot_id varchar(20) NOT NULL,"
            "  country_code varchar(3) NOT NULL,"
            "  PRIMARY KEY (id, snapshot_id, country_code)"
            ") PARTITION BY LIST (snapshot_id)"
        )
        cursor.execute(
            f"CREATE TABLE {ROOT_TABLE}_2025_12_31 "
            f"PARTITION OF {ROOT_TABLE} FOR VALUES IN ('2025_12_31')"
        )

        command._reconcile_root_columns(cursor)

        for table in (ROOT_TABLE, f"{ROOT_TABLE}_2025_12_31"):
            cursor.execute(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_schema = %s AND table_name = %s
                """,
                [SCRATCH, table],
            )
            present = {row[0] for row in cursor.fetchall()}
            assert "name_romanized" in present, f"{table} missing the column"


def test_reconcile_is_idempotent(scratch):
    """Running it twice adds nothing the second time."""
    command = Command()
    with scratch.cursor() as cursor:
        cursor.execute(f"CREATE TABLE {ROOT_TABLE} (id bigint)")
        first = command._reconcile_root_columns(cursor)
        second = command._reconcile_root_columns(cursor)

    assert first
    assert second == []
