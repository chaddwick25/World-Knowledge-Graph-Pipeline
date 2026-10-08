# LLM Capacity Envelope & Request Flow: qwen3:8b on the RTX 4070 Ti Super

> **Focus:** how a user request travels through parser → executor → LLM
> synthesis on the resident `qwen3:8b`, and the VRAM/compute envelope that
> bounds concurrent-user capacity.
>
> **Key idea:** capacity is **scheduler-bound, not VRAM-bound**. One
> resident 8B model (8.7 GB, 6 parallel slots) serves ~8–10 interactive
> users at P95 TTFT ~6s while leaving ~1.6 GiB headroom for the pipeline
> worker on the same card. All numbers measured live 2026-09-16 (A/B replay
> of exact production prompts + `OLLAMA_NUM_PARALLEL` load-test sweep).
> Supersedes the 14b envelope (qwen3:14b deleted 2026-09-16; see
> `docs/plans/LLM_OLLAMA_4070_CAPACITY_ENVELOPE.md`).

---

## 1. Request Flow: Question → Parsed → Executed → Enriched → Streamed

```
 user question
   (frontend-v3 SemanticSearchPanel, EventSource GET)
        |
        v
 GET /api/nca/execute-query/stream/?query=…&country_code=…&snapshot_date=…
 (worldkg_nca/views/search.py — plain @require_GET view, SSE out)
        |
        v
 resolve_iso_code(country) ── worker thread ── asyncio.Queue(512)
        |                                   └→ SSE generator (named events)
        v
 QueryParserService.parse(question)
   ├─ TF-IDF classifier → {template, concepts, roles, dag, confidence}
   └─ confidence < 0.5? ──yes──▶ _llm_refine   (LLM chat_json re-classify;
        │  no                  MAPQA_LLM_FALLBACK_CONFIDENCE   validated against
        │                       the label set; any failure keeps the
        │                       heuristic parse — accelerator, not SPOF)
        v
 emit "parsed"  {template, concepts, confidence}
        |
        v
 QueryExecutorService.execute(parsed, country, snapshot, question, callback)
   ├─ template routing  (5 trained executors + spectral/drift/community/diffusion)
   ├─ PostGIS           (geography GiST index, snapshot-id TTL cache)
   ├─ emit "executed"   {template, result_count, trace}     e.g. "18 results"
   ├─ EntityContextService.get_context()  (deterministic SQL:
   │     USLP links + communities + class distribution, per TEMPLATE_CONTEXT_MAP)
   ├─ emit "context"
   ├─ QueryEnrichmentService.synthesize()  ← THE ONE LLM CALL on the direct path
   │     prompt = primary digest + compacted context (~1.4K tokens)
   │            → LLMService.chat_stream → emit "answer_delta" (token stream)
   │     fail-soft: any error → keep the deterministic primary answer
   └─ emit "done" {result}   (enriched answer TTL-cached in-process, 1800s;
                              cache hits replay answer_delta events, no LLM call)
        |
        v
 SSE events → SemanticSearchPanel (EventSource)
   parsed → executed → context → answer_delta… → done    (direct path)
```

Invariant: **the LLM is an accelerator, never a single point of failure** —
every stage falls back to a deterministic result, and the frontend shows
progressive events from ~0.2s regardless of model latency.

### The LLM's three task shapes (all validated at 8B, 2026-09-16)

| Task | Where | Shape | 8b verdict |
|---|---|---|---|
| Answer synthesis | `synthesize()` (direct path, 1 call) | ≤3 sentences / 80 words, `max_tokens=200`, grounded-only | pass — never hallucinated counts, entities, or distances |
| Research tool selection | `enrich()` (agent path, 2 calls) | STRICT JSON `{tools:[…]}` | pass — valid, same behavior as 14b |
| Parser refine | `_llm_refine` (low-confidence fallback) | STRICT JSON template+concepts, label-validated | pass — byte-identical to 14b on the test parse |

---

## 2. Ollama Scheduler & Slot Model

```
 request ──▶ Ollama server (:11434)
              ├─ OLLAMA_MAX_QUEUE=32   (invisible back-pressure; client
              │                         LLM_TIMEOUT=60s kills stragglers)
              └─ scheduler allocates 1 of OLLAMA_NUM_PARALLEL=6 slots
                   (each slot = its own 4096-token KV cache + compute buffer)
                    ├─ slot free  → decode now
                    │               single-stream 96–103 tok/s
                    │               aggregate 280–290 tok/s across 6 slots
                    └─ all 6 busy → wait in queue (P95 TTFT climbs — this
                                     is the real concurrency ceiling)
              model stays resident forever (OLLAMA_KEEP_ALIVE=-1)
              OLLAMA_MAX_LOADED_MODELS=1 → goose shares the same 8b,
              no model swap (14b + 8b = 16.3 GB > 16 GB would thrash)
```

KV math: fp16 KV = 2 × 36 layers × 8 KV heads × 128 head_dim × 2 bytes
≈ **144 KiB/token**; a 4096-token slot ≈ 0.55 GB. Measured resident size
at 6 slots = **8.7 GB** (6.3 GB at 2 slots, 9.9 GB at 8).

---

## 3. Capacity Envelope (measured 2026-09-16)

Load test: exact production synthesis prompt (~1,351 prompt tokens, 200
out), 8 concurrent users × 4 requests, streaming, P95 metrics:

| NUM_PARALLEL | P95 TTFT | P95 total | agg tok/s | resident | worker margin |
|---:|---:|---:|---:|---:|---:|
| 2 (old) | 12.8s | 13.7s | 171 | 6.3 GB | ~4.1 GiB |
| 4 | 7.8s | 8.9s | 276 | ~8.1 GB | ~2.6 GiB |
| **6 (chosen)** | **6.3s** | **8.0s** | **280–290** | **8.7 GB** | **~1.6 GiB** |
| 8 | 6.0s | 7.6s | 324 | 9.9 GB | ~0.45 GiB |

- Chosen: **6** — ~90% of the 8-slot throughput, with a real worker margin.
- **8 is OOM territory**: 0.45 GiB margin, and the 2026-09-11 incident
  happened at 83 MiB free. Do not use while the worker shares the card.
- Capacity: **~8–10 interactive users** (vs 4–6 on the 14b). A ~200-token
  answer costs ~2.3s of decode at the aggregate rate; TTFT is the lever
  that degrades first as users arrive.
- Latency variance: cold-start runs are noisy (one 11.3s P95 outlier at 6;
  warm re-runs stable at 6.3–6.5s). Budget TTFT with margin.

---

## 4. GPU Sharing with the Pipeline Worker

```
4070 Ti Super (16 GB), one card, two tenants:
  ollama (qwen3:8b, 6 slots)   8.7 GB   NVIDIA_VISIBLE_DEVICES=1
  worker (GV-NLE / USLP / spectral)     NVIDIA_VISIBLE_DEVICES=1
                                       train_gv_nle peak ≈ 5.6 GiB
                                       (5.22 GiB + 372 MiB spike, measured
                                        in the 2026-09-11 incident)
  free VRAM: 7,355 MiB  →  margin over worker peak ≈ 1.6 GiB
```

- The **2026-09-11 BZ Step-5 OOM** (resident 14b ≈ 10.2 GiB + Node2Vec
  5.6 GiB → 83 MiB free) is **resolved by the 8b switch**: 8.7 + 5.6 ≈
  14.3 GiB < 16 GB.
- Normal pipeline runs no longer need `docker compose down ollama`.
- IE-scale spectral runs that need the full 16 GB still stop ollama first.

---

## 5. Wiring

```yaml
# docker-compose.yml
ollama:
  environment:
    - NVIDIA_VISIBLE_DEVICES=1      # 4070 Ti Super (GPU 1); worker also =1
    - OLLAMA_KEEP_ALIVE=-1          # resident forever — no cold reloads
    - OLLAMA_NUM_PARALLEL=6         # load-tested ceiling (2026-09-16)
    - OLLAMA_MAX_LOADED_MODELS=1    # one resident model (goose shares 8b)
    - OLLAMA_MAX_QUEUE=32           # bounded back-pressure

# backend + worker env
LLM_ENABLED=1
LLM_BASE_URL=http://ollama:11434/v1
LLM_MODEL=qwen3:8b                  # default in llm_service.py too
LLM_API_STYLE=native                # /api/chat + think:false (qwen3 thinking
                                    # mode eats the budget on /v1)
LLM_TIMEOUT=60.0
LLM_ANSWER_CACHE_TTL_SECONDS=1800

# goose env
GOOSE_PROVIDER=ollama
GOOSE_MODEL=qwen3:8b                # same model — no swap-thrash
```

---

## 6. Verification

```
docker exec eda-ollama ollama ps          # SIZE 8.7 GB, CONTEXT 4096, Forever
docker exec eda-ollama ollama list        # qwen3:8b only (14b deleted)
nvidia-smi --query-gpu=name,memory.used,memory.free --format=csv
curl -sN -H "Accept: text/event-stream" \
  "http://localhost:8000/api/nca/execute-query/stream/?query=Which%20cafes%20are%20within%2050km%20of%20Belize%20City%3F&country_code=Belize"
```

Capacity changes require a re-run of the load test (`OLLAMA_NUM_PARALLEL`
sweep, P95 TTFT at 8 concurrent users) — the numbers above are measured,
not guaranteed; model or context-size changes shift the envelope.

---

## Files

- `backend/core/services/llm_service.py` (client; default model `qwen3:8b`)
- `backend/worldkg_nca/views/search.py` (`execute_query_stream`)
- `backend/semantic_search/services/query_parser_service.py` (`_llm_refine`)
- `backend/semantic_search/services/query_executor_service/` (execution +
  enrichment wiring)
- `backend/semantic_search/services/query_enrichment_service.py`
  (`synthesize`, answer cache)
- `backend/core/management/commands/warm_llm.py` (resident warm-up)
- `docker-compose.yml` (ollama / backend / worker / goose env)
- `docs/plans/LLM_OLLAMA_4070_CAPACITY_ENVELOPE.md` (plan + A/B evidence)
- Companion schematics: `02_Platform_LLM_Enrichment.md` (LLMService /
  enrichment / parser refine), `03_SSE_Streaming_Performance.md` (SSE
  protocol + latency budget), `04_Docker_Agent_Stack.md` (containers)
