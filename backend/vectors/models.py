from django.db import models
from pgvector.django import VectorField

class OsmEmbedding(models.Model):
    """
    Tag-level embedding: one 300D vector per unique OSM tag string (e.g. 'amenity=cafe').
    Used for aggregate semantic search over tag vocabulary.

    For entity-level embeddings (individual OSM nodes/ways with dual GV-Tags + GV-NLE
    vectors, geometry, and versioning), see semantic_search.OsmEntity.
    """
    tag = models.TextField(unique=True, help_text="Unique OSM tag, e.g., 'amenity=school'")
    embedding = VectorField(dimensions=300)
    metadata = models.JSONField(default=dict, null=True, blank=True, help_text="Optional metadata from OSM")

    def __str__(self):
        return self.tag

    class Meta:
        verbose_name = "OSM Embedding"
        verbose_name_plural = "OSM Embeddings"
        indexes = [
            models.Index(fields=['tag']),
        ]
