"""Stateless service: train GV-NLE (DeepWalk) embeddings.

Wraps the DeepWalk training command for both country-level and
subgraph-level training. Extracts the logic from step_5_nle.py
and helper.py's run_gv_nle_training.

Pure data plane — no Celery, no logging dispatcher, no WS push.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Dict

from django.core.management import call_command

if TYPE_CHECKING:
    from pipeline.config import SubgraphConfig
    from pipeline.envelopes import CountryEnvelope

logger = logging.getLogger(__name__)


class GvNleTrainingService:
    """Train GV-NLE (DeepWalk) embeddings.

    Stateless — no constructor args.
    """

    def __init__(self) -> None:
        pass

    def run(self, cfg: "CountryEnvelope") -> Dict:
        """Train GV-NLE at the country level.

        Args:
            cfg: CountryEnvelope with deepwalk_* hyperparams, slug, poly_path, iso.

        Returns:
            ``{"status": "trained", "region": str}``.
        """
        logger.info(
            "Training GV-NLE at country level [country=%s region=%s poly=%s "
            "k=%d dim=%d walk=%d num_walks=%d workers=%d gpu=%s]",
            cfg.iso, cfg.slug, cfg.poly_path,
            cfg.deepwalk_k, cfg.deepwalk_embedding_dim,
            cfg.deepwalk_walk_length, cfg.deepwalk_num_walks,
            cfg.deepwalk_workers, cfg.deepwalk_use_gpu,
        )

        call_command(
            "train_gv_nle",
            region=cfg.slug,
            poly_file=cfg.poly_path,
            country=cfg.iso,
            k=cfg.deepwalk_k,
            embedding_dim=cfg.deepwalk_embedding_dim,
            walk_length=cfg.deepwalk_walk_length,
            num_walks=cfg.deepwalk_num_walks,
            workers=cfg.deepwalk_workers,
            gpu=cfg.deepwalk_use_gpu,
            gpu_device=cfg.deepwalk_gpu_device,
            buffer_deg=cfg.deepwalk_buffer_deg,
            batch_size=10000,
        )

        return {"status": "trained", "region": cfg.slug}

    def run_subgraph(
        self,
        subgraph: "SubgraphConfig",
        cfg: "CountryEnvelope",
    ) -> Dict:
        """Train GV-NLE for a single subgraph.

        Args:
            subgraph: SubgraphConfig with slug, poly_path.
            cfg: Parent CountryEnvelope with deepwalk_* hyperparams.

        Returns:
            ``{"subgraph": str, "status": "trained"}``.
        """
        logger.info(
            "Training GV-NLE for subgraph [subgraph=%s country=%s]",
            subgraph.name, cfg.iso,
        )

        call_command(
            "train_gv_nle",
            region=subgraph.slug,
            poly_file=subgraph.poly_path,
            k=cfg.deepwalk_k,
            embedding_dim=cfg.deepwalk_embedding_dim,
            walk_length=cfg.deepwalk_walk_length,
            num_walks=cfg.deepwalk_num_walks,
            workers=cfg.deepwalk_workers,
            gpu=cfg.deepwalk_use_gpu,
            gpu_device=cfg.deepwalk_gpu_device,
            buffer_deg=cfg.deepwalk_buffer_deg,
            batch_size=10000,
        )

        return {"subgraph": subgraph.name, "status": "trained"}
