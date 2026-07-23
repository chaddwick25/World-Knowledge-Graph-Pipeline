"""
IGEA App — Iterative Geographic Entity Alignment & Spatial Link Prediction

Responsibilities:
- IGEA entity alignment (OSM ↔ Wikidata) with BiLSTM cross-attention
- USLP spatial link prediction (tri-space: geo + name + topo)
- TransE topological constraint validation
- Temporal snapshot evaluation and urban drift auditing

Mathematical Foundations:
- IGEA: Dsouza et al., ISWC 2023 (https://arxiv.org/abs/2105.00847)
- USLP: Mann et al., ISWC 2023 (https://arxiv.org/abs/2304.09503)

NOT responsible for:
- Entity storage (see worldkg_nca.models.OsmEntity)
- WorldKG ontology (see worldkg_nca)
- Embedding generation (see geovectors_encoder)
"""

default_app_config = 'igea.apps.IgeaConfig'
