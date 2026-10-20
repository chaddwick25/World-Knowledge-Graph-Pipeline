from django.db import models
import uuid


class EntityAlignment(models.Model):
    """
    Tracks OSM → Wikidata entity alignments from IGEA.
    
    Preserves alignment provenance and confidence scores.
    Per Dsouza et al., ISWC 2023 (https://arxiv.org/abs/2105.00847)
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    
    # OSM entity reference (cross-database FK to worldkg_nca.OsmEntity)
    osm_type = models.CharField(max_length=10, db_index=True)
    osm_id = models.BigIntegerField(db_index=True)
    
    # Wikidata alignment
    wikidata_uri = models.CharField(max_length=200, db_index=True)
    wikidata_label = models.CharField(max_length=500, null=True, blank=True)
    
    # Alignment metadata
    alignment_method = models.CharField(
        max_length=20,
        choices=[
            ('SEED', 'Seed (wikidata= tag)'),
            ('COSINE', 'Cosine similarity baseline'),
            ('CROSS_ATTN', 'BiLSTM cross-attention'),
        ],
        help_text="Method used for alignment"
    )
    confidence = models.FloatField(
        help_text="Alignment confidence score (0.0-1.0)"
    )
    iteration = models.IntegerField(
        default=0,
        help_text="IGEA iteration when alignment was accepted"
    )
    
    # Spatial validation
    distance_meters = models.FloatField(
        null=True,
        blank=True,
        help_text="Haversine distance between OSM and Wikidata coordinates"
    )
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'igea_entity_alignment'
        unique_together = [['osm_type', 'osm_id', 'wikidata_uri']]
        indexes = [
            models.Index(fields=['osm_type', 'osm_id']),
            models.Index(fields=['wikidata_uri']),
            models.Index(fields=['alignment_method', 'confidence']),
            models.Index(fields=['created_at']),
        ]
        ordering = ['-confidence', '-created_at']
    
    def __str__(self):
        return f"{self.osm_type}/{self.osm_id} → {self.wikidata_uri} ({self.confidence:.2f})"


class SpatialTripletScore(models.Model):
    """
    USLP tri-space scores for spatial link prediction.
    
    Stores decomposed scores: geo + name + topo = total
    Per Mann et al., ISWC 2023 Section 3.3 (https://arxiv.org/abs/2304.09503)
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    
    # Head entity (cross-database FK to worldkg_nca.OsmEntity)
    head_osm_type = models.CharField(max_length=10, db_index=True)
    head_osm_id = models.BigIntegerField(db_index=True)
    
    # Tail entity (cross-database FK to worldkg_nca.OsmEntity)
    tail_osm_type = models.CharField(max_length=10, db_index=True)
    tail_osm_id = models.BigIntegerField(db_index=True)
    
    # Relation
    relation = models.CharField(
        max_length=50,
        db_index=True,
        help_text="Spatial relation (e.g., isInCounty, addrSuburb)"
    )
    
    # USLP tri-space scores (Section 3.3)
    geo_score = models.FloatField(
        help_text="Geohash distance score (precision by relation)"
    )
    name_score = models.FloatField(
        help_text="FastText cosine similarity (literal vs candidate name)"
    )
    topo_score = models.FloatField(
        help_text="TransE topological constraint score"
    )
    unnormalized_score = models.FloatField(
        db_index=True,
        default=0.0,
        help_text="Raw sum of geo + name + topo scores (range [0, 3.0])"
    )
    normalized_score = models.FloatField(
        db_index=True,
        default=0.0,
        help_text="Normalized score = unnormalized_score / 3.0 (range [0, 1.0])"
    )
    
    # Acceptance
    predicted = models.BooleanField(
        default=False,
        help_text="True if normalized_score >= ACCEPTANCE_THRESHOLD (0.7)"
    )
    
    # Metadata
    geohash_precision = models.IntegerField(
        help_text="Geohash precision used for geo_score"
    )
    snapshot_id = models.CharField(
        max_length=20,
        null=True,
        blank=True,
        db_index=True,
        help_text="Snapshot date (YYYY_MM_DD) these links were generated from. "
                  "Matches OsmEntity.snapshot_id for partition-scoped queries."
    )
    country_name = models.CharField(
        max_length=100,
        null=True,
        blank=True,
        db_index=True,
        help_text="Country name for this link (self-describing, independent of snapshot_id)"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'igea_spatial_triplet_score'
        unique_together = [['head_osm_type', 'head_osm_id', 'relation', 'tail_osm_type', 'tail_osm_id', 'snapshot_id', 'country_name']]
        indexes = [
            models.Index(fields=['head_osm_type', 'head_osm_id']),
            models.Index(fields=['tail_osm_type', 'tail_osm_id']),
            models.Index(fields=['relation', 'unnormalized_score']),
            models.Index(fields=['normalized_score']),
            models.Index(fields=['predicted']),
            models.Index(fields=['snapshot_id']),
            models.Index(fields=['country_name']),
            # Phase 3 of USLP data flow: Composite index for dashboard queries
            # +--------+---------------------------------------------------------------+----------------------------------------------------------+
            # | Phase  | What happens                                                  | Where                                                    |
            # +========+===============================================================+==========================================================+
            # | 3      | Composite index on (country_name, snapshot_id, predicted)    | Migration igea.0003_spatialtripletscore_igea_triplet_csp |
            # |        | enables Index Only Scan for dashboard WHERE clause           | _idx on vectors DB                                       |
            # +--------+---------------------------------------------------------------+----------------------------------------------------------+
            # Used by AugmentedDataService.get_summary() filter():
            #   filter(country_name=..., snapshot_id=..., predicted=True)
            models.Index(
                fields=['country_name', 'snapshot_id', 'predicted'],
                name='igea_triplet_csp_idx'
            ),
        ]
        ordering = ['-normalized_score']
    
    def __str__(self):
        return f"({self.head_osm_type}/{self.head_osm_id}) --{self.relation}-> ({self.tail_osm_type}/{self.tail_osm_id}) [raw={self.unnormalized_score:.2f}, norm={self.normalized_score:.2f}]"


class SpatialTripletScoreRejected(models.Model):
    """
    USLP tri-space scores for spatial link prediction - REJECTED links (below threshold).

    Stores decomposed scores: geo + name + topo = total
    Per Mann et al., ISWC 2023 Section 3.3 (https://arxiv.org/abs/2304.09503)

    This table stores links that did not meet the acceptance threshold.
    Separated from SpatialTripletScore for cleaner data management and analysis.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Head entity (cross-database FK to worldkg_nca.OsmEntity)
    head_osm_type = models.CharField(max_length=10, db_index=True)
    head_osm_id = models.BigIntegerField(db_index=True)

    # Tail entity (cross-database FK to worldkg_nca.OsmEntity)
    tail_osm_type = models.CharField(max_length=10, db_index=True)
    tail_osm_id = models.BigIntegerField(db_index=True)

    # Relation
    relation = models.CharField(
        max_length=50,
        db_index=True,
        help_text="Spatial relation (e.g., isInCounty, addrSuburb)"
    )

    # USLP tri-space scores (Section 3.3)
    geo_score = models.FloatField(
        help_text="Geohash distance score (precision by relation)"
    )
    name_score = models.FloatField(
        help_text="FastText cosine similarity (literal vs candidate name)"
    )
    topo_score = models.FloatField(
        help_text="TransE topological constraint score"
    )
    unnormalized_score = models.FloatField(
        db_index=True,
        default=0.0,
        help_text="Raw sum of geo + name + topo scores (range [0, 3.0])"
    )
    normalized_score = models.FloatField(
        db_index=True,
        default=0.0,
        help_text="Normalized score = unnormalized_score / 3.0 (range [0, 1.0])"
    )

    # Metadata
    geohash_precision = models.IntegerField(
        help_text="Geohash precision used for geo_score"
    )
    snapshot_id = models.CharField(
        max_length=20,
        null=True,
        blank=True,
        db_index=True,
        help_text="Snapshot date (YYYY_MM_DD) these links were generated from. "
                  "Matches OsmEntity.snapshot_id for partition-scoped queries."
    )
    country_name = models.CharField(
        max_length=100,
        null=True,
        blank=True,
        db_index=True,
        help_text="Country name for this link (self-describing, independent of snapshot_id)"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'igea_spatial_triplet_score_rejected'
        unique_together = [['head_osm_type', 'head_osm_id', 'relation', 'tail_osm_type', 'tail_osm_id', 'snapshot_id', 'country_name']]
        indexes = [
            models.Index(fields=['head_osm_type', 'head_osm_id']),
            models.Index(fields=['tail_osm_type', 'tail_osm_id']),
            models.Index(fields=['relation', 'unnormalized_score']),
            models.Index(fields=['normalized_score']),
            models.Index(fields=['snapshot_id']),
            models.Index(fields=['country_name']),
        ]
        ordering = ['-normalized_score']

    def __str__(self):
        return f"({self.head_osm_type}/{self.head_osm_id}) --{self.relation}-> ({self.tail_osm_type}/{self.tail_osm_id}) [raw={self.unnormalized_score:.2f}, norm={self.normalized_score:.2f}] (REJECTED)"
