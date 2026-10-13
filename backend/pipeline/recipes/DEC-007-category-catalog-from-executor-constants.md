---
id: DEC-007
title: Index categories derive from the executor constants, never a parallel list
kind: decision
status: active
date: 2026-10-12
trigger: none (design decision)
source: INDEX_BUILDER_POST_PROCESSING_PLAN.md §3 (decided 2026-10-11); the amenity_to_wkgs hand-maintained dict diverging from _constants.AMENITY_TO_WKGS is the failure mode this prevents
citations:
  - backend/semantic_search/services/query_executor_service/_constants.py:62
  - backend/semantic_search/services/query_executor_service/_constants.py:299
  - backend/worldkg_nca/management/commands/create_osmentity_vector_indexes.py:78
---

Every surface that lists indexable OSM tag keys derives the set from
`GENERIC_AMENITY_KEYS` union the tag keys inside `CATEGORY_TAG_TARGETS`
in `query_executor_service/_constants.py`. No endpoint, task, or
frontend keeps its own list. If a key is not in those constants it is
not indexable, because the query path could never emit it.

The automatic Step 6 build is `GENERIC_AMENITY_KEYS` itself (8 keys:
amenity, shop, tourism, leisure, office, craft, healthcare,
public_transport) — the parser's generic OBJECT vocabulary, where
enumeration has readers. Measured on the 2025_12_31 maintained leaves
(ie/jm/mx): ~1.1M index entries across all eight keys, ~60% amenity.
The larger tag-key sets (building 8.3M, highway 6.5M rows) are excluded
on purpose: they are geometry layers, not bounded vocabularies, so a
vocab index there is write cost with no reader.
