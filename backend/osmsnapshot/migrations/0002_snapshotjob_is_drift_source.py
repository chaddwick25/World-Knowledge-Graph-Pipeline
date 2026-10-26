from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('osmsnapshot', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='snapshotjob',
            name='is_drift_source',
            field=models.BooleanField(
                db_index=True,
                default=False,
                help_text=(
                    'True for backfill jobs: the snapshot is a drift-source '
                    'only (no embeddings) and must never be offered as '
                    'queryable.'
                ),
            ),
        ),
    ]
