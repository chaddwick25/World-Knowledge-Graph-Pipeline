# Generated for SUBGRAPH_FUNCTIONAL_MAPS_PLAN_v2 Phase 1

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('worldkg_nca', '0015_add_subgraph_slug_to_spectralnodemetric'),
    ]

    operations = [
        migrations.CreateModel(
            name='SubgraphTransport',
            fields=[
                ('id', models.BigAutoField(primary_key=True)),
                ('snapshot_id', models.CharField(
                    db_index=True, max_length=20,
                    help_text='Snapshot date (YYYY_MM_DD) — matches '
                              'OsmEntity.snapshot_id',
                )),
                ('country_code', models.CharField(
                    db_index=True, max_length=3,
                    help_text='ISO 3166-1 alpha-2 country code',
                )),
                ('subgraph_from', models.CharField(
                    db_index=True, max_length=255,
                    help_text='Source subgraph slug '
                              '(eigenbasis to transport FROM)',
                )),
                ('subgraph_to', models.CharField(
                    db_index=True, max_length=255,
                    help_text='Target subgraph slug '
                              '(eigenbasis to transport TO)',
                )),
                ('transport_matrix', models.JSONField(
                    help_text='k×k matrix (list of lists).  Transports '
                              'eigen-loadings from subgraph_from\'s '
                              'eigenbasis to subgraph_to\'s eigenbasis: '
                              'loadings_to = C · loadings_from.',
                )),
                ('k_dim', models.IntegerField(
                    help_text='Dimension of the transport matrix (k×k).',
                )),
                ('shared_entity_count', models.IntegerField(
                    help_text='Number of shared buffer-zone entities '
                              'used to fit C.',
                )),
                ('fit_residual', models.FloatField(
                    blank=True, null=True,
                    help_text='Frobenius norm of the fit residual '
                              '‖F_B - F_A Cᵀ‖_F / ‖F_B‖_F.',
                )),
                ('commutativity_residual', models.FloatField(
                    blank=True, null=True,
                    help_text='Laplacian commutativity residual '
                              '‖CΛ_A - Λ_B C‖_F / ‖Λ_A‖_F.',
                )),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'db_table': 'factor_subgraph_transport',
                'ordering': [
                    'snapshot_id', 'country_code',
                    'subgraph_from', 'subgraph_to',
                ],
                'unique_together': {
                    ('snapshot_id', 'country_code',
                     'subgraph_from', 'subgraph_to'),
                },
            },
        ),
        migrations.AddIndex(
            model_name='subgraphtransport',
            index=models.Index(
                fields=[
                    'snapshot_id', 'country_code',
                    'subgraph_from', 'subgraph_to',
                ],
                name='factor_transport_pair_idx',
            ),
        ),
    ]
