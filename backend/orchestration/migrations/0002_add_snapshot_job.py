# Generated for TEMPORAL_SNAPSHOT_REFACTOR.md Phase A.
#
# Adds the SnapshotJob model — DB ground truth for "has this country been
# processed for this snapshot date?". The unique_together on
# (snapshot_date, country_code) enforces "once processed, can't re-run".

import uuid
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('orchestration', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='SnapshotJob',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('snapshot_date', models.CharField(db_index=True, help_text="Snapshot date in YYYY_MM_DD format (e.g., '2025_12_31').", max_length=10)),
                ('country_code', models.CharField(db_index=True, help_text="ISO 3166-1 alpha-2 country code (e.g., 'JM').", max_length=3)),
                ('country_name', models.CharField(max_length=100)),
                ('status', models.CharField(choices=[('pending', 'Pending'), ('running', 'Running'), ('completed', 'Completed'), ('failed', 'Failed')], default='pending', max_length=20)),
                ('celery_task_id', models.CharField(blank=True, max_length=255, null=True)),
                ('total_entities', models.BigIntegerField(default=0)),
                ('total_aligned', models.BigIntegerField(default=0)),
                ('total_spatial_links', models.BigIntegerField(default=0)),
                ('error_message', models.TextField(blank=True, null=True)),
                ('started_at', models.DateTimeField(blank=True, null=True)),
                ('completed_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('pipeline_run', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='snapshot_jobs', to='orchestration.pipelinerun')),
            ],
            options={
                'db_table': 'snapshot_jobs',
                'ordering': ['-created_at'],
                'unique_together': {('snapshot_date', 'country_code')},
            },
        ),
        migrations.AddIndex(
            model_name='snapshotjob',
            index=models.Index(fields=['snapshot_date', 'status'], name='orch_snaps_date_status_idx'),
        ),
        migrations.AddIndex(
            model_name='snapshotjob',
            index=models.Index(fields=['country_code', 'status'], name='orch_snaps_cc_status_idx'),
        ),
    ]
