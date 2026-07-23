"""
Semantic Search App — Query & Retrieval Layer

Responsibilities:
- Hybrid vector search (pgvector + PostGIS)
- k-NN queries and ranking
- Search-specific models (ProjectionHead, TrainingMetrics, SnapshotDiff, SpatialLink)
- GPT-2 projection for query understanding
- Census tract embeddings and drift monitoring

NOT responsible for:
- Entity storage (see worldkg_nca.models.OsmEntity)
- WorldKG ontology (see worldkg_nca)
- NCA classification (see worldkg_nca)
- Entity alignment/IGEA (see igea)
- Spatial link prediction/USLP (see igea)
"""
