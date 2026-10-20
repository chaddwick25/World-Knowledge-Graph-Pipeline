---
id: DEC-002
title: Verification checks cited rows, not prose
kind: decision
status: draft
date: 2026-10-11
trigger: claim-citation verification design (next-stage); open choice: cite granularity (per row vs per block)
source: session 2026-10-11; trust-layer frame (Akerlof/Spence signaling); three runs' assembler incoherence as the evidence
citations:
  - backend/semantic_search/services/research_service.py:346
---
When answers cite the digest rows they used (id, snapshot, metric, value,
G4 flag), the verification pass becomes a row check: confirm each cited
row instead of re-analyzing the prose. Receipts are the citations. Draft
until the cite-granularity decision lands. Depends on DEC-001. The
evidence: three 2026-10-11 runs narrated "clustered in the same
location" over degenerate or unavailable distances, with the prompt rule
live; prompt enforcement is exhausted, the mechanical check is the fix.
