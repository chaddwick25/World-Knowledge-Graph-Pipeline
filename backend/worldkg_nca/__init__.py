"""
WorldKG NCA App — Neural Class Alignment & Ontology Management

Responsibilities:
- Entity storage (OsmEntity model with GeoVectors embeddings)
- WorldKG ontology management and hierarchy
- NCA (Neural Class Alignment) classification
- Entity enrichment with WorldKG classes
- Wikidata/DBpedia alignment
- Pipeline orchestration for WorldKG workflow

NOT responsible for:
- Vector search/retrieval (see semantic_search)
- Embedding generation (see geovectors_encoder)
- Entity alignment/IGEA (see igea — Part B)
"""
