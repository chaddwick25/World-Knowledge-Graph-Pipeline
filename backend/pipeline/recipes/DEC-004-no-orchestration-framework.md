---
id: DEC-004
title: No graph-orchestration framework until a measured trigger
kind: decision
status: active
date: 2026-10-11
trigger: revisit when 3+ recipes duplicate loop plumbing, or cross-run checkpoint/resume is required
source: session 2026-10-11 design review
citations:
  - backend/semantic_search/services/research_service.py:74
  - deploy/plans/AGENT_ORCHESTRATION_TIERED_PLAN.md:55
---
The research loop stays hand-rolled. LangGraph is a second graph formalism
competing with the GeoFlow Graph (trained, gated) and a speculative
substrate until its trigger fires. The frontend structure contract is the
SSE event set (plan, question, follow_up, replan, summary_delta, done),
not a framework client. Showing structure on the frontend is a rendering
feature (draw the DAG / coverage ledger), not an orchestration rewrite.
