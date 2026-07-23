"""
WorldKG Pipeline v2 — Celery Canvas Control Plane

Pipeline steps:
    0.   Initialize Planet    (infrastructure, runs once)
    0.5  Initialize Continent (extract continent PBFs from planet)
    1.   Embed OSM Entities   (preprocessing + GV-Tags + provisional GV-NLE)
    2.   Harvest Wikidata     (SPARQL candidates)
    3.   Run IGEA             (Iterative Geographic Entity Alignment)
    4.   Predict Spatial Links  (USLP — name+geo+class scoring / gating)
    5.   Train GV-NLE         (Authoritative DeepWalk embeddings)

Small-territory handling (Monaco, Belize, etc.):
    Countries WITHOUT subgraphs automatically skip the subgraph fan-out
    in Steps 1 and 5. They run at country level only.
"""

