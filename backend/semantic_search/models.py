from django.db import models
import uuid


# ---------------------------------------------------------------------------
# Temporal diff tracker — records what changed between two snapshots
# ---------------------------------------------------------------------------

class SnapshotDiff(models.Model):
    """
    Records the embedding diff produced by one temporal snapshot update run.

    Created by SearchUpdateOrchestrator Step 6 after upserting new entities.
    Enables downstream consumers to know which entities changed and act accordingly.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    snapshot_id = models.UUIDField(
        db_index=True,
        help_text="UUID of the Snapshot that produced this diff"
    )
    prev_snapshot_id = models.UUIDField(
        null=True,
        blank=True,
        help_text="UUID of the previous Snapshot (None = first run)"
    )

    country_code = models.CharField(max_length=10, blank=True, default='')
    country_name = models.CharField(max_length=100, blank=True, default='')

    # Entity change counts
    entities_added = models.IntegerField(default=0,
        help_text="New OSM entity rows inserted")
    entities_modified = models.IntegerField(default=0,
        help_text="Existing entities with changed tags")
    entities_deleted = models.IntegerField(default=0,
        help_text="Entities present in prev_snapshot but absent in current")
    entities_re_embedded = models.IntegerField(default=0,
        help_text="Entities whose GV-Tags embedding was refreshed")

    # GV-NLE specifics
    gv_nle_inductive_count = models.IntegerField(default=0,
        help_text="Entities that received an inductive (provisional) GV-NLE embedding")
    gv_nle_retrained = models.BooleanField(default=False,
        help_text="True if full DeepWalk retraining was run this cycle")

    # Spatial links produced
    spatial_links_predicted = models.IntegerField(default=0,
        help_text="SpatialLink records created by USLP this cycle")

    # Entity alignment
    wikidata_links_added = models.IntegerField(default=0,
        help_text="OsmEntity.wikidata_uri fields updated by IGEA this cycle")

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'semantic_search_snapshotdiff'
        indexes = [
            models.Index(fields=['snapshot_id']),
            models.Index(fields=['-created_at']),
        ]
        ordering = ['-created_at']
        verbose_name = 'Snapshot Diff'
        verbose_name_plural = 'Snapshot Diffs'

    def __str__(self):
        return (f"Diff({self.country_code}) snap={self.snapshot_id} "
                f"+{self.entities_added}/-{self.entities_deleted}")


# ---------------------------------------------------------------------------
# Predicted spatial links (USLP output)
# ---------------------------------------------------------------------------

class SpatialLink(models.Model):
    """
    A predicted spatial entity link produced by SpatialLinkPredictionService (USLP).

    Represents the object-property triples missing from WorldKG v1.0:
        head_entity --[relation]--> tail_entity

    Example:
        wkg:Leicester  wkgs:isInCounty  wkg:Leicestershire   (confidence=0.84)

    These links can be promoted to WorldKG once verified, or used directly
    in downstream spatial queries via the pgvector search layer.
    """

    METHOD_CHOICES = [
        ('USLP', 'Unsupervised Spatial Link Prediction (USLP)'),
        ('SSLP', 'Supervised Spatial Link Prediction (SSLP)'),
        ('MANUAL', 'Manually verified'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    head_osm_id = models.BigIntegerField(
        db_index=True,
        help_text="OSM ID of the head (subject) entity"
    )
    relation_name = models.CharField(
        max_length=100,
        db_index=True,
        help_text="Predicted relation (e.g. 'isInCountry', 'addrCity')"
    )
    tail_osm_id = models.BigIntegerField(
        db_index=True,
        help_text="OSM ID of the predicted tail (object) entity"
    )
    literal_value = models.CharField(
        max_length=500,
        blank=True,
        default='',
        help_text="Original literal string from which the tail was predicted"
    )
    confidence = models.FloatField(
        help_text="USLP total score (geo + name + class spaces, range ~0–3)"
    )
    method = models.CharField(
        max_length=20,
        choices=METHOD_CHOICES,
        default='USLP',
    )
    source_snapshot_id = models.UUIDField(
        null=True,
        blank=True,
        db_index=True,
        help_text="Snapshot that produced this prediction"
    )
    verified = models.BooleanField(
        default=False,
        help_text="True after manual or automated verification"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'semantic_search_spatiallink'
        unique_together = [['head_osm_id', 'relation_name', 'tail_osm_id']]
        indexes = [
            models.Index(fields=['head_osm_id', 'relation_name']),
            models.Index(fields=['tail_osm_id']),
            models.Index(fields=['confidence']),
            models.Index(fields=['source_snapshot_id']),
            models.Index(fields=['verified']),
            models.Index(fields=['-created_at']),
        ]
        ordering = ['-confidence']
        verbose_name = 'Spatial Link'
        verbose_name_plural = 'Spatial Links'

    def __str__(self):
        return (f"SpatialLink({self.head_osm_id} "
                f"--[{self.relation_name}]--> {self.tail_osm_id} "
                f"@{self.confidence:.2f})")


# ---------------------------------------------------------------------------
# Graph spectral analysis (Step 5c / 5d — GRAPH_SPECTRAL_TEMPORAL_PLAN.md)
# ---------------------------------------------------------------------------

class GraphSpectralFingerprint(models.Model):
    """Spectral features of the k-NN graph per snapshot.

    Computed in pipeline Step 5c from the k-NN graph built in Step 5.
    Used by temporal drift analysis (Step 5d) and query-time spectral
    queries.

    References:
    - [COHEN:Ch13] — Eigendecomposition
    - [GRAPH_REP:Ch3] — Graph Laplacian, spectral features
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    region = models.CharField(
        max_length=255,
        db_index=True,
        help_text="Country code or subgraph slug for subdivision-scoped analysis",
    )
    snapshot = models.ForeignKey(
        'osmsnapshot.Snapshot',
        on_delete=models.CASCADE,
        related_name='spectral_fingerprints',
        help_text="Snapshot this fingerprint was computed from",
    )

    eigenvalues = models.JSONField(
        help_text="Top-k non-trivial eigenvalues of the normalized Laplacian "
                  "(λ₁..λₖ, sorted ascending, each ∈ [0, 2])"
    )
    fiedler_vector = models.JSONField(
        help_text="Fiedler vector (2nd eigenvector) — sampled/truncated for storage"
    )
    algebraic_connectivity = models.FloatField(
        help_text="λ₂ (Fiedler value) — how well-connected the graph is"
    )
    spectral_gap = models.FloatField(
        help_text="λₖ - λ₂ — reveals cluster structure"
    )
    signal_smoothness = models.FloatField(
        help_text="Dirichlet energy sᵀLs for the wkg_class graph signal"
    )
    node_count = models.IntegerField(help_text="Number of graph nodes")
    edge_count = models.IntegerField(help_text="Number of graph edges")
    k_eigenvalues = models.IntegerField(
        default=128,
        help_text="Number of eigenvalues computed",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'semantic_search_graphspectralfingerprint'
        unique_together = [('region', 'snapshot')]
        indexes = [
            models.Index(fields=['region', '-created_at']),
        ]
        ordering = ['region', '-created_at']
        verbose_name = 'Graph Spectral Fingerprint'
        verbose_name_plural = 'Graph Spectral Fingerprints'

    def __str__(self):
        return (
            f"SpectralFP({self.region}, λ₂={self.algebraic_connectivity:.6f}, "
            f"nodes={self.node_count})"
        )


class GraphSpectralDrift(models.Model):
    """Spectral drift between two snapshots.

    Computed in pipeline Step 5d when ≥2 snapshots exist for a country.
    Captures structural change (eigenvalue/Fiedler drift) and
    signal-weighted co-evolution (Dirichlet energy delta) plus optional
    ARIMA / exponential-smoothing forecasts.

    References:
    - [STATS:Ch3] — Spectral distance, KL divergence
    - [STATS:Ch6] — Time series, forecasting, change-point detection
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    region = models.CharField(
        max_length=255,
        db_index=True,
        help_text="Country code or subgraph slug for subdivision-scoped analysis",
    )
    snapshot_from = models.ForeignKey(
        'osmsnapshot.Snapshot',
        related_name='spectral_drift_from',
        on_delete=models.CASCADE,
        help_text="Earlier snapshot (T_{t-1})",
    )
    snapshot_to = models.ForeignKey(
        'osmsnapshot.Snapshot',
        related_name='spectral_drift_to',
        on_delete=models.CASCADE,
        help_text="Later snapshot (T_t)",
    )

    spectral_distance = models.FloatField(
        help_text="‖λ_t - λ_{t-1}‖₂ — structural change magnitude"
    )
    connectivity_delta = models.FloatField(
        help_text="Δλ₂ — algebraic connectivity shift"
    )
    spectral_gap_delta = models.FloatField(
        help_text="Δ(λₖ - λ₂) — spectral gap shift"
    )
    fiedler_drift = models.FloatField(
        help_text="Cosine distance between Fiedler vectors ∈ [0, 2]"
    )
    smoothness_delta = models.FloatField(
        help_text="Δ(sᵀLs) — Dirichlet energy shift"
    )
    drift_magnitude = models.CharField(
        max_length=10,
        default='low',
        help_text="Categorical magnitude: low / medium / high / extreme",
    )

    forecast_eigenvalues = models.JSONField(
        null=True, blank=True,
        help_text="ARIMA / exp-smoothing forecast of next-snapshot eigenvalues",
    )
    forecast_confidence = models.JSONField(
        null=True, blank=True,
        help_text="1.96σ prediction interval half-widths per eigenvalue",
    )
    changepoint_detected = models.BooleanField(
        default=False,
        help_text="True if CUSUM detected a structural change point",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'semantic_search_graphspectraldrift'
        unique_together = [('region', 'snapshot_from', 'snapshot_to')]
        indexes = [
            models.Index(fields=['region', '-created_at']),
        ]
        ordering = ['region', '-created_at']
        verbose_name = 'Graph Spectral Drift'
        verbose_name_plural = 'Graph Spectral Drifts'

    def __str__(self):
        return (
            f"SpectralDrift({self.region}, "
            f"dist={self.spectral_distance:.4f}, "
            f"Δλ₂={self.connectivity_delta:.4f})"
        )
