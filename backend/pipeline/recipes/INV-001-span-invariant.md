---
id: INV-001
title: Agent claims lie in the span of resolved artifacts
kind: invariant
status: active
date: 2026-10-11
trigger: none (design invariant)
source: AFT span invariant (Qiu et al., ICML 2024, Eq. 6) applied to text; session 2026-10-11
citations:
  - backend/semantic_search/services/factor_resolution_service.py:81
---
Every claim in a synthesized answer must be derivable from resolved
artifact rows (factor digests, executor answers, coverage ledger).
Out-of-span claims are dropped, not softened. The synthesis LLM is the
downstream model; the factor digests are the teacher features. Enforced
today by the assembler prompt (nothing invented to fill gaps); enforced
mechanically once answers cite digest rows (DEC-002).
