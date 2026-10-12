---
id: DEC-003
title: Deterministic structure is the default; model discretion is routed
kind: decision
status: active
date: 2026-10-11
trigger: none (standing design rule)
source: incidents 2026-09-18 (runaway brief emission) and 2026-10-01 (Mexico City fabricated template); Spatial-Agent error analysis (0 structural errors, 79.4% execution-layer)
citations:
  - backend/semantic_search/services/query_parser_service.py:5
  - backend/semantic_search/services/research_service.py:1700
---
Tier 0 (trained parser + deterministic executor + one grounded synthesis
call) is the default path. Tools run only on explicit triggers (low parser
confidence, nested anchors, cross-language ambiguity). Rewrites outside the
trained template vocabulary are discarded, not executed. The parser is
trained small models, not a prompted LLM; the executor is the fit-measure.
