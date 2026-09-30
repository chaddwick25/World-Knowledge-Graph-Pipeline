"""Add GIN trigram index on the name tag expression for geocoder lookups."""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('worldkg_nca', '0019_enable_fuzzystrmatch'),
    ]

    operations = [
        ***REMOVED*** Expression GIN trigram index on tags->>'name'. EntityGeocoder's
        ***REMOVED*** contains tier (tags__name__icontains) and the executor's anchor
        ***REMOVED*** search filter on this exact expression; unindexed, they
        ***REMOVED*** sequential-scan the country leaf (11M rows for MX -> 40-80s per
        ***REMOVED*** anchor, the 2026-09-14 desktop query regression). gin_trgm_ops
        ***REMOVED*** serves ILIKE '%..%' patterns directly, so the contains and
        ***REMOVED*** abbreviation tiers become index-assisted for every country,
        ***REMOVED*** independent of name_romanized coverage.
        migrations.RunSQL(
            sql=(
                "CREATE INDEX IF NOT EXISTS idx_osmentity_name_trgm "
                "ON semantic_search_osmentity "
                "USING GIN ((tags->>'name') gin_trgm_ops);"
            ),
            reverse_sql=(
                "DROP INDEX IF EXISTS idx_osmentity_name_trgm;"
            ),
        ),
    ]
