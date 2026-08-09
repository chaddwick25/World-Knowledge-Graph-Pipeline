# Phase 6 Step 4 — Widen OsmEntity unique constraint for partitioned table
#
# ⚠️  CUTOVER MIGRATION — read this before applying.
#
# This migration changes the unique constraint from:
#   (osm_type, osm_id, gv_tags_version)
# to:
#   (osm_type, osm_id, gv_tags_version, snapshot_id, country_code)
#
# **When to run:** This migration is SAFE to run at any time — it uses a
# guard to check whether ``semantic_search_osmentity`` is partitioned (i.e.
# the cutover rename has happened).  If the table is NOT partitioned (still
# the monolith), the database operations are a NO-OP — only the Django model
# state is updated.  If the table IS partitioned (post-cutover), the old
# constraint is dropped and the new widened constraint is created.
#
# **Why the guard:** The monolith has nullable ``snapshot_id`` and
# ``country_code``.  A 5-column unique constraint with nullable columns
# doesn't prevent duplicates (NULL ≠ NULL in Postgres), so it's useless on
# the monolith and would just waste space.  The guard prevents this.
#
# **SeparateDatabaseAndState:** The state operation (AlterUniqueTogether)
# always runs so Django's model state stays in sync with the model file.
# The database operations are conditional via RunPython.
#
# IMPORTANT: Run with --database=vectors (OsmEntity lives in the vectors DB).

from django.db import migrations, models


def _is_partitioned(cursor, table_name):
    """Check if a table is a partitioned table (has a partitioning spec)."""
    cursor.execute("""
        SELECT EXISTS (
            SELECT 1 FROM pg_partitioned_table pt
            JOIN pg_class c ON c.oid = pt.partrelid
            WHERE c.relname = %s
        );
    """, [table_name])
    return cursor.fetchone()[0]


def forward(apps, schema_editor):
    """Apply constraint changes only if the table is partitioned (post-cutover).

    Pre-cutover (monolith): NO-OP.  The monolith's 3-column constraint stays
    in place.  Only the Django model state is updated (via AlterUniqueTogether
    in state_operations).

    Post-cutover (partitioned table renamed to semantic_search_osmentity):
    1. Drop the old monolith's 3-column unique constraint (IF EXISTS — it
       may not exist on the renamed partitioned table).
    2. Drop the raw-SQL unique index from create_country_partitions.
    3. Create the new Django-named unique constraint with 5 columns.
    """
    conn = schema_editor.connection
    with conn.cursor() as cursor:
        if not _is_partitioned(cursor, 'semantic_search_osmentity'):
            # Pre-cutover: the table is the monolith.  Do NOT touch the
            # constraints — the monolith has nullable partition keys.
            # Also ensure the partitioned table's unique index exists
            # (in case a previous incorrect run dropped it).
            cursor.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_embeddings_partitioned_entity_unique
                ON embeddings_partitioned
                (osm_type, osm_id, gv_tags_version, snapshot_id, country_code);
            """)
            print("  0009: semantic_search_osmentity is not partitioned — "
                  "cutover has not happened. DB operations skipped.")
            return

        # Post-cutover: the table is the partitioned one.
        # 1. Drop old monolith constraint (if it somehow still exists).
        cursor.execute("""
            ALTER TABLE semantic_search_osmentity
                DROP CONSTRAINT IF EXISTS
                semantic_search_osmentity_osm_type_osm_id_gv_tags_versi_1c24ec52_uniq;
        """)
        # Also try the actual monolith constraint name (different hash).
        cursor.execute("""
            ALTER TABLE semantic_search_osmentity
                DROP CONSTRAINT IF EXISTS
                semantic_search_osmentit_osm_type_osm_id_gv_tags__67c1c7ee_uniq;
        """)
        # 2. Drop the raw-SQL unique index from create_country_partitions.
        cursor.execute("""
            DROP INDEX IF EXISTS idx_embeddings_partitioned_entity_unique;
        """)
        # 3. Create the new Django-named unique constraint.
        #    PostgreSQL doesn't support ADD CONSTRAINT IF NOT EXISTS, so
        #    we drop first (IF EXISTS) then add.
        cursor.execute("""
            ALTER TABLE semantic_search_osmentity
                DROP CONSTRAINT IF EXISTS
                semantic_search_osmentity_osm_type_osm_id_gv_tags_versi_0c6b1e14_uniq;
        """)
        cursor.execute("""
            ALTER TABLE semantic_search_osmentity
                ADD CONSTRAINT
                semantic_search_osmentity_osm_type_osm_id_gv_tags_versi_0c6b1e14_uniq
                UNIQUE (osm_type, osm_id, gv_tags_version, snapshot_id, country_code);
        """)
        print("  0009: Applied widened unique constraint on partitioned table.")


def reverse(apps, schema_editor):
    """Reverse the constraint changes.

    Pre-cutover: re-create the partitioned table's unique index (in case
    a previous incorrect run dropped it) and remove the 5-column constraint
    from the monolith (if a previous incorrect run added it).

    Post-cutover: drop the 5-column constraint, re-create the raw-SQL
    unique index on the partitioned table.
    """
    conn = schema_editor.connection
    with conn.cursor() as cursor:
        if _is_partitioned(cursor, 'semantic_search_osmentity'):
            # Post-cutover reverse
            cursor.execute("""
                ALTER TABLE semantic_search_osmentity
                    DROP CONSTRAINT IF EXISTS
                    semantic_search_osmentity_osm_type_osm_id_gv_tags_versi_0c6b1e14_uniq;
            """)
            cursor.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_embeddings_partitioned_entity_unique
                ON semantic_search_osmentity
                (osm_type, osm_id, gv_tags_version, snapshot_id, country_code);
            """)
            print("  0009 reverse: Reversed widened unique constraint on partitioned table.")
        else:
            # Pre-cutover reverse (fixes incorrect application to monolith).
            # 1. Drop the 5-column constraint that was incorrectly added.
            cursor.execute("""
                ALTER TABLE semantic_search_osmentity
                    DROP CONSTRAINT IF EXISTS
                    semantic_search_osmentity_osm_type_osm_id_gv_tags_versi_0c6b1e14_uniq;
            """)
            # 2. Re-create the partitioned table's unique index (was dropped
            #    by the incorrect forward run).
            cursor.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_embeddings_partitioned_entity_unique
                ON embeddings_partitioned
                (osm_type, osm_id, gv_tags_version, snapshot_id, country_code);
            """)
            # 3. The 3-column constraint should still exist on the monolith
            #    (it was never dropped because the constraint name didn't
            #    match).  Ensure it exists — drop first (IF EXISTS) then add
            #    since PostgreSQL doesn't support ADD CONSTRAINT IF NOT EXISTS.
            cursor.execute("""
                ALTER TABLE semantic_search_osmentity
                    DROP CONSTRAINT IF EXISTS
                    semantic_search_osmentit_osm_type_osm_id_gv_tags__67c1c7ee_uniq;
            """)
            cursor.execute("""
                ALTER TABLE semantic_search_osmentity
                    ADD CONSTRAINT
                    semantic_search_osmentit_osm_type_osm_id_gv_tags__67c1c7ee_uniq
                    UNIQUE (osm_type, osm_id, gv_tags_version);
            """)
            print("  0009 reverse: Fixed monolith constraints + re-created "
                  "partitioned table unique index.")


class Migration(migrations.Migration):

    dependencies = [
        ('worldkg_nca', '0008_drop_redundant_indexes'),
    ]

    operations = [
        # Use SeparateDatabaseAndState because:
        # - The state operation (AlterUniqueTogether) always runs to keep
        #   Django's model state in sync with the model file.
        # - The database operations are conditional via RunPython — they
        #   only apply when the table is partitioned (post-cutover).
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunPython(forward, reverse),
            ],
            state_operations=[
                migrations.AlterUniqueTogether(
                    name='osmentity',
                    unique_together={('osm_type', 'osm_id', 'gv_tags_version', 'snapshot_id', 'country_code')},
                ),
            ],
        ),
    ]
