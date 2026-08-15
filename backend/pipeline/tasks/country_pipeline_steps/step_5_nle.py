"""
Celery tasks: Step 5 — Train GV-NLE

Authoritative DeepWalk embeddings for country and subgraph level.

Multi-GPU scheduling (2026-08-15):
  All subgraphs are sent to the primary GPU (cuda:0, RTX 4070, 16 GB)
  which can handle any IE county.  Concurrency defaults to 1 because
  large IE subgraphs (kildare: 847K entities, leitrim: 1M entities)
  each need ~7-8 GB VRAM — two concurrent causes CUDA OOM on 16 GB.
  The GpuSlotLock queues subgraphs when the GPU is busy.
  The GPU set and concurrency are configurable via env vars:
    GV_NLE_GPU_DEVICES:   comma-separated list (default: "cuda:0,cuda:1")
    GV_NLE_GPU_CONCURRENCY: comma-separated per-device limits (default: "1,1")
"""

from __future__ import annotations
import dataclasses
import logging
import os
import tempfile
import fcntl
from typing import List, Optional, Tuple
from pipeline.config import SubgraphConfig
from pipeline.envelopes import CountryEnvelope
from pipeline.task_decorator import pipeline_step
from pipeline.tasks.helper import _log
from pipeline.celery_app import (
    pipeline_task,
    PipelineTask,
)

logger = logging.getLogger("pipeline")

# ── Multi-GPU configuration ────────────────────────────────────────────────


def _parse_gpu_config() -> Tuple[List[str], List[int]]:
    """Parse GPU device list and concurrency from env vars.

    Default concurrency=1 on cuda:0 because IE subgraphs like kildare
    (847K entities) and leitrim (1M entities) each need ~7-8 GB VRAM.
    Two of those concurrent on a 16 GB 4070 causes CUDA OOM.  Set to 2
    only for countries with uniformly small subgraphs (<100K entities).
    """
    devices_str = os.environ.get("GV_NLE_GPU_DEVICES", "cuda:0,cuda:1")
    concurrency_str = os.environ.get("GV_NLE_GPU_CONCURRENCY", "1,1")
    devices = [d.strip() for d in devices_str.split(",") if d.strip()]
    concurrency = []
    for c in concurrency_str.split(","):
        try:
            concurrency.append(int(c.strip()))
        except ValueError:
            concurrency.append(1)
    while len(concurrency) < len(devices):
        concurrency.append(1)
    return devices, concurrency


_GPU_DEVICES, _GPU_CONCURRENCY = _parse_gpu_config()

# Size threshold (MB) for assigning subgraphs to the big GPU vs the small one.
# Subgraphs with PBF > this go to cuda:0 (more VRAM); smaller ones can go to
# cuda:1.  0.3 MB ≈ ~50K entities, which needs ~4-5 GB VRAM — fits on 8 GB.
_SMALL_PBF_THRESHOLD_MB = float(os.environ.get("GV_NLE_SMALL_PBF_MB", "0.3"))


def _assign_gpu(subgraph: SubgraphConfig) -> str:
    """Pick a GPU device for this subgraph.

    Default policy: send all subgraphs to the primary GPU (cuda:0, the
    RTX 4070 with 16 GB VRAM) which can handle any IE county.  The
    secondary GPU (cuda:1, the 2070 with 8 GB) is only used as overflow
    when the primary GPU's slots are saturated — but since the slot
    lock handles queuing, we always return cuda:0 here and let the
    concurrency limit naturally serialize.

    Falls back to cuda:0 if only one GPU is configured.
    """
    if len(_GPU_DEVICES) == 1:
        return _GPU_DEVICES[0]

    # Send everything to the big GPU.  The GpuSlotLock handles queuing
    # when the GPU is at capacity.  The secondary GPU is reserved for
    # future use or manual override via GV_NLE_GPU_DEVICES.
    return _GPU_DEVICES[0]


def _gpu_concurrency_for(device: str) -> int:
    """Get the concurrency limit for a given GPU device."""
    for dev, conc in zip(_GPU_DEVICES, _GPU_CONCURRENCY):
        if dev == device:
            return conc
    return 1


class GpuSlotLock:
    """Cross-process counting semaphore using fcntl lock files.

    For a GPU with concurrency N, creates N lock files.  acquire() tries
    each in order until one is available (non-blocking trylock); if all are
    held, blocks on the first one.  release() unlocks the held slot.

    This works across prefork worker processes because fcntl locks are
    per-file-descriptor at the OS level.
    """

    def __init__(self, device: str, concurrency: int):
        self.device = device
        self.concurrency = concurrency
        # Sanitize device name for filename (cuda:0 → cuda_0)
        dev_tag = device.replace(":", "_")
        self._lock_dir = os.path.join(tempfile.gettempdir(), "gpu_slots")
        os.makedirs(self._lock_dir, exist_ok=True)
        self._lock_files = [
            os.path.join(self._lock_dir, f"{dev_tag}_slot{i}.lock")
            for i in range(concurrency)
        ]
        self._held_fd: Optional[int] = None
        self._held_index: Optional[int] = None

    def acquire(self) -> None:
        """Acquire one slot, blocking until a slot is available."""
        while True:
            for i, path in enumerate(self._lock_files):
                fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    # Got it
                    self._held_fd = fd
                    self._held_index = i
                    return
                except (BlockingIOError, OSError):
                    os.close(fd)
                    continue
            # All slots busy — block on the first slot (waits until it's free),
            # then loop back to try all slots again.  This avoids thundering
            # herd: only one waiter wakes per release.
            fd = os.open(self._lock_files[0], os.O_CREAT | os.O_RDWR, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)  # blocks
                # We got the first slot — but we might want a different one.
                # Just use this one.
                self._held_fd = fd
                self._held_index = 0
                return
            except Exception:
                os.close(fd)
                continue

    def release(self) -> None:
        """Release the held slot."""
        if self._held_fd is not None:
            try:
                fcntl.flock(self._held_fd, fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                os.close(self._held_fd)
            except Exception:
                pass
            self._held_fd = None
            self._held_index = None


@pipeline_task(
    bind=True, base=PipelineTask,
    name="step_5_train_gv_nle",
    max_retries=1, default_retry_delay=300,
)
@pipeline_step("train_gv_nle", CountryEnvelope, 5.0)
def step_5_train_gv_nle(self, env: CountryEnvelope) -> CountryEnvelope:
    """Step 5: Train authoritative GV-NLE (DeepWalk).

    Small territories (has_subgraphs=False) run at country level only,
    skipping the subgraph fan-out.
    """
    # Rehydrate subgraphs from DB to ensure fresh data
    if not env.has_subgraphs or not env.subgraphs:
        try:
            fresh = CountryEnvelope.from_db(env.iso, snapshot_date=env.snapshot_date)
            if fresh.has_subgraphs and fresh.subgraphs:
                env = dataclasses.replace(
                    env,
                    subgraphs=fresh.subgraphs,
                    state=dataclasses.replace(env.state, has_subgraphs=True),
                )
                _log(
                    logger,
                    "info",
                    "Rehydrated subgraphs from DB",
                    country=env.iso,
                    subgraph_count=len(env.subgraphs),
                    pipeline_run_id=env.pipeline_run_id,
                )
        except Exception as exc:
            _log(
                logger,
                "info",
                "Subgraph rehydration failed, using config as-is",
                country=env.iso,
                error=str(exc),
                pipeline_run_id=env.pipeline_run_id,
            )

    _log(
        logger,
        "info",
        "Step 5: Train GV-NLE",
        country=env.iso,
        k=env.deepwalk_k,
        embedding_dim=env.deepwalk_embedding_dim,
        walk_length=env.deepwalk_walk_length,
        num_walks=env.deepwalk_num_walks,
        use_gpu=env.deepwalk_use_gpu,
        has_subgraphs=env.has_subgraphs,
        gpu_devices=_GPU_DEVICES,
        gpu_concurrency=_GPU_CONCURRENCY,
        pipeline_run_id=env.pipeline_run_id,
    )

    if env.has_subgraphs and env.subgraphs:
        # Subgraph fan-out is handled by canvas.py via a chord so the chain
        # waits for all subgraph NLE training to complete before proceeding
        # to Step 6.  This task returns the envelope; canvas.py wraps it
        # with chord([_train_subgraph_gv_nle.si(...)], step_5b_finalize.s(cfg)).
        _log(
            logger,
            "info",
            "Step 5: country-level training complete, subgraph fan-out via chord",
            subgraph_count=len(env.subgraphs),
            country=env.iso,
            pipeline_run_id=env.pipeline_run_id,
        )
    else:
        _log(
            logger,
            "info",
            "No subgraphs — training at country level (small territory)",
            country=env.iso,
            pipeline_run_id=env.pipeline_run_id,
        )
        from geovectors_encoder.services.gv_nle_training_service import (
            GvNleTrainingService,
        )
        GvNleTrainingService().run(env)

    _log(
        logger,
        "info",
        "Step 5 complete",
        country=env.iso,
        pipeline_run_id=env.pipeline_run_id,
    )
    return env


@pipeline_task(
    bind=True, base=PipelineTask,
    name="step_5b_finalize_subgraph_nle",
)
def step_5b_finalize_subgraph_nle(
    self, aggregated_results: list, config_dict: dict = None
) -> dict:
    """Chord callback — finalize Step 5 after all subgraph NLE training completes.

    ``config_dict`` is passed via ``chord(header, callback.s(config_dict))``
    in ``canvas.py`` so the callback can reconstruct the envelope for
    downstream chain continuation (Step 6).
    """
    if config_dict is None:
        for item in aggregated_results:
            if isinstance(item, dict) and "iso" in item and "slug" in item:
                config_dict = item
                break

    env = CountryEnvelope.from_dict(config_dict) if config_dict else None

    subgraph_results = [
        item for item in aggregated_results
        if isinstance(item, dict) and "subgraph" in item
    ]

    _log(
        logger,
        "info",
        "Step 5b: Finalizing subgraph NLE training — all subgraphs complete",
        subgraph_count=len(subgraph_results),
        country=env.iso if env else "unknown",
        pipeline_run_id=env.pipeline_run_id if env else "unknown",
    )

    for sg_result in subgraph_results:
        _log(
            logger,
            "info",
            "Subgraph NLE training result",
            subgraph=sg_result.get("subgraph"),
            status=sg_result.get("status"),
        )

    return config_dict or {"status": "completed", "subgraphs": subgraph_results}


@pipeline_task(
    bind=True, base=PipelineTask,
    name="sub_train_subgraph_gv_nle",
)
def _train_subgraph_gv_nle(
    self, subgraph_dict: dict, parent_config: dict
) -> dict:
    """Train GV-NLE for a single subgraph.

    Uses per-GPU slot locks (fcntl-based) to allow concurrent training
    on the primary GPU.  All subgraphs go to cuda:0 (RTX 4070, 16 GB)
    with concurrency=2.  The slot lock queues subgraphs when the GPU
    is at capacity.
    """
    sg = SubgraphConfig.from_dict(subgraph_dict)
    env = CountryEnvelope.from_dict(parent_config)

    # Pick GPU based on subgraph PBF size
    gpu_device = _assign_gpu(sg)
    concurrency = _gpu_concurrency_for(gpu_device)
    slot_lock = GpuSlotLock(gpu_device, concurrency)

    _log(
        logger,
        "info",
        "Training GV-NLE for subgraph",
        subgraph=sg.name,
        country=env.iso,
        gpu_device=gpu_device,
        gpu_concurrency=concurrency,
        pipeline_run_id=env.pipeline_run_id,
    )

    # Acquire per-GPU slot (blocks if that GPU is at capacity)
    slot_lock.acquire()
    try:
        # Override the envelope's gpu_device for this subgraph
        env = dataclasses.replace(
            env,
            hyperparams=dataclasses.replace(
                env.hyperparams,
                deepwalk_gpu_device=gpu_device,
            ),
        )
        from geovectors_encoder.services.gv_nle_training_service import (
            GvNleTrainingService,
        )
        return GvNleTrainingService().run_subgraph(sg, env)
    finally:
        slot_lock.release()
