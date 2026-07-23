from django.db import models
from django.contrib.postgres.fields import ArrayField
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
        db_index=True,
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
        db_index=True,
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
        db_index=True,
        help_text="UUID of source TemporalSnapshot (cross-database reference)"
    )
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'semantic_search_osmentity'
        unique_together = [['osm_type', 'osm_id', 'gv_tags_version']]
        indexes = [
            models.Index(fields=['osm_type', 'osm_id']),
            models.Index(fields=['created_at']),
            models.Index(fields=['source_snapshot_id', 'gv_nle_trained']),
            models.Index(fields=['wkg_class']),
            models.Index(fields=['wikidata_uri']),
            models.Index(fields=['wkg_depth']),
            models.Index(fields=['wkg_type_key', 'wkg_type_value']),
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
        """Resolve cross-database reference to TemporalSnapshot (default DB)."""
        if self.source_snapshot_id:
            from api.models import TemporalSnapshot
            return TemporalSnapshot.objects.using('default').filter(id=self.source_snapshot_id).first()
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

