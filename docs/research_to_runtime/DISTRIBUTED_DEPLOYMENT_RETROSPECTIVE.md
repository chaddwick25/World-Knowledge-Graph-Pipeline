# Distributed deployment and K80 provisioning: milestone retrospective

> **Status:** written 2026-11-13. Covers 2026-11-05 to 2026-11-11: provisioning
> the k80 host, the two-host split, routing Step 4 and Step 5c, and the first
> verified end-to-end run.
> **Audience:** the maintainer, and anyone reading the build record later.

## 1. Scope

The milestone had four goals:

1. Turn a 2014 Tesla K80 into a working compute node on a modern consumer board.
2. Split the pipeline across two hosts: workstation for compute, k80 for the
   data tier and the FP64 work.
3. Route Step 4 (USLP) and Step 5c (spectral) to the k80 without moving Step 5.
4. Prove the split with a real country, end to end.

## 2. Timeline

| Date | What happened |
|---|---|
| 2026-11-05 | Hardware inventory, BIOS flash 3.40 to 3.70, memory tuning |
| 2026-11-06 | Recovery log, Above 4G fix, both K80 dies enumerate, data tier running |
| 2026-11-08 | `Dockerfile.k80-worker`, worker compose, database pair, first routed runs |
| 2026-11-09 | Commit `dd6c31d`: task routing, bounded chord polling, FP64 gate |
| 2026-11-10 | IGEA fixes, USLP pool diagnosis |
| 2026-11-11 | USLP pool fix, verified JM @ `2026_11_02` end to end |

## 3. Decisions and why

**A separate image for the k80.** The backend image pins torch 2.0.1, and torch
2.x has no `sm_37` cubins. The K80 is visible and then every op fails. The k80
image uses CUDA 11.7.1 and `torch==1.13.1+cu117`, the last line that runs on
Kepler. It also removes the CUDA compat shim that shadows the host driver.

**Routing by task name, not by canvas position.** `pipeline/task_routing.py`
maps the four USLP entry points and Step 5c to `K80_GPU_QUEUE`. The canvas does
not change. The route travels with the task name, so the full chain, a
`--stage` run, and an API dispatch all land on the same worker.

**Step 5 stays on the workstation.** GV-NLE needs PyG, and FP32 throughput is
where the 4070 Ti SUPER wins. Moving it to the K80 would be slower.

**FP64 gate for Step 5c.** The original code ran `float32` with `tol=1e-8`.
float32 epsilon is about `1.2e-7`, so the tolerance is unreachable. The K80's
FP64 ratio is 1:2.6, so the gate is cheap on that card and expensive on the
workstation. `SPECTRAL_FP64=1` is set only on the k80 worker.

**A custom chord result backend.** django-celery-results 2.0.0 loses the
`ChordCounter` for a chord inside a `chain()`, so the chord unlock never fires
and the header re-runs. Norway trained 34 subgraphs instead of 17, 9h47m instead
of about 4h50m. `PatchedDatabaseBackend` replaces the counter with a bounded
`fallback_chord_unlock` poll.

**USLP has no CPU fallback.** The CPU and GPU paths produced different link
counts, and only the CPU path wrote rejected rows. A silent fallback made a
misrouted step look like a data defect. The step now raises when
`uslp.use_gpu=true` and the host has no CUDA.

## 4. What shipped

- `backend/Dockerfile.k80-worker`, the k80 worker image and compose service.
- `pipeline/task_routing.py`, `K80_GPU_QUEUE` in settings and compose.
- `SPECTRAL_FP64` gate in `spectral_analysis_service.py`.
- `pipeline/celery_results_backend.py` chord fix and the `worker_init` bound.
- `ISSUE-k80-gpu-worker` verified on JM, NI, and IE.
- The first fully verified JM run at `2026_11_02`: 8 stages, 28,930-candidate
  USLP pool, 3,746 accepted and 45,354 rejected links, no second pass.

## 4b. Release-blocker pass (2026-11-13)

Three completion blockers were closed after the first draft of this record:

- **Single-stage status.** `_update_run_stage` now marks a `single_stage` run
  COMPLETED when its only stage finishes, instead of leaving it RUNNING until
  the next dispatch's zombie recovery. Covered by `test_run_pipeline_stages.py`.
- **`--wait` preflight.** `run_pipeline --wait` checks that a worker is
  consuming `K80_GPU_QUEUE` and fails fast when none is. `--wait-timeout` is
  configurable.
- **Unit suite.** All 12 pre-existing failures were fixed. `pytest tests/unit`
  is 1172 passed, 17 skipped.

The ticket set was refreshed at the same time: `ISSUE-docs-refresh` and
`ISSUE-wkg3-snapshot-cutover` were created, and the geodata and embedding-source
tickets were marked deferred until the release cadence and the wkg3 cutover
are closed.

## 5. What broke, and what it cost

**The BIOS flash cost two days.** The flash itself was about twenty minutes.
The rest was seven independent failure modes, none of which was the firmware:
a solid LED that means "file rejected", two attempts at the wrong power state,
an oversized USB stick, a CMOS clear that reset Above 4G Decoding and stopped
the board from POSTing with the K80 installed, a four-DIMM population that would
not train until the sticks were added one at a time, a Memtest86+ freeze at one
second that was a tool bug, and a memory population that defaults to 2133 after
a clear.

**The routing was opt-in and therefore off.** `K80_GPU_QUEUE` was not set on the
workstation services until 2026-11-11. Full chains dispatched from the
workstation did not reach the k80. The issue record shows the symptom: Step 4
ran on the k80 in some runs and not others, which looked like a code defect
until the environment was checked.

**The k80 image bakes the code.** There is no bind mount. Every change to a
routed step needs an image rebuild and a worker recreate. That is now part of
the deployment checklist, but it is a manual step.

**The data tier is a single host.** Non-ECC memory, Ubuntu 20.04 (EOL), a
consumer board, and a 931 GB NVMe for Postgres. The 3.7 TB data disk is for
data, not the database. This is a deliberate cost trade, not a hidden risk.

**Cross-host logs do not reach the dispatcher.** Step 4 writes its execution
lines to the k80's per-run log. The dispatcher's copy has only the dispatch
lines.

## 6. Metrics

| Measurement | Value |
|---|---|
| FP64 solve on the k80 | about 3x slower than FP32, predicted 2.6x |
| USLP Step 4, JM @ `2026_11_02` | 62 s, 28,930 pool, 709 heads, 49,100 scored |
| Step 5c, JM @ `2026_11_02` | 156 s, 31,048 spectral rows, 59 communities, modularity 0.958 |
| Full JM chain | about 17 min (Step 1 399 s, Step 2 110 s, Step 3 33 s, Step 4 62 s, Step 5 213 s, Step 5c 156 s, Step 6 52 s) |
| K80 idle | about 33 °C, both dies visible, 11.4 GiB usable per die |
| K80 die delta | die 0 about 8 °C hotter, fan-insensitive at 149 W (preliminary, PWM sweep pending) |

## 7. What we would do differently

1. **Put the four flash rules at the top of the procedure.** The LED semantics,
   the powered-off requirement, the 2 GB FAT32 partition, and the Above 4G trap
   would have turned two days into 30 to 45 minutes.
2. **Set `K80_GPU_QUEUE` in compose from the first commit.** An opt-in route is
   a route that is off until someone remembers.
3. **Add the no-GPU preflight before the first routed run.** A failure is
   cheaper than a wrong result.
4. **Add a queue-consumer preflight for `--wait`.** The command should not hang
   for four hours because the worker is down.
5. **Write the release checklist before the split.** The image rebuild, the
   `.env` values, and the smoke test are release steps, not tribal knowledge.
6. **Keep the data snapshot in the release.** A tagged image without a verified
   snapshot is only half a release.

## 8. Open items

- Remote log forwarding into the dispatcher's per-run log.
- Step 5c parity baseline.
- k80 image build automation.
- `PLANET_OSM_FILE_PATH` rollout to the running services and the reset script.
- The docs refresh and the wkg3 snapshot cutover are the next milestones.

## 9. Lessons

**A flash is a sequence of independent state machines.** The LED tells you
whether the write happened. The power state tells you whether the controller is
listening. The stick layout tells you whether the file is readable. After a CMOS
clear, check what the clear changed before suspecting the firmware.

**A legacy GPU is useful only for the workload whose ratio matches its
architecture.** The K80 wins FP64 by about 4x and loses FP32 by about 18x. That
asymmetry is the routing rule, not a preference.

**A silent fallback is worse than a failure.** The CPU path and the GPU path
produced different results, and the fallback hid the difference. The fix was to
fail, not to degrade.

**The data tier is part of the release.** The database host, its BIOS settings,
its memory timings, and its backups are as much a release artifact as the
image. The two-day flash is the proof.
