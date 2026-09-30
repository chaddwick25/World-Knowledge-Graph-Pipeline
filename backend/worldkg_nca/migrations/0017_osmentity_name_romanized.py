"""Add name_romanized field to OsmEntity + pg_trgm GIN index."""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('worldkg_nca', '0016_subgraph_transport'),
    ]

    operations = [
        # Add the name_romanized column (nullable — populated by romanize_names command)
        migrations.AddField(
            model_name='osmentity',
            name='name_romanized',
            field=models.CharField(
                blank=True,
                max_length=500,
                null=True,
                help_text="Romanized name for cross-script similarity search. "
                          "Populated by romanize_names command. NULL before romanization."
            ),
        ),
        # Enable pg_trgm extension (idempotent — safe if already enabled)
        migrations.RunSQL(
            sql="CREATE EXTENSION IF NOT EXISTS pg_trgm;",
            reverse_sql="DROP EXTENSION IF EXISTS pg_trgm;",
        ),
        # GIN trigram index on name_romanized for fast similarity search.
        # Uses gin_trgm_ops for the % operator and similarity() function.
        # This index enables WHERE name_romanized % 'query' in O(1) instead
        # of scanning all rows.
        migrations.RunSQL(
            sql=(
                "CREATE INDEX IF NOT EXISTS idx_osmentity_name_romanized_trgm "
                "ON semantic_search_osmentity "
                "USING GIN (name_romanized gin_trgm_ops);"
            ),
            reverse_sql=(
                "DROP INDEX IF EXISTS idx_osmentity_name_romanized_trgm;"
            ),
        ),
    ]
