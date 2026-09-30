# Phase 6 — Add partition-key fields to OsmEntity (non-destructive)
#
# Adds nullable `snapshot_id` (VARCHAR partition key, e.g. '2025_12_31') and
# `country_code` (ISO 3166-1 alpha-2) to the monolith table.  These are
# populated by the `backfill_partition_keys` management command and become
# NOT NULL partition keys after the production cutover (Step 4 of
# PHASE6_OSMID_AUDIT_AND_CUTOVER_PLAN.md).
#
# The existing unique_together (osm_type, osm_id, gv_tags_version) is NOT
# changed here — that happens during the cutover when the constraint is
# widened to include the partition keys.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('worldkg_nca', '0006_precomputedlinkcandidate'),
    ]

    operations = [
        migrations.AddField(
            model_name='osmentity',
            name='snapshot_id',
            field=models.CharField(
                blank=True,
                db_index=True,
                help_text=(
                    "Temporal partition key (YYYY_MM_DD, e.g. '2025_12_31'). "
                    "Derived from source_snapshot_id -> TemporalSnapshot.timestamp."
                ),
                max_length=20,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name='osmentity',
            name='country_code',
            field=models.CharField(
                blank=True,
                db_index=True,
                help_text=(
                    "ISO 3166-1 alpha-2 country code (geographic partition key). "
                    "Populated via spatial join: geom -> country bbox."
                ),
                max_length=3,
                null=True,
            ),
        ),
        migrations.AddIndex(
            model_name='osmentity',
            index=models.Index(fields=['snapshot_id'], name='osmentity_snap_id_idx'),
        ),
        migrations.AddIndex(
            model_name='osmentity',
            index=models.Index(fields=['country_code'], name='osmentity_cc_idx'),
        ),
        migrations.AddIndex(
            model_name='osmentity',
            index=models.Index(fields=['snapshot_id', 'country_code'], name='osmentity_snap_cc_idx'),
        ),
    ]
