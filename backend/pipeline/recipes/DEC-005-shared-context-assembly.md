---
id: DEC-005
title: Shared context assembled once per research run
kind: decision
status: draft
date: 2026-10-11
trigger: pending trace evidence (same anchor geocoded N times in one run, 4-9s miss path hit)
source: session 2026-10-11; MQO §4 batcher applied at plan level
citations:
  - backend/semantic_search/services/entity_geocoder.py:24
  - backend/semantic_search/services/query_executor_service/template_executors.py:57
---
Within one research run, the shared sub-expressions (anchor geocode,
snapshot resolution, polygon scope, class resolution) are assembled once
and reused across questions. Scan-level composite predicates are out of
scope: filters are the volatile operator class in the MQO evaluation.
EntityGeocoder.geocode has no cross-call memo today; the per-run memo is
the minimal form.
