"""Add GraphSpectralFingerprint + GraphSpectralDrift models.

Computed in pipeline Steps 5c/5d (GRAPH_SPECTRAL_TEMPORAL_PLAN.md) from
the k-NN graph built in Step 5. Both models live on the ``default`` DB
(same as ``osmsnapshot.Snapshot`` they reference).
"""

import uuid

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('osmsnapshot', '0001_initial'),
        ('semantic_search', '0002_alter_snapshotdiff_prev_snapshot_id_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='GraphSpectralFingerprint',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('region', models.CharField(db_index=True, help_text='ISO 3166-1 alpha-2 country code', max_length=10)),
                ('eigenvalues', models.JSONField(help_text="Top-k non-trivial eigenvalues of the normalized Laplacian (λ₁..λₖ, sorted ascending, each ∈ [0, 2])")),
                ('fiedler_vector', models.JSONField(help_text='Fiedler vector (2nd eigenvector) — sampled/truncated for storage')),
                ('algebraic_connectivity', models.FloatField(help_text='λ₂ (Fiedler value) — how well-connected the graph is')),
                ('spectral_gap', models.FloatField(help_text='λₖ - λ₂ — reveals cluster structure')),
                ('signal_smoothness', models.FloatField(help_text='Dirichlet energy sᵀLs for the wkg_class graph signal')),
                ('node_count', models.IntegerField(help_text='Number of graph nodes')),
                ('edge_count', models.IntegerField(help_text='Number of graph edges')),
                ('k_eigenvalues', models.IntegerField(default=128, help_text='Number of eigenvalues computed')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('snapshot', models.ForeignKey(help_text='Snapshot this fingerprint was computed from', on_delete=models.deletion.CASCADE, related_name='spectral_fingerprints', to='osmsnapshot.snapshot')),
            ],
            options={
                'db_table': 'semantic_search_graphspectralfingerprint',
                'ordering': ['region', '-created_at'],
                'verbose_name': 'Graph Spectral Fingerprint',
                'verbose_name_plural': 'Graph Spectral Fingerprints',
                'unique_together': {('region', 'snapshot')},
            },
        ),
        migrations.AddIndex(
            model_name='graphspectralfingerprint',
            index=models.Index(
                fields=['region', '-created_at'],
                name='semantic_se_region_7a5eee_idx',
            ),
        ),
        migrations.CreateModel(
            name='GraphSpectralDrift',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('region', models.CharField(db_index=True, help_text='ISO 3166-1 alpha-2 country code', max_length=10)),
                ('spectral_distance', models.FloatField(help_text='‖λ_t - λ_{t-1}‖₂ — structural change magnitude')),
                ('connectivity_delta', models.FloatField(help_text='Δλ₂ — algebraic connectivity shift')),
                ('spectral_gap_delta', models.FloatField(help_text='Δ(λₖ - λ₂) — spectral gap shift')),
                ('fiedler_drift', models.FloatField(help_text='Cosine distance between Fiedler vectors ∈ [0, 2]')),
                ('smoothness_delta', models.FloatField(help_text='Δ(sᵀLs) — Dirichlet energy shift')),
                ('drift_magnitude', models.CharField(default='low', help_text='Categorical magnitude: low / medium / high / extreme', max_length=10)),
                ('forecast_eigenvalues', models.JSONField(blank=True, help_text='ARIMA / exp-smoothing forecast of next-snapshot eigenvalues', null=True)),
                ('forecast_confidence', models.JSONField(blank=True, help_text='1.96σ prediction interval half-widths per eigenvalue', null=True)),
                ('changepoint_detected', models.BooleanField(default=False, help_text='True if CUSUM detected a structural change point')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('snapshot_from', models.ForeignKey(help_text='Earlier snapshot (T_{t-1})', on_delete=models.deletion.CASCADE, related_name='spectral_drift_from', to='osmsnapshot.snapshot')),
                ('snapshot_to', models.ForeignKey(help_text='Later snapshot (T_t)', on_delete=models.deletion.CASCADE, related_name='spectral_drift_to', to='osmsnapshot.snapshot')),
            ],
            options={
                'db_table': 'semantic_search_graphspectraldrift',
                'ordering': ['region', '-created_at'],
                'verbose_name': 'Graph Spectral Drift',
                'verbose_name_plural': 'Graph Spectral Drifts',
                'unique_together': {('region', 'snapshot_from', 'snapshot_to')},
            },
        ),
        migrations.AddIndex(
            model_name='graphspectraldrift',
            index=models.Index(
                fields=['region', '-created_at'],
                name='semantic_se_region_a01382_idx',
            ),
        ),
    ]
