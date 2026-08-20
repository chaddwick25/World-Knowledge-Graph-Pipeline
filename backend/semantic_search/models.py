from django.db import models
from django.contrib.postgres.fields import ArrayField
from pgvector.django import VectorField
from django.contrib.gis.db import models as gis_models
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


class ProjectionTrainingPair(models.Model):
    """Training pairs for projection head knowledge distillation."""
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tag_counts = models.JSONField(
        help_text="OSM tag counts, e.g., {'cafe': 40, 'residential': 30}"
    )
    query_text = models.TextField(
        help_text="Natural language query generated from tags"
    )
    teacher_embedding = VectorField(
        dimensions=300,
        help_text="FastText weighted average embedding (teacher)"
    )
    ground_truth_tract = models.ForeignKey(
        'CensusTract',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        help_text="Known relevant census tract for this query"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        db_table = 'semantic_search_projectiontrainingpair'
        indexes = [
            models.Index(fields=['created_at']),
        ]
        verbose_name = 'Projection Training Pair'
        verbose_name_plural = 'Projection Training Pairs'
    
    def __str__(self):
        return f"TrainingPair {self.id}: {self.query_text[:50]}"


class ProjectionHeadCheckpoint(models.Model):
    """Saved checkpoints of trained projection head models."""
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    version = models.CharField(
        max_length=50,
        help_text="Model version identifier, e.g., 'v1.0'"
    )
    model_state = models.BinaryField(
        help_text="PyTorch state_dict serialized as bytes"
    )
    optimizer_state = models.BinaryField(
        null=True,
        blank=True,
        help_text="Optimizer state_dict for resuming training"
    )
    epoch = models.IntegerField(
        help_text="Training epoch when checkpoint was saved"
    )
    train_loss = models.FloatField(
        help_text="Training loss at checkpoint"
    )
    val_loss = models.FloatField(
        null=True,
        blank=True,
        help_text="Validation loss at checkpoint"
    )
    config = models.JSONField(
        help_text="Model configuration (architecture, hyperparameters)"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        db_table = 'semantic_search_projectionheadcheckpoint'
        indexes = [
            models.Index(fields=['version', '-epoch']),
            models.Index(fields=['-created_at']),
        ]
        ordering = ['-created_at']
        verbose_name = 'Projection Head Checkpoint'
        verbose_name_plural = 'Projection Head Checkpoints'
    
    def __str__(self):
        return f"{self.version} Epoch {self.epoch}: Loss {self.train_loss:.4f}"


class EmbeddingComparison(models.Model):
    """Comparison results between FastText baseline and hidden state projection."""
    
    METHOD_CHOICES = [
        ('fasttext_baseline', 'FastText Weighted Average'),
        ('hidden_state_projection', 'DistilGPT2 Hidden State + Projection'),
    ]
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    test_name = models.CharField(
        max_length=255,
        help_text="Name of the test or benchmark"
    )
    method = models.CharField(
        max_length=50,
        choices=METHOD_CHOICES,
        help_text="Embedding method used"
    )
    query_tags = models.JSONField(
        help_text="Tag counts used for the query"
    )
    query_text = models.TextField(
        null=True,
        blank=True,
        help_text="Natural language query (for GPT methods)"
    )
    
    # Performance metrics
    inference_time_ms = models.FloatField(
        help_text="Inference time in milliseconds"
    )
    memory_mb = models.FloatField(
        null=True,
        blank=True,
        help_text="CPU memory usage in MB"
    )
    gpu_memory_mb = models.FloatField(
        null=True,
        blank=True,
        help_text="GPU memory usage in MB"
    )
    
    # Retrieval quality
    top_5_results = models.JSONField(
        help_text="Top 5 retrieved census tracts with scores"
    )
    top_1_similarity = models.FloatField(
        help_text="Similarity score of top-1 result"
    )
    recall_at_5 = models.FloatField(
        null=True,
        blank=True,
        help_text="Recall@5 metric"
    )
    recall_at_10 = models.FloatField(
        null=True,
        blank=True,
        help_text="Recall@10 metric"
    )
    mrr = models.FloatField(
        null=True,
        blank=True,
        help_text="Mean Reciprocal Rank"
    )
    ndcg_at_10 = models.FloatField(
        null=True,
        blank=True,
        help_text="Normalized Discounted Cumulative Gain @10"
    )
    
    # Agreement with baseline
    overlap_at_5 = models.FloatField(
        null=True,
        blank=True,
        help_text="Jaccard overlap with FastText top-5 results"
    )
    overlap_at_10 = models.FloatField(
        null=True,
        blank=True,
        help_text="Jaccard overlap with FastText top-10 results"
    )
    rank_correlation = models.FloatField(
        null=True,
        blank=True,
        help_text="Spearman's rank correlation with FastText"
    )
    
    timestamp = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        db_table = 'semantic_search_embeddingcomparison'
        indexes = [
            models.Index(fields=['method', 'test_name']),
            models.Index(fields=['-timestamp']),
        ]
        ordering = ['-timestamp']
        verbose_name = 'Embedding Comparison'
        verbose_name_plural = 'Embedding Comparisons'
    
    def __str__(self):
        return f"{self.method} - {self.test_name} @ {self.timestamp.strftime('%Y-%m-%d %H:%M')}"


class TrainingMetrics(models.Model):
    """Training metrics logged during projection head training."""
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    checkpoint = models.ForeignKey(
        ProjectionHeadCheckpoint,
        on_delete=models.CASCADE,
        related_name='training_metrics',
        help_text="Associated checkpoint"
    )
    epoch = models.IntegerField(
        help_text="Training epoch"
    )
    batch_idx = models.IntegerField(
        help_text="Batch index within epoch"
    )
    
    # Loss components
    loss_total = models.FloatField(
        help_text="Total combined loss"
    )
    loss_alignment = models.FloatField(
        help_text="Alignment loss component"
    )
    loss_contrastive = models.FloatField(
        help_text="Contrastive (InfoNCE) loss component"
    )
    loss_rank_distillation = models.FloatField(
        null=True,
        blank=True,
        help_text="Rank distillation loss component"
    )
    
    # Learning metrics
    learning_rate = models.FloatField(
        help_text="Current learning rate"
    )
    gradient_norm = models.FloatField(
        null=True,
        blank=True,
        help_text="Gradient norm (for monitoring)"
    )
    
    timestamp = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        db_table = 'semantic_search_trainingmetrics'
        indexes = [
            models.Index(fields=['checkpoint', 'epoch', 'batch_idx']),
            models.Index(fields=['-timestamp']),
        ]
        ordering = ['checkpoint', 'epoch', 'batch_idx']
        verbose_name = 'Training Metric'
        verbose_name_plural = 'Training Metrics'
    
    def __str__(self):
        return f"Epoch {self.epoch} Batch {self.batch_idx}: Loss {self.loss_total:.4f}"


# OsmEntity model has been moved to worldkg_nca app
# Import it from there if needed:
# from worldkg_nca.models import OsmEntity

class CensusTract(gis_models.Model):
    """
    Represents a Census Tract with its geographical boundaries and a 
    purely semantic 300D GeoVector embedding representing its tag makeup.
    
    Follows Geofabrik naming convention: country/region/tract
    """
    geouid = gis_models.CharField(
        max_length=100,
        unique=True,
        db_index=True,
        help_text="Geographic UID following Geofabrik hierarchy: country-region-tract"
    )
    name = gis_models.CharField(
        max_length=200,
        help_text="Human-readable name (e.g., 'Toronto Downtown Core')"
    )
    
    # Geofabrik-style hierarchy (matches polygon file structure)
    country = gis_models.CharField(
        max_length=100,
        db_index=True,
        help_text="Country name (e.g., 'canada', 'united-states')"
    )
    region = gis_models.CharField(
        max_length=100,
        db_index=True,
        null=True,
        blank=True,
        help_text="Region/state name (e.g., 'ontario', 'california')"
    )
    
    # PostGIS Field for spatial clipping
    geom = gis_models.PolygonField(
        srid=4326,
        help_text="WGS84 Polygon representing tract boundaries"
    )
    
    # pgvector Field for semantic search (300D FastText GeoVector)
    semantic_embedding = VectorField(
        dimensions=300,
        help_text="L2-Normalized Weighted Average of OSM GeoVectors"
    )
    
    # Metadata
    created_at = gis_models.DateTimeField(auto_now_add=True)
    updated_at = gis_models.DateTimeField(auto_now=True)
    embedding_version = gis_models.CharField(
        max_length=20,
        default='1.0',
        help_text="Version of embedding algorithm used"
    )

    class Meta:
        db_table = 'semantic_search_censustract'
        indexes = [
            gis_models.Index(fields=['country', 'region']),
            gis_models.Index(fields=['geouid']),
        ]
        ordering = ['country', 'region', 'geouid']
        verbose_name = 'Census Tract'
        verbose_name_plural = 'Census Tracts'
    
    def __str__(self):
        """String representation with Geofabrik hierarchy."""
        if self.region:
            return f"{self.country}/{self.region} - {self.geouid}"
        return f"{self.country} - {self.geouid}"

class DriftScore(gis_models.Model):
    """
    Stores the results of the amenity drift monitor.
    Tracks temporal changes in neighborhood characteristics.
    """
    census_tract = gis_models.ForeignKey(
        CensusTract,
        on_delete=gis_models.CASCADE,
        related_name='drift_scores'
    )
    timestamp = gis_models.DateTimeField(auto_now_add=True)
    drift_score = gis_models.FloatField(
        help_text="Cosine distance from base amenity vector (0=identical, 1=opposite)"
    )
    is_alert = gis_models.BooleanField(
        default=False,
        help_text="True if drift exceeded threshold (default: 0.10)"
    )
    amenity_profile = gis_models.JSONField(
        default=dict,
        help_text="Current amenity tag counts at time of measurement"
    )

    class Meta:
        db_table = 'semantic_search_driftscore'
        indexes = [
            gis_models.Index(fields=['census_tract', '-timestamp']),
            gis_models.Index(fields=['is_alert', '-timestamp']),
        ]
        ordering = ['-timestamp']
        verbose_name = 'Drift Score'
        verbose_name_plural = 'Drift Scores'

    def __str__(self):
        alert_flag = "🚨" if self.is_alert else "✓"
        return f"{alert_flag} {self.census_tract.geouid} @ {self.timestamp.strftime('%Y-%m-%d')}: {self.drift_score:.4f}"


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
