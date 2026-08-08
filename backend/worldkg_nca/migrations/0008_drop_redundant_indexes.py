# Drop 9 redundant indexes on OsmEntity (Phase 2 of OSMENTITY_MONOLITH_OPTIMIZATION.md)
#
# Removes db_index=True from 5 fields that also have explicit Meta.indexes entries
# (or are covered by a composite index).  This prevents Django from auto-creating
# duplicate btree + _like (varchar_pattern_ops) indexes on every makemigrations.
#
# Indexes dropped (9 total):
#   4 unused _like (varchar_pattern_ops) indexes — zero LIKE queries in codebase
#   4 duplicate btree indexes (db_index=True + Meta.indexes on same column)
#   1 covered index (source_snapshot_id single-col, covered by composite)
#
# The AlterField operations below handle dropping the db_index=True auto-created
# btree indexes.  The RunSQL block is a safety net that explicitly drops the
# _like indexes and the covered index (Django versions differ on whether
# AlterField drops _like indexes automatically).
#
# After this migration: 14 indexes remain (down from 23), saving ~380 MB now
# (~51 GB at 400M rows).  See docs/plans/OSMENTITY_MONOLITH_OPTIMIZATION.md Phase 2.
#
# IMPORTANT: Run with --database=vectors (OsmEntity lives in the vectors DB).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('worldkg_nca', '0007_osmentity_snapshot_id_country_code'),
    ]

    operations = [
        # ── Normalize Meta.indexes names (Django auto-generated → stable names) ──
        migrations.RenameIndex(
            model_name='osmentity',
            new_name='semantic_se_snapsho_4f8970_idx',
            old_name='osmentity_snap_id_idx',
        ),
        migrations.RenameIndex(
            model_name='osmentity',
            new_name='semantic_se_country_c3b37a_idx',
            old_name='osmentity_cc_idx',
        ),
        migrations.RenameIndex(
            model_name='osmentity',
            new_name='semantic_se_snapsho_c9aad4_idx',
            old_name='osmentity_snap_cc_idx',
        ),
        # ── Remove db_index=True from 5 fields ──────────────────────────────────
        # Each AlterField drops the auto-created btree index on that column.
        # The Meta.indexes entries (for wkg_class, wikidata_uri, country_code,
        # snapshot_id) ensure one properly-named index remains per column.
        # source_snapshot_id has no single-column Meta entry — it's covered by
        # the composite index (source_snapshot_id, gv_nle_trained).
        migrations.AlterField(
            model_name='osmentity',
            name='country_code',
            field=models.CharField(blank=True, help_text='ISO 3166-1 alpha-2 country code (geographic partition key). Populated via spatial join: geom → country bbox.', max_length=3, null=True),
        ),
        migrations.AlterField(
            model_name='osmentity',
            name='snapshot_id',
            field=models.CharField(blank=True, help_text="Temporal partition key (YYYY_MM_DD, e.g. '2025_12_31'). Derived from source_snapshot_id → TemporalSnapshot.timestamp.", max_length=20, null=True),
        ),
        migrations.AlterField(
            model_name='osmentity',
            name='source_snapshot_id',
            field=models.UUIDField(blank=True, help_text='UUID of source TemporalSnapshot (cross-database reference)', null=True),
        ),
        migrations.AlterField(
            model_name='osmentity',
            name='wikidata_uri',
            field=models.CharField(blank=True, help_text="Wikidata equivalent class URI via NCA alignment (owl:equivalentClass on the class, not owl:sameAs on the instance). E.g., 'http://www.wikidata.org/entity/Q11707'.", max_length=200, null=True),
        ),
        migrations.AlterField(
            model_name='osmentity',
            name='wkg_class',
            field=models.CharField(blank=True, help_text="WorldKG ontological class using wkgs: namespace (e.g., 'wkgs:Cafe', 'wkgs:Hospital'). Corresponds to rdf:type assertion in WorldKG RDF triples.", max_length=200, null=True),
        ),
        # ── Safety net: explicitly drop _like + covered indexes ──────────────────
        # The AlterField operations above should drop these, but Django versions
        # differ on _like index handling.  IF EXISTS makes this idempotent.
        migrations.RunSQL(
            sql="""
                -- 4 unused _like (varchar_pattern_ops) indexes (219 MB)
                DROP INDEX IF EXISTS semantic_search_osmentity_wkg_class_9b593cfc_like;
                DROP INDEX IF EXISTS semantic_search_osmentity_country_code_6c9f4923_like;
                DROP INDEX IF EXISTS semantic_search_osmentity_snapshot_id_1b669e86_like;
                DROP INDEX IF EXISTS semantic_search_osmentity_wikidata_uri_91ca2854_like;
                -- 1 covered index (51 MB) — covered by composite (source_snapshot_id, gv_nle_trained)
                DROP INDEX IF EXISTS semantic_search_osmentity_source_snapshot_id_2727345c;
            """,
            reverse_sql="""
                -- Recreate _like indexes (rollback — only if needed for LIKE queries)
                CREATE INDEX IF NOT EXISTS semantic_search_osmentity_wkg_class_9b593cfc_like
                    ON semantic_search_osmentity (wkg_class varchar_pattern_ops);
                CREATE INDEX IF NOT EXISTS semantic_search_osmentity_country_code_6c9f4923_like
                    ON semantic_search_osmentity (country_code varchar_pattern_ops);
                CREATE INDEX IF NOT EXISTS semantic_search_osmentity_snapshot_id_1b669e86_like
                    ON semantic_search_osmentity (snapshot_id varchar_pattern_ops);
                CREATE INDEX IF NOT EXISTS semantic_search_osmentity_wikidata_uri_91ca2854_like
                    ON semantic_search_osmentity (wikidata_uri varchar_pattern_ops);
                -- Recreate covered index
                CREATE INDEX IF NOT EXISTS semantic_search_osmentity_source_snapshot_id_2727345c
                    ON semantic_search_osmentity (source_snapshot_id);
            """,
        ),
    ]
