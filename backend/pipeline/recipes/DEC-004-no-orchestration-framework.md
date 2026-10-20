---
id: DEC-004
title: No graph-orchestration framework until a measured trigger
kind: decision
status: superseded
date: 2026-10-12
trigger: fired 2026-10-12 (cross-run checkpoint/resume required by the metacognitive router policy)
source: session 2026-10-11 design review; superseded session 2026-10-12
citations:
  - backend/semantic_search/services/research_service.py:74
---
Superseded by [[DEC-006]] (LangGraph control plane, Plan-SOFAI policy).
The trigger fired by this recipe's own terms: the metacognitive router's
model of self (accountability K, experience gates T1/T2) requires
cross-run checkpoint/resume state the hand-rolled loop does not have.

Original decision (2026-10-11): the research loop stays hand-rolled.
LangGraph is a second graph formalism competing with the GeoFlow Graph
(trained, gated) and a speculative substrate until its trigger fires.
The frontend structure contract is the SSE event set (plan, question,
follow_up, replan, summary_delta, done), not a framework client. Showing
structure on the frontend is a rendering feature (draw the DAG / coverage
ledger), not an orchestration rewrite.
