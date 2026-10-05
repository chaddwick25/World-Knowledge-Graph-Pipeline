# Celery as the Control Plane: Canvas, Chords, and the Decorator Seam

> **Focus:** how Celery acts as the **control plane** for the country
> pipeline. The chain + chord canvas, the `PatchedDatabaseBackend` that fixes
> the ChordCounter bug via polling, and the `@pipeline_step` decorator that
> separates control plane (logging, WS push, run tracking) from data plane
> (stateless services).
>
> **Key idea:** Celery owns orchestration and lifecycle hooks; services own
> the data plane. The `@pipeline_step` decorator is the seam between them.
> Chords synchronize subgraph fan-out; chord completion is detected by a
> polling task over `TaskResult` rows, not by a `ChordCounter` table row.

---

## 1. The Country Pipeline Canvas

The country pipeline is a single Celery **chain** with three **chords**
embedded in it. Chords are the synchronization primitive for subgraph
fan-out: each chord header runs one task per subgraph (embed upserts, USLP,
GV-NLE training), and the chord body (callback) aggregates the results and
hands the config dict back to the chain.

**Source:** <ref_file file="backend/pipeline/canvas.py" />

```
chain(
  step_1_embed_osm_entities.s(cfg),
  chord( [sub_embed_subgraph.si(sg) × N], step_1b_finalize.s(cfg) ),
  step_2_harvest_wikidata.s(),
  step_3_run_igea.s(),
  chord( [sub_run_subgraph_uslp.si(sg) × N], step_4b_finalize.s(cfg) ),
  step_5_train_gv_nle.s(),
  chord( [sub_train_subgraph_gv_nle.si(sg) × N], step_5b_fin.s(cfg) ),
  step_5c_graph_spectral_analysis.s(),   # non-fatal, conditional
  step_5d_temporal_drift.s(),            # non-fatal, conditional, ≥2 snapshots
  step_6_mark_search_ready.s(),
)

Dispatched once: canvas.apply_async(task_id=cfg.pipeline_run_id)
```

- Header tasks use **`.si()`** (immutable signatures) so the chain does not
  inject the previous task's return value as a positional arg.
- Chord callbacks receive `(aggregated_results, config_dict)` and return the
  config dict, which the chain passes to the next step.
- Steps 1 and 5 skip their chord entirely when `has_subgraphs=False`
  (small territories); Step 4's chord is always present.
- Steps 5c (graph spectral analysis) and 5d (temporal drift) run after Step
  5/5b and before Step 6. Both are non-fatal and conditional.
- **Planet init is NOT in this canvas.** It is the `init_planet` management
  command run as a Docker entrypoint step (see
  [Docker Entrypoint Init](../04_ETL_Django_Vue_Primitives/01_Docker_Entrypoint_Init.md)).

---

## 2. Control Plane vs Data Plane

The pipeline separates concerns into two planes.

### 2.1 Control Plane (owned by Celery primitives)

| Primitive | Owner | Role |
|---|---|---|
| `task_prerun` signal | `celery_app.py` | Attaches per-run file logger before task execution |
| `setup_logging` signal | `celery_app.py` | Re-attaches console handler after worker fork |
| `on_success` hook | `PipelineTask` | Step-complete `PipelineLogEntry`, `PipelineRun.complete_stage`, WS completed |
| `on_failure` hook | `PipelineTask` | Step-error `PipelineLogEntry`, `PipelineRun.mark_failed`, WS failed |
| `after_return` hook | `PipelineTask` | Defensive cleanup of `_invocations` entries |

**Source:** <ref_file file="backend/pipeline/celery_app.py" />

### 2.2 Data Plane (owned by the `@pipeline_step` decorator + services)

**Source:** <ref_file file="backend/pipeline/task_decorator.py" />

The `@pipeline_step(step_name, EnvelopeClass, step_index)` decorator owns
only the data-plane boundary:
- Deserializes envelope from dict (`CountryEnvelope.from_dict()`)
- Logs step start to `PipelineLogEntry` via `PipelineLogger`
- Calls `PipelineRun.start_stage()`
- Pushes WebSocket in_progress via `_push_update()`
- Registers the `_invocations` entry (start time, logger, step details)
  consumed by hooks
- Calls `on_success`/`on_failure` hooks directly (so they fire in the
  direct-call eager path that bypasses Celery's trace)
- Re-raises exceptions after logging them (Celery marks task as FAILURE)

### 2.3 Services (pure data plane, no Celery concerns)

All country pipeline tasks delegate to stateless services:

| Service | File | Called by |
|---|---|---|
| `EmbeddingService` | `core/services/snapshot/embedding_service.py` | Step 1 |
| `IgeaPipelineService` | `igea/services/igea_pipeline_service.py` | Step 3 |
| `GvNleTrainingService` | `geovectors_encoder/services/gv_nle_training_service.py` | Step 5 |
| `SearchReadyService` | `core/services/pipeline/search_ready_service.py` | Step 6 |

Services accept a `CountryEnvelope` as their primary argument and return
either the same envelope (mutated) or a result dict. They do NOT handle
Celery concerns (serialization, WS push, logging); that's the decorator's
job.

### 2.4 Tasks Are Thin Wrappers

All task files in `pipeline/tasks/` are thin wrappers that:
1. Reconstruct the envelope from the config dict
2. Call a service
3. Return the envelope (serialized via `to_dict()`)

The structure per task:

- **`@pipeline_step` decorator (data-plane seam):**
  - deserialize envelope from dict
  - log step start (`PipelineLogEntry`)
  - `PipelineRun.start_stage()`
  - WS push: in_progress
  - call service
- **Service (pure data plane):** read files / query DB; encode / train /
  enrich; return result dict
- **`@pipeline_step` decorator (continued):** `on_success` hook (log
  complete, WS completed); return `envelope.to_dict()`

---

## 3. Registered Tasks (12 total: Steps 1-6 + 5c/5d)

Planet init tasks (formerly `step_0_initialize_planet`,
`step_0b_initialize_continent`, `step_0c`-`step_0m`,
`_finalize_planet_init_chain`) have been **deleted**. Planet init is now the
`init_planet` management command.

| Internal Name | Task Name | Role |
|---|---|---|
| `step_1_embed_osm_entities` | `step_1_embed_osm_entities` | Preprocessing + encoding + entropy gate + subgraph embed fan-out |
| `_embed_subgraph` | `sub_embed_subgraph` | Per-subgraph pickle generation (group) |
| `step_2_harvest_wikidata` | `step_2_harvest_wikidata` | Wikidata SPARQL candidate harvesting |
| `step_3_run_igea` | `step_3_run_igea` | Iterative Geographic Entity Alignment |
| `step_4_predict_spatial_links` | `step_4_predict_spatial_links` | USLP, rehydrates subgraphs from DB; self-dispatches per-subgraph USLP |
| `_run_subgraph_uslp` | `sub_run_subgraph_uslp` | Per-subgraph USLP via `--poly-file` (chord header, `.si()`) |
| `step_4b_finalize_subgraph_uslp` | `step_4b_finalize_subgraph_uslp` | Chord callback, aggregates subgraph USLP results |
| `step_5_train_gv_nle` | `step_5_train_gv_nle` | DeepWalk + subgraph train fan-out |
| `_train_subgraph_gv_nle` | `sub_train_subgraph_gv_nle` | Per-subgraph DeepWalk training (chord header, `.si()`) |
| `step_5c_graph_spectral_analysis` | `step_5c_graph_spectral_analysis` | Laplacian spectral features + graph signals, non-fatal |
| `step_5d_temporal_drift` | `step_5d_temporal_drift` | Spectral drift + ARIMA forecast + CUSUM, non-fatal, ≥2 snapshots |
| `step_6_mark_search_ready` | `step_6_mark_search_ready` | Upsert `CountrySearchProcessing.is_processed = True` |

---

## 4. Chord-in-Chain Dispatch (Verified against Celery 4.4.7)

### 4.1 Client side: `chain.prepare_steps()`

When `canvas.apply_async()` is called, the chain is prepared in **reverse
order**:
1. Each task signature is cloned and frozen (`Signature.freeze` assigns
   `task_id`, `root_id`).
2. A `chord` encountered in the chain is frozen via `chord.freeze`: the
   header group gets a `task_id` (this becomes the **group_id**) and each
   header task gets `options['chord'] = body`.
3. `prepare_steps` only calls `app.backend.ensure_chords_allowed()`. It does
   **not** create any backend state.
4. The remaining tasks travel with each message in the `chain` embed field.

### 4.2 Worker side: chain continuation

When a chain task succeeds, the worker pops the next signature **before**
marking the current task done. For a chord signature, `_chsig.apply_async()`
→ `chord.run()`, which **does** call `apply_chord()` before dispatching the
header:

```python
header_result = header.freeze(group_id=group_id, chord=body, root_id=root_id)
if len(header_result) > 0:
    app.backend.apply_chord(          # ← called before header dispatch
        header_result, body,
        interval=None,                # ← explicit None (see §5.2)
        countdown=countdown,
        max_retries=max_retries,
    )
    header_result = header(*partial_args, task_id=group_id, **options)
```

So for the `chain(..., chord(...), ...)` pattern, `apply_chord()` **is**
invoked, on the worker that ran the preceding chain step, **before** any
header task is dispatched.

### 4.3 Header completion path

```python
def mark_as_done(self, task_id, result, request=None, ...):
    if store_result:
        self.store_result(task_id, result, state, request=request)  # ← FIRST
    if request and request.chord:
        self.on_chord_part_return(request, state, result)           # ← SECOND
```

**Ordering guarantee:** the `TaskResult` row is written *before*
`on_chord_part_return()` runs. The polling mechanism in §5 relies on this.

---

## 5. PatchedDatabaseBackend: Chord Completion via Polling

**Source:** <ref_file file="backend/pipeline/celery_results_backend.py" />

Replaces the buggy `ChordCounter` mechanism of django-celery-results 2.0.0
with Celery's standard fallback polling task.

**Chord lifecycle (patched backend):**

1. `chord.run()`: `header.freeze(group_id)`
2. `apply_chord()` (patched):
   - delete stale `ChordCounter` (hygiene only)
   - publish `fallback_chord_unlock()` → `celery.chord_unlock` message with
     `kwargs: result=[t1..tN], interval, max_retries`
3. `header()` dispatches N header task messages
4. Header tasks run; `mark_as_done` writes `store_result` (TaskResult row),
   then `on_chord_part_return` = no-op
5. `celery.chord_unlock` polls:
   - rebuilds `GroupResult` from the message payload (not from the DB)
   - `deps.ready()` queries `TaskResult` rows
   - not ready: `retry(countdown=interval)`, forever (`max_retries=None`)
   - ready: `deps.join()` from `TaskResult` rows, then `callback.delay(ret)`
     → body runs, chain continues to the next step

Key mechanics:

1. **`apply_chord`** deletes any stale `ChordCounter` row for the group
   (hygiene, nothing reads that table anymore), then delegates to
   `BaseBackend.fallback_chord_unlock()`, which publishes a
   `celery.chord_unlock` message carrying `result=[r.as_tuple() ...]`, the
   full list of header task ids embedded in the message payload.
2. **`on_chord_part_return`** is a no-op. It never touches `ChordCounter`,
   so `ChordCounter.DoesNotExist` is structurally impossible.
3. **`celery.chord_unlock`** is a built-in task. It rebuilds the dependency
   set **from the message payload**, not from any saved group state:
   ```python
   deps = GroupResult(group_id,
                      [result_from_tuple(r, app=app) for r in result],
                      app=app)
   ready = deps.ready()          # queries TaskResult rows via the backend
   if not ready: raise self.retry(countdown=interval, max_retries=max_retries)
   ret = deps.join(timeout=result_chord_join_timeout, propagate=True)
   callback.delay(ret)
   ```
4. **What is preserved:** `store_result()` is untouched, so `TaskResult`
   rows keep flowing into Django's DB. `/api/task-results/<run_id>/`,
   `SnapshotJobResultsView`, and the `PipelineAsset.metadata.task_id` join
   all keep working. `CELERY_RESULT_EXPIRES = 48h`.

---

## 6. Failure Semantics

| Event | Behavior |
|---|---|
| Header task succeeds | `TaskResult` = SUCCESS; poller sees it on next tick |
| Header task fails | `TaskResult` = FAILURE → poller fires → `deps.join(propagate=True)` raises → callback marked failed; chain after body does **not** run |
| Header task lost | `TaskResult` stays PENDING → poller retries **forever** (`max_retries=None`); chord never fires; pipeline stalls at the chord |
| Chord dispatched twice | Two pollers, both see `ready()`, both `callback.delay(ret)` → callback runs **twice**. The polling path has no once-only guard (the old `ChordCounter` did). See `docs/issues/CHORD_BACKEND_REVIEW_FINDINGS.md` F6 |

Ack semantics: Celery defaults, **acks-early** (`acks_late` is not set). A
task that fails is rejected without requeue.

---

## 7. Subgraph Rehydration (Fresh-DB Fix)

On a fresh DB, `has_subgraphs=False` at canvas dispatch time because subgraph
poly files have not been generated yet. The canvas chord for Step 4 (USLP)
was built with this stale flag, so USLP would run at country level only
instead of fanning out to subgraphs.

To fix this, Step 4 and Step 5 now **rehydrate subgraphs from DB at task
start** (via `CountryEnvelope.from_db()`). When subgraphs are found after
rehydration, the task **self-dispatches per-subgraph work inline** instead of
relying on the canvas chord. The `rehydrated` flag distinguishes this path
from the normal chord-driven path.

The eager path in `canvas.py` no longer runs Step 1b/4b/5b chord callbacks
because Steps 1, 4, and 5 self-dispatch per-subgraph work internally. The
old eager path would double-execute USLP and NLE training.

---

## 8. Configuration Surface

| Setting | Value | Notes |
|---|---|---|
| `CELERY_RESULT_BACKEND` | `pipeline.celery_results_backend:PatchedDatabaseBackend` | Custom backend; bare `"django-db"` is the buggy stock backend |
| `CELERY_RESULT_EXPIRES` | 48h | Covers longest pipeline run + buffer |
| `CELERY_BROKER_URL` | `redis://<host>:6379/0` | Broker only; results go to Postgres |
| Worker pool | `--pool=prefork --concurrency=4` | 4 CPU workers for parallel upserts/IGEA/USLP |
| GPU tasks | `fcntl.flock` per-GPU slot locks | Prevents CUDA OOM on single-GPU machines |

### Known defect: `CHORD_UNLOCK_INTERVAL` is ineffective

`chord.run` passes `interval=None` **explicitly**, so
`kwargs.setdefault("interval", CHORD_UNLOCK_INTERVAL)` in
`PatchedDatabaseBackend.apply_chord` never fires (the key exists with value
`None`). `chord_unlock` then falls back to `default_retry_delay` = 1 second.
Impact: ~600 poll messages per 10-minute chord instead of ~60. Cosmetic
Redis churn, not a correctness issue.

---

## 9. Dual-Track Logging

The pipeline uses **dual-track logging**: DB-backed structured logs via
`PipelineLogEntry` for queryable history, plus per-run consolidated `.log`
files for human/grep workflows. Both tracks are always on.

| Layer | Model/Service | Role |
|---|---|---|
| Run-level state | `PipelineRun` | Authoritative run status (PENDING/RUNNING/COMPLETED/FAILED), `current_stage`, `completed_stages`, `stage_metrics` |
| Step-level audit | `PipelineLogEntry` | Queryable structured log entries per step with `metadata` JSONField |
| Celery audit | `TaskResult` (django-celery-results) | Celery's internal task state machine. 48h expiry. Do NOT fight it, layer on top |

**Per-run file logging** (`setup_pipeline_run_logger()`):
- Always on. One file per `pipeline_run_id` at
  `backend/logs/pipeline/pipeline_{COUNTRY}_{run_short}_{ts}.log`
- Idempotent per run. All steps in a run (across worker processes) append
  to the same file.
- Uses `WatchedFileHandler` (cross-process safe).

---

## 10. Envelopes: The Celery-Safe Config Contract

**Source:** <ref_file file="backend/pipeline/envelopes.py" />

`CountryEnvelope` and `PlanetEnvelope` are frozen dataclasses that replace
the legacy `CountryConfig`. They are:
- **DB-constructed** via `from_db(iso)`, reads `CountryPipelineProfile`,
  `SubgraphProfile`, hyperparams from `hyperparams.yaml`
- **Celery-safe** via `to_dict()` / `from_dict()`, serialize across task
  hops
- **Hyperparam-aware**, `hyperparam_overrides` dict preserved across hops
  (critical for `skip_entropy_gate` → `min_entropy=0.0`)

```python
env = CountryEnvelope.from_db('BZ', hyperparam_overrides={
    "skip_enrich": True,
    "min_entropy": 0.0,
})
config_dict = env.to_dict()  # ← passed to Celery task
# ... task hop ...
env = CountryEnvelope.from_dict(config_dict)  # ← reconstructed in worker
```

---

## 11. Invariants

- Planet init is NOT a Celery canvas. It is the `init_planet` management
  command. `run_planet_initialization()` / `run_continent_initialization()`
  in `canvas.py` are thin facades that delegate to the command
  synchronously.
- Chord header tasks MUST use `.si()` (immutable signatures) to prevent
  Celery from passing inherited positional args.
- The `PatchedDatabaseBackend` is required. The bare `django-db` backend has
  the ChordCounter bug that causes duplicate task execution.
- Hooks are idempotent via `_invocations`: the decorator's wrapper calls
  them directly, and Celery's trace calls them again after `run` returns.
  The first caller pops the entry and does the work; the second is a no-op.
- `PipelineLogEntry` has denormalized `country_code` and `continent` fields
  so the `ShardRouter` can route writes to the correct shard DB without a
  JOIN (currently a no-op; shard routing is deferred).
