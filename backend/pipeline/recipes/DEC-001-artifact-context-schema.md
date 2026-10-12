---
id: DEC-001
title: Artifacts enter reasoning as resolved digests, never raw
kind: decision
status: active
date: 2026-10-11
trigger: design phase of AGENT_ORCHESTRATION_TIERED_PLAN §4.2
source: session 2026-10-11; Spatial-Agent §3.6 (intermediate states recorded and provided to the agent)
citations:
  - backend/semantic_search/services/factor_resolution_service.py:114
  - backend/semantic_search/services/entity_context_service.py:29
  - backend/semantic_search/management/commands/compute_amenity_embeddings.py:1
---
Pre-computed graph artifacts reach a prompt only as resolved digests from
FactorResolutionService, each carrying its G4 availability flag, typed by
the template role it serves (SUPPORT, COND, ...). Raw eigen-loadings,
adjacency, and graph rebuilds never reach a prompt. The digest is the
serving form of the factor tables: staging vs serving, never serve through
raw parsing. Extends DEC-003: better grounding without a second LLM call,
so Tier 0 cost stays flat.
