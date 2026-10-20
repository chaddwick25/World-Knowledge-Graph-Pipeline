"""Step 5c tuning constants (package-internal).

Extracted from ``step_5c_graph_spectral.py`` (monolith split, Phase 5 of
PIPELINE_CONTROL_PLANE_AND_CLEANUP_PLAN).
"""

# Default number of eigenvalues to compute. Large countries (Norway ~1-3M
# nodes) may need k=64 — eigsh with k=128 takes ~30-60 min for 3M nodes.
DEFAULT_K_EIGENVALUES = 128
LARGE_COUNTRY_K_EIGENVALUES = 64
# Country node-count threshold above which we reduce k to keep eigsh tractable.
LARGE_COUNTRY_NODE_THRESHOLD = 500_000
# Skip GraphML serialization for graphs above this node count.
# ``nx.write_graphml`` builds the full ``xml.etree.ElementTree`` in memory
# (one Python ``Element`` object per node/edge/attribute — ~100+ bytes each
# before character data), which for 73.8M edges allocates 20–40 GB on top of
# the NetworkX graph.  GraphML is a debug artifact only — the factor-table
# path (``factor_spectral_node_metric`` + pgvector ``<#>``) is authoritative
# at runtime.  Tunable via the ``GRAPHML_NODE_THRESHOLD`` Django setting.
GRAPHML_NODE_THRESHOLD = 200_000

# Node-count threshold for routing between country-level GPU LOBPCG and
# subdivision-scoped spectral analysis.  Below this threshold, the country
# graph fits in GPU VRAM and is solved as one global eigenbasis (fastest
# path — one solve, one eigenbasis, no transport matrices needed).  At or
# above this threshold, the country is split into subgraphs, each solved
# independently, with functional-map transport matrices computed in
# Phase B for cross-subgraph diffusion at runtime.
#
# GPU LOBPCG VRAM at k=128, float32: ~2240 bytes/node.  16 GB VRAM minus
# ~2 GB torch overhead ≈ 14 GB available → ~6.25M nodes theoretical max,
# ~5M with safety margin for fragmentation and convergence spikes.
# IE (9.7M nodes at current ingestion limits) uses subdivision; JM/BZ/CV
# use country-level GPU.  The 2.47M IE run (2026-08-19) was before the
# uslp.limit raise from 200K to 1.72M which grew IE to 9.7M entities.
SUBDIVISION_NODE_THRESHOLD = 5_000_000
