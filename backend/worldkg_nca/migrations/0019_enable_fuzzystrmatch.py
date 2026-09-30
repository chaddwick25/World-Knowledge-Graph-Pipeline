"""Enable the fuzzystrmatch extension (levenshtein) for Kuhn's Template
query correction (QueryCorrectionService)."""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('worldkg_nca', '0018_amenityclassmapping_and_more'),
    ]

    operations = [
        # fuzzystrmatch provides levenshtein()/difference() — the
        # edit-distance tier of the misspelled-amenity correction layer
        # (pg_trgm, added in 0017, provides the similarity tier).
        migrations.RunSQL(
            sql="CREATE EXTENSION IF NOT EXISTS fuzzystrmatch;",
            reverse_sql="DROP EXTENSION IF EXISTS fuzzystrmatch;",
        ),
    ]
