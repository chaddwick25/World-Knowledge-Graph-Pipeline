from django.db import models
from django.contrib.postgres.fields import ArrayField
from django.contrib.postgres.indexes import GinIndex
from pgvector.django import VectorField
from django.contrib.gis.db import models as gis_models
import uuid


class OsmEntity(models.Model):
    """
    Individual OSM entity (node, way, relation) with GeoVectors embeddings and WorldKG enrichment.
    
    This model stores:
    - Core OSM data (type, ID, tags, geometry)
    - GV-Tags embedding (semantic, 300D FastText)
    - GV-NLE embedding (spatial, 100D DeepWalk)
    - WorldKG ontological classification
    - Wikidata alignment via NCA
    
    Primary key: Composite of osm_type + osm_id + gv_tags_version
    Database: vectors (port 5433) via VectorDBRouter
    """
    
    OSM_TYPE_CHOICES = [
        ('node', 'Node'),
        ('way', 'Way'),
        ('relation', 'Relation'),
    ]
    
    # Composite primary key: osm_type + osm_id
    osm_type = models.CharField(
        max_length=10,
        choices=OSM_TYPE_CHOICES,
        help_text="OSM element type (node, way, relation)"
    )
    osm_id = models.BigIntegerField(
        help_text="OSM element ID"
    )
    
    # OSM tags
    tags = models.JSONField(
        help_text="OSM tags as key-value pairs, e.g., {'amenity': 'cafe', 'name': 'Starbucks'}"
    )

    # Romanized name for cross-script similarity search.
    # Populated by the romanize_names management command using the
    # RomanizerRegistry (auto-detects script from the text).  Used with
    # PostgreSQL pg_trgm GIN index for indexed similarity matching.
    # NULL for entities without a name= tag or before romanization.
    name_romanized = models.CharField(
        max_length=500,
        null=True,
        blank=True,
        help_text="Romanized name for cross-script similarity search. "
                  "Populated by romanize_names command. NULL before romanization."
    )

    # DUAL EMBEDDINGS: GV-Tags (semantic) + GV-NLE (spatial)
    # GV-Tags: 300D (FastText)  |  GV-NLE: 100D (GeoVectors-master implementation)
    
    # GV-Tags: Semantic embedding from FastText
    # Captures: "What you are" - functional similarity (cafe ≈ restaurant ≈ bar)
    # Formula: GV-Tags(entity) = (1/2|tags|) × Σ(FastText(key) + FastText(value))
    gv_tags_embedding = VectorField(
        dimensions=300,
        null=True,
        blank=True,
        help_text="GV-Tags: Semantic embedding from FastText (What you are)"
    )
    
    # GV-NLE: Spatial embedding from k-NN weighted DeepWalk
    # Captures: "Where you are" - geographic similarity (nearby entities similar)
    # Formula: k-NN graph (k=50) + weighted DeepWalk with w(o,o') = ln(1 + 1/distance)
    gv_nle_embedding = VectorField(
        dimensions=100,
        null=True,
        blank=True,
        help_text="GV-NLE: Spatial embedding from k-NN graph (Where you are)"
    )

    # Static embedding for ANN search (concatenated GV-Tags + GV-NLE, 300D + 100D = 400D)
    static_embedding = VectorField(
        dimensions=400,
        null=True,
        blank=True,
        help_text="Static embedding approximating fused geo+name+class score for ANN search (GV-Tags+GV-NLE)"
    )

    # WorldKG Semantic Type Assertions
    # Provides hierarchical ontological class from WorldKG knowledge graph
    # Enables subsumption queries and type-aware reasoning
    wkg_class = models.CharField(
        max_length=200,
        null=True,
        blank=True,
        help_text="WorldKG ontological class using wkgs: namespace (e.g., 'wkgs:Cafe', 'wkgs:Hospital'). "
                  "Corresponds to rdf:type assertion in WorldKG RDF triples."
    )
    wkg_superclasses = ArrayField(
        models.CharField(max_length=200),
        null=True,
        blank=True,
        help_text="WorldKG class hierarchy path using wkgs: namespace "
                  "(e.g., ['wkgs:Cafe', 'wkgs:Amenity', 'wkgs:WKGObject']). "
                  "Derived from rdfs:subClassOf chain in the WorldKG ontology."
    )
    wikidata_uri = models.CharField(
        max_length=200,
        null=True,
        blank=True,
        help_text="Wikidata equivalent class URI via NCA alignment (owl:equivalentClass on the class, "
                  "not owl:sameAs on the instance). E.g., 'http://www.wikidata.org/entity/Q11707'."
    )
    wkg_depth = models.IntegerField(
        null=True,
        blank=True,
        help_text="Depth in WorldKG ontology tree: 0=wkgs:WKGObject, 1=key class, 2=value subclass"
    )
    wkg_type_key = models.CharField(
        max_length=100,
        null=True,
        blank=True,
        help_text="OSM key that determined the WorldKG class assignment (e.g., 'amenity'). "
                  "Distinguishes type-asserting tags from property tags per WorldKG Section 4.2."
    )
    wkg_type_value = models.CharField(
        max_length=100,
        null=True,
        blank=True,
        help_text="OSM value that determined the WorldKG class assignment (e.g., 'restaurant'). "
                  "Null for top-level key classes (e.g., building=yes → wkgs:Building)."
    )
    wkg_enriched_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp when WorldKG enrichment was performed"
    )
    
    # Backward compatibility: alias for gv_tags_embedding
    @property
    def embedding(self):
        """Alias for gv_tags_embedding for backward compatibility."""
        return self.gv_tags_embedding

    @property
    def osm_node_uri(self) -> str:
        """Full OSM node URI as used in WorldKG wkgs:osmLink triples.

        Example: https://www.openstreetmap.org/node/1014675277
        Corresponds to the osmn: namespace in WorldKG RDF.
        """
        return f"https://www.openstreetmap.org/node/{self.osm_id}"
    
    # Optional geometry
    geom = gis_models.PointField(
        srid=4326,
        null=True,
        blank=True,
        help_text="WGS84 Point geometry (for nodes)"
    )
    
    # Metadata
    version = models.IntegerField(
        null=True,
        blank=True,
        help_text="OSM element version"
    )
    timestamp = models.DateTimeField(
        null=True,
        blank=True,
        help_text="OSM element timestamp"
    )
    
    # Embedding version tracking
    gv_tags_version = models.CharField(
        max_length=50,
        default='1.0',
        help_text="GV-Tags FastText model version"
    )
    gv_nle_version = models.CharField(
        max_length=50,
        null=True,
        blank=True,
        help_text="GV-NLE trained model version"
    )
    gv_nle_trained = models.BooleanField(
        default=False,
        help_text="Has GV-NLE embedding been computed?"
    )
    
    source_snapshot_id = models.UUIDField(
        null=True,
        blank=True,
        help_text="UUID of source Snapshot (cross-database reference)"
    )
    # ── Phase 6 partition keys (temporal + geographic) ──────────────────
    # These are nullable on the monolith and populated via backfill.
    # After cutover they become NOT NULL partition keys on the
    # partitioned table (embeddings_partitioned).
    # NOTE: source_snapshot_id (UUID FK) ≠ snapshot_id (VARCHAR partition key).
    #   source_snapshot_id = UUID of Snapshot row (default DB)
    #   snapshot_id         = human-readable 'YYYY_MM_DD' partition key
    snapshot_id = models.CharField(
        max_length=20,
        null=True,
        blank=True,
        help_text="Temporal partition key (YYYY_MM_DD, e.g. '2025_12_31'). "
                  "Derived from source_snapshot_id → Snapshot.snapshot_date."
    )
    country_code = models.CharField(
        max_length=3,
        null=True,
        blank=True,
        help_text="ISO 3166-1 alpha-2 country code (geographic partition key). "
                  "Populated via spatial join: geom → country bbox."
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'semantic_search_osmentity'
        # Phase 6 cutover: widened to include partition keys (snapshot_id,
        # country_code).  The monolith's old constraint was
        # (osm_type, osm_id, gv_tags_version).  Migration 0009 applies this
        # change — it must only be run AFTER the table rename cutover (see
        # PHASE6_OSMID_AUDIT_AND_CUTOVER_PLAN.md Step 4).
        unique_together = [['osm_type', 'osm_id', 'gv_tags_version', 'snapshot_id', 'country_code']]
        indexes = [
            models.Index(fields=['osm_type', 'osm_id']),
            models.Index(fields=['created_at']),
            models.Index(fields=['source_snapshot_id', 'gv_nle_trained']),
            models.Index(fields=['wkg_class']),
            models.Index(fields=['wikidata_uri']),
            models.Index(fields=['wkg_depth']),
            models.Index(fields=['wkg_type_key', 'wkg_type_value']),
            # Phase 6 partition-key indexes (added for snapshot/country scoping)
            models.Index(fields=['snapshot_id']),
            models.Index(fields=['country_code']),
            models.Index(fields=['snapshot_id', 'country_code']),
            # GIN index on tags jsonb — accelerates tags ? 'key' and
            # tags ?| ARRAY[...] queries used by USLP head filtering and
            # semantic search tag matching.  On CA (53M rows) this drops
            # tag-existence queries from ~85s seq scan to <100ms.
            GinIndex(
                fields=['tags'],
                name='osmentity_tags_gin_idx',
                opclasses=['jsonb_path_ops'],
            ),
        ]
        ordering = ['osm_type', 'osm_id']
        verbose_name = 'OSM Entity'
        verbose_name_plural = 'OSM Entities'
    
    def __str__(self):
        name = self.tags.get('name', self.tags.get('amenity', self.tags.get('highway', 'unnamed')))
        return f"{self.osm_type}/{self.osm_id}: {name}"
    
    @property
    def osm_url(self):
        """Return OpenStreetMap URL for this entity."""
        return f"https://www.openstreetmap.org/{self.osm_type}/{self.osm_id}"
    
    @property
    def source_snapshot(self):
        """Resolve cross-database reference to Snapshot (default DB)."""
        if self.source_snapshot_id:
            from osmsnapshot.models import Snapshot
            return Snapshot.objects.using('default').filter(id=self.source_snapshot_id).first()
        return None
    
    @classmethod
    def create_from_osm(cls, osm_type, osm_id, tags, gv_tags_embedding=None, gv_nle_embedding=None, 
                        geom=None, version=None, timestamp=None):
        """
        Factory method to create OsmEntity from OSM data with dual embeddings.
        
        Args:
            osm_type: 'node', 'way', or 'relation'
            osm_id: OSM element ID
            tags: Dictionary of OSM tags
            gv_tags_embedding: GV-Tags embedding (semantic from FastText) - 300D numpy array
            gv_nle_embedding: GV-NLE embedding (spatial from k-NN graph) - 100D numpy array (optional)
            geom: Optional Point geometry
            version: OSM version number
            timestamp: OSM timestamp
        
        Returns:
            OsmEntity instance with dual embeddings
        """
        # Convert numpy arrays to lists for pgvector
        if gv_tags_embedding is not None:
            gv_tags_embedding = gv_tags_embedding.tolist() if hasattr(gv_tags_embedding, 'tolist') else gv_tags_embedding
        
        if gv_nle_embedding is not None:
            gv_nle_embedding = gv_nle_embedding.tolist() if hasattr(gv_nle_embedding, 'tolist') else gv_nle_embedding
        
        return cls(
            osm_type=osm_type,
            osm_id=osm_id,
            tags=tags,
            gv_tags_embedding=gv_tags_embedding,
            gv_nle_embedding=gv_nle_embedding,
            gv_nle_trained=(gv_nle_embedding is not None),
            geom=geom,
            version=version,
            timestamp=timestamp
        )


# ---------------------------------------------------------------------------
# Factor-node metric tables (docs/plans/FACTOR_NODE_RUNTIME_JOINS_PLAN.md)
#
# Per-entity, snapshot-pinned metrics written by batch pipeline steps
# (5c/5d) and consumed at query time via SQL joins — the materialized
# "factor nodes" of the Spatial-Agent paper's factorized GeoFlow Graph G′
# (§3.3).  All tables live on the ``vectors`` DB so they can join
# ``OsmEntity`` without FDW.
#
# Key convention: the k-NN graph's node keyspace is ``osm_id`` only
# (KNNGraphService.build_graph), so these tables key on
# (snapshot_id, country_code, osm_id) — no osm_type.
# ---------------------------------------------------------------------------

# Fixed width of the eigen_loadings vector column.  Small countries compute
# K=128 non-trivial eigenvectors; large countries (≥500k nodes) compute
# K=64 and are zero-padded to 128 (both vectors share the padding, so
# pgvector inner products remain correct).
EIGEN_LOADING_DIM = 128


class SpectralNodeMetric(models.Model):
    """Per-entity spectral + structural metrics for one snapshot (Step 5c).

    One row per (snapshot_id, country_code, osm_id).  ``eigen_loadings``
    holds the node's row of the eigenvector matrix Φ (φ₁..φ_K, zero-padded
    to EIGEN_LOADING_DIM); combined with the eigenvalues stored on
    ``GraphSpectralFingerprint`` it turns heat-kernel diffusion into a
    single pgvector inner-product query:

        score(node) = Σ_k e^{-t·λ_k} · φ_k(anchor) · φ_k(node)

    IMPORTANT: eigen_loadings are coordinates in *this snapshot's*
    eigenbasis — never mix rows across snapshot_id in one vector operation.

    References:
    - [COHEN:Ch13] — Eigendecomposition
    - [GRAPH_REP:Ch3] — Graph Laplacian, spectral features
    - [SPATIAL_AGENT:§3.3] — factor nodes (materialized as rows)
    """

    id = models.BigAutoField(primary_key=True)

    snapshot_id = models.CharField(
        max_length=20,
        help_text="Snapshot date (YYYY_MM_DD) — matches OsmEntity.snapshot_id",
    )
    country_code = models.CharField(
        max_length=3,
        help_text="ISO 3166-1 alpha-2 country code",
    )
    osm_id = models.BigIntegerField(
        help_text="OSM element ID (k-NN graph node key)",
    )
    subgraph_slug = models.CharField(
        max_length=255, null=True, blank=True, db_index=True,
        help_text="Subgraph slug for subdivision-scoped spectral analysis. "
                  "Null for country-level (small territories).",
    )

    eigen_loadings = VectorField(
        dimensions=EIGEN_LOADING_DIM,
        null=True, blank=True,
        help_text="Node's row of Φ (φ₁..φ_K, zero-padded to 128)",
    )
    fiedler_component = models.FloatField(
        null=True, blank=True,
        help_text="φ₂(node) — duplicated from eigen_loadings[0] for cheap "
                  "scalar filtering/sorting without vector ops",
    )
    louvain_community = models.IntegerField(
        null=True, blank=True,
        help_text="Louvain community ID (CommunityDetectionService, Step 5c)",
    )
    dirichlet_contrib = models.FloatField(
        null=True, blank=True,
        help_text="Per-node local Dirichlet term s_i·(Ls)_i for the "
                  "wkg_class graph signal",
    )

    # Structural metrics (derived from the same k-NN graph)
    degree = models.IntegerField(
        null=True, blank=True,
        help_text="Node degree in the k-NN graph",
    )
    clustering_coeff = models.FloatField(
        null=True, blank=True,
        help_text="Local clustering coefficient",
    )
    component_id = models.IntegerField(
        null=True, blank=True,
        help_text="Connected-component membership (0-indexed, arbitrary)",
    )
    component_size = models.IntegerField(
        null=True, blank=True,
        help_text="Size of the node's connected component",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'factor_spectral_node_metric'
        unique_together = [['snapshot_id', 'country_code', 'subgraph_slug', 'osm_id']]
        indexes = [
            models.Index(fields=['snapshot_id', 'country_code', 'louvain_community'],
                         name='factor_spec_community_idx'),
            # Composite index for subgraph-scoped pgvector <#> queries —
            # lets the planner prune to the subgraph before the distance scan.
            models.Index(
                fields=['snapshot_id', 'country_code', 'subgraph_slug'],
                name='factor_spec_subgraph_idx',
            ),
        ]
        ordering = ['snapshot_id', 'country_code', 'osm_id']

    def __str__(self):
        return (f"SpectralNodeMetric({self.country_code}/{self.snapshot_id}/"
                f"{self.osm_id})")


class DriftNodeMetric(models.Model):
    """Per-entity spectral drift between two snapshots (Step 5d).

    Keyed by the snapshot *pair* (snapshot_from_id, snapshot_to_id) plus
    country and osm_id.  Eigenvector signs are aligned across the pair
    before deltas are computed (eigsh signs are indeterminate per run).

    References:
    - [STATS:Ch3] — distributional drift
    - [SPATIAL_AGENT:§3.3] — factor nodes (materialized as rows)
    """

    id = models.BigAutoField(primary_key=True)

    country_code = models.CharField(
        max_length=3,
        help_text="ISO 3166-1 alpha-2 country code",
    )
    snapshot_from_id = models.CharField(
        max_length=20,
        help_text="Earlier snapshot date (YYYY_MM_DD)",
    )
    snapshot_to_id = models.CharField(
        max_length=20,
        help_text="Later snapshot date (YYYY_MM_DD)",
    )
    osm_id = models.BigIntegerField(
        help_text="OSM element ID (present in both snapshots)",
    )

    fiedler_delta = models.FloatField(
        null=True, blank=True,
        help_text="φ₂(t) − φ₂(t−1) after eigenvector sign alignment",
    )
    loading_drift = models.FloatField(
        null=True, blank=True,
        help_text="Cosine distance between sign-aligned eigen-loading "
                  "vectors across the pair ∈ [0, 2]",
    )
    community_changed = models.BooleanField(
        null=True, blank=True,
        help_text="Louvain community membership changed across the pair",
    )
    degree_delta = models.IntegerField(
        null=True, blank=True,
        help_text="degree(t) − degree(t−1) in the k-NN graph",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'factor_drift_node_metric'
        unique_together = [[
            'country_code', 'snapshot_from_id', 'snapshot_to_id', 'osm_id',
        ]]
        indexes = [
            models.Index(fields=['country_code', 'snapshot_from_id', 'snapshot_to_id'],
                         name='factor_drift_pair_idx'),
        ]
        ordering = ['country_code', 'snapshot_from_id', 'snapshot_to_id', 'osm_id']

    def __str__(self):
        return (f"DriftNodeMetric({self.country_code} "
                f"{self.snapshot_from_id}→{self.snapshot_to_id}/{self.osm_id})")


class AmenityEmbedding(models.Model):
    """Precomputed FastText embedding for a MapQA amenity vocabulary entry.

    Removes the last runtime FastText call from the executor's semantic
    fallback tier: the query-side embedding of a known amenity string
    becomes a plain row lookup.  Written by
    ``python manage.py compute_amenity_embeddings``.
    """

    id = models.BigAutoField(primary_key=True)
    amenity_text = models.CharField(
        max_length=200, unique=True,
        help_text="Amenity value from the MapQA vocabulary (lowercased)",
    )
    embedding = VectorField(
        dimensions=300,
        help_text="FastText 300D embedding (L2-normalized, GeoVectors space)",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'factor_amenity_embedding'
        ordering = ['amenity_text']

    def __str__(self):
        return f"AmenityEmbedding({self.amenity_text})"


class PrecomputedLinkCandidate(models.Model):
    country_code = models.CharField(max_length=10, blank=True, default='', db_index=True)
    osm_type = models.CharField(max_length=10, choices=OsmEntity.OSM_TYPE_CHOICES)
    osm_id = models.BigIntegerField()
    projection_asset_id = models.UUIDField(null=True, blank=True)
    candidates = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'worldkg_precomputed_link_candidate'
        unique_together = [['osm_type', 'osm_id', 'projection_asset_id']]
        indexes = [
            models.Index(fields=['country_code']),
            models.Index(fields=['osm_type', 'osm_id']),
        ]

    def __str__(self):
        return f"{self.osm_type}/{self.osm_id} ({self.country_code})"


class SubgraphTransport(models.Model):
    """Functional map matrix between two adjacent subgraph eigenbases.

    Computed at batch time (Step 5c Phase B) from shared buffer-zone
    entities.  Used at runtime by ``FactorResolutionService.diffusion_rank``
    to transport eigen-loadings across subgraph boundaries for cross-
    subgraph spectral diffusion.

    The k×k matrix C satisfies ``C = Φ_Bᵀ S Φ_A`` (Ovsjanikov et al. 2012),
    where S is the node-to-node correspondence matrix on shared buffer-zone
    entities, and Φ_A, Φ_B are the truncated eigenbases of subgraphs A and B.
    Computed via regularized least squares with a Laplacian commutativity
    regularizer (GRASP; Behmanesh et al. ICML 2026).

    Grounded in:
    - Ovsjanikov et al. 2012 — functional maps framework
    - Pegoraro et al. 2023 — spectral maps for graphs/subgraphs
    - See ``docs/plans/SUBGRAPH_FUNCTIONAL_MAPS_PLAN_v2.md``
    """

    id = models.BigAutoField(primary_key=True)

    snapshot_id = models.CharField(
        max_length=20, db_index=True,
        help_text="Snapshot date (YYYY_MM_DD) — matches OsmEntity.snapshot_id",
    )
    country_code = models.CharField(
        max_length=3, db_index=True,
        help_text="ISO 3166-1 alpha-2 country code",
    )
    subgraph_from = models.CharField(
        max_length=255, db_index=True,
        help_text="Source subgraph slug (eigenbasis to transport FROM)",
    )
    subgraph_to = models.CharField(
        max_length=255, db_index=True,
        help_text="Target subgraph slug (eigenbasis to transport TO)",
    )
    transport_matrix = models.JSONField(
        help_text="k×k matrix (list of lists).  Transports eigen-loadings "
                  "from subgraph_from's eigenbasis to subgraph_to's "
                  "eigenbasis: loadings_to = C · loadings_from."
    )
    k_dim = models.IntegerField(
        help_text="Dimension of the transport matrix (k×k).",
    )
    shared_entity_count = models.IntegerField(
        help_text="Number of shared buffer-zone entities used to fit C.",
    )
    fit_residual = models.FloatField(
        null=True, blank=True,
        help_text="Frobenius norm of the fit residual "
                  "‖F_B - F_A Cᵀ‖_F / ‖F_B‖_F.",
    )
    commutativity_residual = models.FloatField(
        null=True, blank=True,
        help_text="Laplacian commutativity residual "
                  "‖CΛ_A - Λ_B C‖_F / ‖Λ_A‖_F.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'factor_subgraph_transport'
        unique_together = (
            'snapshot_id', 'country_code', 'subgraph_from', 'subgraph_to',
        )
        indexes = [
            models.Index(
                fields=[
                    'snapshot_id', 'country_code',
                    'subgraph_from', 'subgraph_to',
                ],
                name='factor_transport_pair_idx',
            ),
        ]
        ordering = [
            'snapshot_id', 'country_code', 'subgraph_from', 'subgraph_to',
        ]

    def __str__(self):
        return (f"SubgraphTransport({self.country_code}/"
                f"{self.snapshot_id} {self.subgraph_from}→{self.subgraph_to})")

