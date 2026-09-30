"""Create indexes on the partitioned parent table semantic_search_osmentity.

The individual partitions previously had btree indexes on wkg_class,
wikidata_uri, osm_type+osm_id, and GIST on geom — but these were created
manually on each partition, not on the parent table.  This meant new
partitions didn't inherit them.

This migration creates all indexes on the parent table.  PG 15 automatically
propagates CREATE INDEX on a partitioned parent to all existing AND future
partitions.

Also adds a GIN index on `tags` jsonb — this is the critical performance fix.
Tag-existence queries (tags ? 'amenity', tags ?| ARRAY[...]) were doing full
seq scans (~85s on CA's 53M rows).  The GIN index with jsonb_path_ops
accelerates ? and ?| operators to <100ms.

IMPORTANT: Run with --database=vectors (OsmEntity lives in the vectors DB).
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('worldkg_nca', '0010_alter_osmentity_snapshot_id_and_more'),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
                -- GIN index on tags jsonb (critical for tag-existence queries)
                CREATE INDEX IF NOT EXISTS osmentity_tags_gin_idx
                    ON semantic_search_osmentity USING GIN (tags jsonb_path_ops);

                -- Btree indexes on parent (propagate to all partitions)
                CREATE INDEX IF NOT EXISTS osmentity_wkg_class_idx
                    ON semantic_search_osmentity USING btree (wkg_class);

                CREATE INDEX IF NOT EXISTS osmentity_wikidata_uri_idx
                    ON semantic_search_osmentity USING btree (wikidata_uri);

                CREATE INDEX IF NOT EXISTS osmentity_snapshot_id_idx
                    ON semantic_search_osmentity USING btree (snapshot_id);

                CREATE INDEX IF NOT EXISTS osmentity_country_code_idx
                    ON semantic_search_osmentity USING btree (country_code);

                CREATE INDEX IF NOT EXISTS osmentity_snap_cc_idx
                    ON semantic_search_osmentity USING btree (snapshot_id, country_code);
            """,
            reverse_sql="""
                DROP INDEX IF EXISTS osmentity_tags_gin_idx;
                DROP INDEX IF EXISTS osmentity_wkg_class_idx;
                DROP INDEX IF EXISTS osmentity_wikidata_uri_idx;
                DROP INDEX IF EXISTS osmentity_snapshot_id_idx;
                DROP INDEX IF EXISTS osmentity_country_code_idx;
                DROP INDEX IF EXISTS osmentity_snap_cc_idx;
            """,
        ),
    ]
