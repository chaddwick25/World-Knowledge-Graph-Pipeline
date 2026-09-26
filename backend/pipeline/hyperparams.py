"""Model hyperparameters — frozen dataclass loaded from ``hyperparams.yaml``.

Split out of ``pipeline/envelopes.py`` (monolith split, Phase 5 of
PIPELINE_CONTROL_PLANE_AND_CLEANUP_PLAN).  Loaded once at startup into a
frozen ``ModelHyperparams``; operators edit the YAML without touching code.
"""

from __future__ import annotations

import dataclasses
import logging
from pathlib import Path
from typing import Optional

import yaml
from django.conf import settings

logger = logging.getLogger(__name__)

_HYPERPARAMS_CACHE: Optional["ModelHyperparams"] = None


def _as_comma_str(value, default: str) -> str:
    """Normalize a YAML scalar-or-list value to a comma-separated string."""
    if value is None:
        return default
    if isinstance(value, (list, tuple)):
        return ",".join(str(v).strip() for v in value if str(v).strip())
    return str(value)


@dataclasses.dataclass(frozen=True)
class ModelHyperparams:
    """Static model hyperparameters — loaded from YAML, frozen, not serialized."""

    # USLP (link prediction)
    uslp_threshold: float = 0.7
    uslp_top_k: int = 50
    uslp_limit: int = 200000
    uslp_max_heads: int = 50000
    uslp_use_gpu: bool = True
    uslp_gpu_device: str = "cuda:0"

    # DeepWalk / GV-NLE
    deepwalk_k: int = 50
    deepwalk_embedding_dim: int = 100
    deepwalk_walk_length: int = 80
    deepwalk_num_walks: int = 10
    deepwalk_workers: int = 26
    deepwalk_use_gpu: bool = True
    deepwalk_gpu_device: str = "cuda:0"
    deepwalk_buffer_deg: float = 0.45

    # GPU scheduling — shared by Step 4 (USLP) and Step 5 (GV-NLE).
    # Comma-separated device + concurrency lists, parsed by the step modules.
    gv_nle_gpu_devices: str = "cuda:0,cuda:1"
    gv_nle_gpu_concurrency: str = "1,1"
    gv_nle_small_pbf_mb: float = 0.3

    # Step 1 parallel embedding upsert (embedding_service.py)
    parallel_upsert_workers: int = 1
    parallel_upsert_queue_depth: int = 2
    parallel_upsert_min_pbf_mb: int = 0
    parallel_upsert_chunk_size: int = 20000

    # Region enrichment + preprocess-embeddings worker counts
    enrichment_workers: int = 2
    embedding_splits_workers: int = 1

    # Entropy gate
    min_entropy: float = 1.5
    min_entropy_delta: float = 0.2

    # FastText
    fasttext_model_path: Optional[str] = None

    @classmethod
    def load_from_yaml(cls, path: Optional[str] = None) -> "ModelHyperparams":
        """Load hyperparams from YAML, with settings.py fallbacks."""
        global _HYPERPARAMS_CACHE
        if _HYPERPARAMS_CACHE is not None:
            return _HYPERPARAMS_CACHE

        if path is None:
            path = str(Path(__file__).parent / "hyperparams.yaml")

        data = {}
        try:
            with open(path) as f:
                data = yaml.safe_load(f) or {}
        except FileNotFoundError:
            logger.warning("hyperparams.yaml not found at %s — using settings.py defaults", path)

        uslp = data.get("uslp", {})
        dw = data.get("deepwalk", {})
        ent = data.get("entropy", {})
        ft = data.get("fasttext", {})
        gvn = data.get("gv_nle", {})
        pu = data.get("parallel_upsert", {})
        ench = data.get("enrichment", {})
        espl = data.get("embedding_splits", {})

        result = cls(
            uslp_threshold=float(uslp.get("threshold", getattr(settings, "USLP_THRESHOLD", 0.7))),
            uslp_top_k=int(uslp.get("top_k", getattr(settings, "USLP_TOP_K", 50))),
            uslp_limit=int(uslp.get("limit", getattr(settings, "USLP_LIMIT", 200000))),
            uslp_max_heads=int(uslp.get("max_heads", getattr(settings, "USLP_MAX_HEADS", 50000))),
            uslp_use_gpu=str(uslp.get("use_gpu", getattr(settings, "USLP_USE_GPU", "true"))).lower() in ("true", "1", "yes"),
            uslp_gpu_device=str(uslp.get("gpu_device", getattr(settings, "USLP_GPU_DEVICE", "cuda:0"))),
            deepwalk_k=int(dw.get("k", getattr(settings, "DEEPWALK_K", 50))),
            deepwalk_embedding_dim=int(dw.get("embedding_dim", 100)),
            deepwalk_walk_length=int(dw.get("walk_length", 80)),
            deepwalk_num_walks=int(dw.get("num_walks", 10)),
            deepwalk_workers=int(dw.get("workers", getattr(settings, "DEEPWALK_WORKERS", 26))),
            deepwalk_use_gpu=str(dw.get("use_gpu", getattr(settings, "DEEPWALK_USE_GPU", "true"))).lower() in ("true", "1", "yes"),
            deepwalk_gpu_device=str(dw.get("gpu_device", getattr(settings, "DEEPWALK_GPU_DEVICE", "cuda:0"))),
            deepwalk_buffer_deg=float(dw.get("buffer_deg", 0.45)),
            gv_nle_gpu_devices=_as_comma_str(
                gvn.get("gpu_devices"), getattr(settings, "GV_NLE_GPU_DEVICES", "cuda:0,cuda:1")
            ),
            gv_nle_gpu_concurrency=_as_comma_str(
                gvn.get("gpu_concurrency"), getattr(settings, "GV_NLE_GPU_CONCURRENCY", "1,1")
            ),
            gv_nle_small_pbf_mb=float(gvn.get("small_pbf_mb", getattr(settings, "GV_NLE_SMALL_PBF_MB", 0.3))),
            parallel_upsert_workers=int(pu.get("workers", getattr(settings, "PARALLEL_UPSERT_WORKERS", 1))),
            parallel_upsert_queue_depth=int(pu.get("queue_depth", getattr(settings, "PARALLEL_UPSERT_QUEUE_DEPTH", 2))),
            parallel_upsert_min_pbf_mb=int(pu.get("min_pbf_mb", getattr(settings, "PARALLEL_UPSERT_MIN_PBF_MB", 0))),
            parallel_upsert_chunk_size=int(pu.get("chunk_size", getattr(settings, "PARALLEL_UPSERT_CHUNK_SIZE", 20000))),
            enrichment_workers=int(ench.get("workers", getattr(settings, "ENRICHMENT_WORKERS", 2))),
            embedding_splits_workers=int(espl.get("workers", getattr(settings, "EMBEDDING_SPLITS_WORKERS", 1))),
            min_entropy=float(ent.get("min_entropy", getattr(settings, "MIN_PREFLIGHT_SHANNON_ENTROPY", 1.5))),
            min_entropy_delta=float(ent.get("min_entropy_delta", getattr(settings, "MIN_PREFLIGHT_ENTROPY_DELTA", 0.2))),
            fasttext_model_path=ft.get("model_path") or getattr(settings, "FASTTEXT_MODEL_PATH", None),
        )
        _HYPERPARAMS_CACHE = result
        return result
