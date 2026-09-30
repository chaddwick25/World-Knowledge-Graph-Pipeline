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
    uslp_use_fp64: bool = False

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
        """Load hyperparams from YAML, with dataclass defaults as fallback."""
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
            logger.warning("hyperparams.yaml not found at %s — using dataclass defaults", path)

        uslp = data.get("uslp", {})
        dw = data.get("deepwalk", {})
        ent = data.get("entropy", {})
        ft = data.get("fasttext", {})
        gvn = data.get("gv_nle", {})
        pu = data.get("parallel_upsert", {})
        ench = data.get("enrichment", {})
        espl = data.get("embedding_splits", {})

        result = cls(
            uslp_threshold=float(uslp.get("threshold", cls.uslp_threshold)),
            uslp_top_k=int(uslp.get("top_k", cls.uslp_top_k)),
            uslp_limit=int(uslp.get("limit", cls.uslp_limit)),
            uslp_max_heads=int(uslp.get("max_heads", cls.uslp_max_heads)),
            uslp_use_gpu=str(uslp.get("use_gpu", cls.uslp_use_gpu)).lower() in ("true", "1", "yes"),
            uslp_gpu_device=str(uslp.get("gpu_device", cls.uslp_gpu_device)),
            uslp_use_fp64=str(uslp.get("use_fp64", cls.uslp_use_fp64)).lower() in ("true", "1", "yes"),
            deepwalk_k=int(dw.get("k", cls.deepwalk_k)),
            deepwalk_embedding_dim=int(dw.get("embedding_dim", cls.deepwalk_embedding_dim)),
            deepwalk_walk_length=int(dw.get("walk_length", cls.deepwalk_walk_length)),
            deepwalk_num_walks=int(dw.get("num_walks", cls.deepwalk_num_walks)),
            deepwalk_workers=int(dw.get("workers", cls.deepwalk_workers)),
            deepwalk_use_gpu=str(dw.get("use_gpu", cls.deepwalk_use_gpu)).lower() in ("true", "1", "yes"),
            deepwalk_gpu_device=str(dw.get("gpu_device", cls.deepwalk_gpu_device)),
            deepwalk_buffer_deg=float(dw.get("buffer_deg", cls.deepwalk_buffer_deg)),
            gv_nle_gpu_devices=_as_comma_str(gvn.get("gpu_devices"), cls.gv_nle_gpu_devices),
            gv_nle_gpu_concurrency=_as_comma_str(gvn.get("gpu_concurrency"), cls.gv_nle_gpu_concurrency),
            gv_nle_small_pbf_mb=float(gvn.get("small_pbf_mb", cls.gv_nle_small_pbf_mb)),
            parallel_upsert_workers=int(pu.get("workers", cls.parallel_upsert_workers)),
            parallel_upsert_queue_depth=int(pu.get("queue_depth", cls.parallel_upsert_queue_depth)),
            parallel_upsert_min_pbf_mb=int(pu.get("min_pbf_mb", cls.parallel_upsert_min_pbf_mb)),
            parallel_upsert_chunk_size=int(pu.get("chunk_size", cls.parallel_upsert_chunk_size)),
            enrichment_workers=int(ench.get("workers", cls.enrichment_workers)),
            embedding_splits_workers=int(espl.get("workers", cls.embedding_splits_workers)),
            min_entropy=float(ent.get("min_entropy", cls.min_entropy)),
            min_entropy_delta=float(ent.get("min_entropy_delta", cls.min_entropy_delta)),
            fasttext_model_path=ft.get("model_path") or cls.fasttext_model_path,
        )
        _HYPERPARAMS_CACHE = result
        return result
