---
id: DEC-006
title: LangGraph is the orchestration control plane; Plan-SOFAI is the router policy
kind: decision
status: active
date: 2026-10-12
trigger: none (owner decision; DEC-004's checkpoint/resume trigger fired)
source: session 2026-10-12 (Fabiano MFAI 2026 talk; Fast and Slow Planning arXiv:2303.04283)
citations:
  - backend/semantic_search/services/research_service.py:1
  - backend/semantic_search/services/query_enrichment_service.py:98
---
The agent control plane is a LangGraph state graph implementing the
Plan-SOFAI policy: S1-by-default (the Tier 0 direct path), metacognitive
arbitration (confidence cx, accountability K, experience gates T1/T2,
risk aversion T3, exploration eps, time budget), refinement not restart
(the slow tier starts from the fast answer). The GeoFlow Graph stays the
data-plane formalism (the parser's trained DAG); the SSE event set stays
the frontend structure contract. The model stays frozen; learning lives
in small estimators on the trace log (the GNN compass, gated on trace
volume). Supervision semantics (let-it-crash) stay with the re-scoped
BEAM research track.
