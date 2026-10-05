# SSE Streaming & Request-Path Performance

> **Focus:** how `execute-query` streams as Server-Sent Events so the user
> watches the query run instead of a spinner, and the latency levers that
> took the deterministic executor chain from ~2.6s to ~61ms.
>
> **Key idea:** perceived latency is fixed by streaming (first visible event
> at ~0.2s); actual latency is fixed by removing repeated work. The dominant
> cost was `get_latest_snapshot_id()`, an Append over every partition's pkey
> (~850-980ms) called 3-4× per request, now TTL-cached. The remaining budget
> is the LLM synthesis call.

---

## 1. SSE Endpoint: `execute_query_stream`

`GET /api/nca/execute-query/stream/?query=…&country_code=…&snapshot_date=…`
(`worldkg_nca/views/search.py`) is a **plain Django view** (`@require_GET`,
`JsonResponse` for errors). DRF's `@api_view` content-negotiation rejects
`Accept: text/event-stream` with a 406 before the view runs.

Event protocol (named SSE events):

```
parsed         {parsed}                        ~0.2s
executed       {template, result_count, trace} after PostGIS executor
research       {tool, args}                    LLM selection done (agent path)
research_out   {tool, output}                  research tool executed (agent path)
answer_delta   {delta}                         token stream from Ollama,
                                                relayed by chat_stream
done           {result}                        full result JSON
error          {error}
```

Mechanics: the pipeline runs in a **worker thread**; events flow through a
`queue.Queue(maxsize=128)` to the response generator
(`StreamingHttpResponse`, `Cache-Control: no-cache`,
`X-Accel-Buffering: no` for nginx). `QueryExecutorService.execute(…,
event_callback=emit)` emits the `executed` event and forwards the callback
into the enrichment path. On the direct path, `research`/`research_out`
events do not appear; the sequence is parsed → executed → context →
answer_delta → done.

Frontend: the template mode uses `EventSource` (GET-only) with the URL
built from `axios.defaults.baseURL`. A relative `/nca/…` path hits Vite
with no proxy. Events update a live step (`parsed: …`, `executed: 18
results`) and the answer appears token-by-token. (The `/agent` demo page
that consumed this stream was removed 2026-08-26; the stream is consumed by
the search panel's template mode.)

---

## 2. `get_latest_snapshot_id()`: The Hot Path

The query (`worldkg_nca/snapshot_utils.py`) is
`OsmEntity…order_by("-snapshot_id").values_list(…).first()`, an Append over
**every snapshot partition's primary key** (~3.9M heap fetches, 850-980ms),
called by the executor's spatial search, `EntityGeocoder`, and
`_enrich_results`. Now TTL-cached (`SNAPSHOT_ID_CACHE_TTL_SECONDS`, default
120s; `clear_snapshot_cache()` after a pipeline backfill). Cold: 794ms →
cached: 0ms.

Note: `_enrich_results` is effectively a no-op (`AugmentedDataService` has
no `enrich_entity`; the per-result calls raise and are swallowed). Its cost
was purely the snapshot lookup.

---

## 3. Other Levers

- **Answer cache**: `query_enrichment_service.py` TTL cache replays events
  without LLM calls. Repeated questions ~12s → ~2.5s.
- **Warm-up**: `core/management/commands/warm_llm.py`, hooked into
  `docker-entrypoint.sh` (loads the model at container create; `docker
  compose restart` does NOT re-run the entrypoint, use
  `up -d --force-recreate`). Cold reloads are 5-15s.
- **Spatial geography index**: `ST_DWithin(geom::geography, …)` was a full
  partition seq scan (a plain `geom` GiST index can't serve the geography
  cast). `CREATE INDEX idx_osmentity_geom_geog ON semantic_search_osmentity
  USING GIST ((geom::geography))` on the partitioned parent (cascades to
  partitions) → Index Scan (37ms BZ, previously ~77ms; the win grows with
  partition size).
- **Synthesis brevity**: the enrichment prompt caps at 3 sentences/80
  words, `max_tokens` 200 (a verbose 290-token synthesis was observed).

---

## 4. Measured Budget (Belize, 2026-08-24)

| Phase | Before | After |
|---|---|---|
| `get_latest_snapshot_id()` | ~850-980ms × 3-4 | 0ms (cached) |
| Full executor chain | ~2.6s | ~61ms |
| Spatial query (BZ) | ~77ms seq scan | 37ms index scan |
| Repeated enriched query | ~12s | ~2.5s (cache) |
| Fresh enriched query | ~12s | ~10-11s but **streams progressively** (visible at ~0.2s) |

Remaining budget = 1 LLM synthesis call on the direct path (~2-4s; the
agent path additionally does one selection call) + cold-model reload
(killed by warm-up).

---

## 5. Files

- `backend/worldkg_nca/views/search.py`, `execute_query_stream`
- `backend/worldkg_nca/snapshot_utils.py`, snapshot cache
- `backend/core/management/commands/warm_llm.py`, warm-up
- `backend/docker-entrypoint.sh`, warm-up hook
- `frontend-v3/src/components/SemanticSearchPanel.vue`, EventSource
  consumer (template mode)
- `backend/tests/unit/test_snapshot_cache.py`, cache tests
