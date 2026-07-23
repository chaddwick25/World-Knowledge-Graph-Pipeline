"""
Celery task: Step 1 — Embed OSM Entities

Preprocessing + GV-Tags + provisional GV-NLE + entropy gate + subgraph fan-out.
"""

from __future__ import annotations

import itertools
from pathlib import Path
import logging

from pipeline.celery_app import (
    celery_app,
    PipelineTask,
    CELERY_AVAILABLE,
)
from pipeline.config import CountryConfig, SubgraphConfig
from pipeline.exceptions import EntropyGateBlocked
from pipeline.tasks.helper import (
    _log,
    _push_update,
    preprocess_snapshot,
    enrich_worldkg_classes,
    compute_entropy,
)

logger = logging.getLogger("pipeline")


if CELERY_AVAILABLE:
    from celery import group

    @celery_app.task(
        bind=True, base=PipelineTask,
        name="step_1_embed_osm_entities",
        max_retries=1, default_retry_delay=120,
    )
    def step_1_embed_osm_entities(self, config_dict: dict) -> dict:
        """Step 1: Preprocessing + embedding + entropy gate + subgraphs.

        Small territories (Monaco, Belize, etc.) with has_subgraphs=False
        automatically skip the subgraph fan-out and run at country level only.
        """
        config_dict = self.setup_pipeline_context(config_dict)
        cfg = CountryConfig.from_dict(config_dict)

        # Rehydrate subgraphs from DB to ensure fresh data
        # (config_dict may be stale if dispatched before DB was updated)
        if not cfg.has_subgraphs or not cfg.subgraphs:
            try:
                fresh = CountryConfig.from_db(cfg.iso, snapshot_date=cfg.snapshot_date)
                if fresh.has_subgraphs and fresh.subgraphs:
                    cfg.subgraphs = fresh.subgraphs
                    cfg.has_subgraphs = True
                    config_dict = cfg.to_dict()
                    _log(
                        logger,
                        "info",
                        "Rehydrated subgraphs from DB",
                        country=cfg.iso,
                        subgraph_count=len(cfg.subgraphs),
                        pipeline_run_id=cfg.pipeline_run_id,
                    )
            except Exception as exc:
                _log(
                    logger,
                    "info",
                    "Subgraph rehydration failed, using config_dict as-is",
                    country=cfg.iso,
                    error=str(exc),
                    pipeline_run_id=cfg.pipeline_run_id,
                )

        _push_update(
            pipeline_run_id=cfg.pipeline_run_id,
            name="embed_osm_entities", status="in_progress",
            message="Preprocessing snapshot and embedding OSM entities...",
            pct=10, step=2,
        )

        _log(
            logger,
            "info",
            "Step 1: Embed OSM Entities",
            country=cfg.iso,
            has_pretrained_nle=cfg.has_pretrained_nle,
            has_subgraphs=cfg.has_subgraphs,
            subgraph_count=len(cfg.subgraphs),
            pipeline_run_id=cfg.pipeline_run_id,
        )

        # Snapshot preprocessing (v2 helper with explicit logger)
        preprocess_snapshot(cfg, logger=logger)

        from geovectors_encoder.services.geovectors_service import (
            DBOnlyWriter,
            DualEncodingWriter,
        )
        from geovectors_encoder.services.vector_storage_service import (
            VectorStorageService,
        )
        from geovectors_encoder.core.models.fasttext import FastTextModel
        from geovectors_encoder.core.models.nle import NLEModel
        from geovectors_encoder.core.db import DjangoPostgresDB
        from geovectors_encoder.core.util import read_from_snapshot

        ft_model = FastTextModel()
        tags_storage = VectorStorageService(
            model_type="tags", version=cfg.snapshot_date,
        )

        if cfg.has_pretrained_nle and cfg.pickle_path:
            _log(
                logger,
                "info",
                "Dual encoding mode (FastText + NLE from pickle)",
                pickle_path=cfg.pickle_path,
                country=cfg.iso,
                pipeline_run_id=cfg.pipeline_run_id,
            )
            db_bridge = DjangoPostgresDB()
            nle_model = NLEModel(
                str(Path(cfg.pickle_path).parent),
                njobs=1, db=db_bridge,
            )
            nle_model.load_indexes()
            nle_storage = VectorStorageService(
                model_type="nle", version=cfg.snapshot_date,
            )
            writer = DualEncodingWriter(
                tag_encoder=ft_model,
                nle_encoder=nle_model,
                tag_storage=tags_storage,
                nle_storage=nle_storage,
            )
            n_data, w_data, r_data = read_from_snapshot(
                cfg.snapshot_pbf_path, writer=writer, max_runs=2,
            )
            for record in itertools.chain(w_data, r_data):
                writer.add_line(record)
            tags_storage.flush()
            nle_storage.flush()
            nle_model.destroy()
        else:
            if not cfg.has_pretrained_nle:
                _log(
                    logger,
                    "info",
                    "FastText-only mode (no pre-trained NLE model)",
                    country=cfg.iso,
                    pipeline_run_id=cfg.pipeline_run_id,
                )
            writer = DBOnlyWriter(ft_model, tags_storage)
            n_data, w_data, r_data = read_from_snapshot(
                cfg.snapshot_pbf_path, writer=writer, max_runs=2,
            )
            for record in itertools.chain(w_data, r_data):
                writer.add_line(record)
            tags_storage.flush()
        # TODO: this could be the reason why classes are visible in the UI
        # v2 helpers accept an explicit logger; keep behavior identical
        enrich_worldkg_classes(cfg, logger=logger)

        entropy = compute_entropy(cfg, logger=logger)
        _log(
            logger,
            "info",
            "Entropy check",
            entropy=round(entropy, 4),
            threshold=cfg.min_entropy,
            country=cfg.iso,
            pipeline_run_id=cfg.pipeline_run_id,
        )
        if entropy < cfg.min_entropy and entropy > 0.0:
            raise EntropyGateBlocked(
                entropy=entropy, threshold=cfg.min_entropy,
            )

        # Subgraph fan-out: only if country HAS subgraphs
        if cfg.has_subgraphs and cfg.subgraphs:
            _log(
                logger,
                "info",
                "Launching subgraph embeddings in parallel",
                subgraph_count=len(cfg.subgraphs),
                country=cfg.iso,
                pipeline_run_id=cfg.pipeline_run_id,
            )
            subgraph_tasks = [
                _embed_subgraph.s(sg.to_dict(), cfg.to_dict())
                for sg in cfg.subgraphs
            ]
            group(subgraph_tasks).apply_async()
        else:
            _log(
                logger,
                "info",
                "No subgraphs to process (small territory or no subgraphs configured)",
                country=cfg.iso,
                pipeline_run_id=cfg.pipeline_run_id,
            )

        _push_update(
            pipeline_run_id=cfg.pipeline_run_id,
            name="embed_osm_entities", status="completed",
            message="OSM entity embedding complete.",
            pct=100, step=2,
        )

        _log(
            logger,
            "info",
            "Step 1 complete",
            country=cfg.iso,
            pipeline_run_id=cfg.pipeline_run_id,
        )
        return cfg.to_dict()


    @celery_app.task(bind=True, base=PipelineTask, name="sub_embed_subgraph")
    def _embed_subgraph(
        self, subgraph_dict: dict, parent_config: dict
    ) -> dict:
        """Embed a single subgraph (parallel Group subtask)."""
        sg = SubgraphConfig.from_dict(subgraph_dict)
        cfg = CountryConfig.from_dict(parent_config)
        _log(
            logger,
            "info",
            "Embedding subgraph",
            subgraph=sg.name,
            country=cfg.iso,
            pipeline_run_id=cfg.pipeline_run_id,
        )
        from geovectors_encoder.services.geovectors_service import (
            GeoVectorsEncoderService,
        )
        service = GeoVectorsEncoderService()
        result = service.generate_subgraph_pickle(
            country_name=cfg.name,
            subgraph_name=sg.name,
            continent=cfg.continent,
        )
        return {"subgraph": sg.name, "result": result}
