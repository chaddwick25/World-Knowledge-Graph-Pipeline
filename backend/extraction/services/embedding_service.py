"""Stateless service: embed OSM entities from a snapshot PBF.

Handles FastText + NLE model loading, writer setup, snapshot reading,
WorldKG class enrichment, and entropy computation.

Pure data plane — no Celery, no logging dispatcher, no WS push.
Receives a CountryEnvelope, reads files, writes to DB,
returns a result dict.
"""

from __future__ import annotations

import itertools
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Dict

if TYPE_CHECKING:
    from pipeline.envelopes import CountryEnvelope

logger = logging.getLogger(__name__)


class EmbeddingService:
    """Embed OSM entities from a snapshot PBF.

    Stateless — takes its root path in ``__init__`` (dependency injection).
    """

    def __init__(self, embeddings_root: Path) -> None:
        self.embeddings_root = Path(embeddings_root)

    def run(self, cfg: "CountryEnvelope", drop_indexes_during_load: bool = False,
            pbf_path_override: str = None,
            skip_index_drop: bool = False,
            skip_index_rebuild: bool = False,
            skip_post_process: bool = False) -> Dict:
        """Run the embedding pipeline for a country.

        Args:
            cfg: CountryEnvelope with snapshot_pbf_path, pickle_path,
                 has_pretrained_nle, iso, snapshot_date, etc.
            drop_indexes_during_load: If True, drop HNSW vector indexes
                HNSW) before the bulk upsert and rebuild them in parallel
                after.  Gives 3-5x faster upserts for large countries
                (Phase 5 of OSMENTITY_MONOLITH_OPTIMIZATION.md).
            pbf_path_override: If set, read from this PBF instead of
                ``cfg.snapshot_pbf_path``.  Used by parallel subgraph upsert
                tasks to read per-subgraph PBFs.
            skip_index_drop: If True, skip dropping indexes (coordinated by
                the caller for parallel subgraph upserts).
            skip_index_rebuild: If True, skip rebuilding indexes (coordinated
                by the caller for parallel subgraph upserts).
            skip_post_process: If True, skip WorldKG enrichment + entropy
                computation (done by the caller after all subgraphs complete).

        Returns:
            ``{"entropy": float, "has_nle": bool, "entity_count": int}``.
        """
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

        pbf_path = pbf_path_override or cfg.snapshot_pbf_path

        ft_model = FastTextModel()
        tags_storage = VectorStorageService(
            model_type="tags", version=cfg.snapshot_date,
            snapshot_id=cfg.snapshot_date, country_code=cfg.iso,
        )

        if cfg.has_pretrained_nle and cfg.pickle_path:
            logger.info(
                "Dual encoding mode (FastText + NLE from pickle) [pickle_path=%s country=%s]",
                cfg.pickle_path, cfg.iso,
            )
            writer, nle_storage = self._build_dual_writer(cfg, ft_model, tags_storage)
        else:
            if not cfg.has_pretrained_nle:
                logger.info(
                    "FastText-only mode (no pre-trained NLE model) [country=%s]",
                    cfg.iso,
                )
            writer = DBOnlyWriter(ft_model, tags_storage)
            nle_storage = None

        should_drop = drop_indexes_during_load and not skip_index_drop
        if should_drop:
            self._drop_vector_indexes()

        try:
            n_data, w_data, r_data = read_from_snapshot(
                pbf_path, writer=writer, max_runs=2,
            )
            for record in itertools.chain(w_data, r_data):
                writer.add_line(record)
            tags_storage.flush()
            if nle_storage:
                nle_storage.flush()
                # NLEModel doesn't expose destroy in all versions; guard it
                try:
                    if hasattr(writer, "nle_encoder") and hasattr(writer.nle_encoder, "destroy"):
                        writer.nle_encoder.destroy()
                except Exception:
                    pass
        finally:
            if should_drop and not skip_index_rebuild:
                self._rebuild_vector_indexes()

        entity_count = len(n_data) + len(w_data) + len(r_data)

        if skip_post_process:
            return {
                "entropy": 0.0,
                "has_nle": cfg.has_pretrained_nle,
                "entity_count": entity_count,
            }

        # Enrich WorldKG classes + compute entropy (v2 helpers)
        from pipeline.tasks.helper import enrich_worldkg_classes, compute_entropy
        enrich_worldkg_classes(cfg, logger=logger)
        entropy = compute_entropy(cfg, logger=logger)

        return {
            "entropy": entropy,
            "has_nle": cfg.has_pretrained_nle,
            "entity_count": entity_count,
        }

    def _drop_vector_indexes(self) -> None:
        """Drop HNSW indexes for fast bulk load (no per-row maintenance)."""
        from django.core.management import call_command

        logger.info("Dropping vector indexes for bulk load...")
        call_command("drop_osmentity_vector_indexes")
        logger.info("Vector indexes dropped.")

    def _rebuild_vector_indexes(self) -> None:
        """Rebuild HNSW indexes in parallel after bulk load."""
        from django.core.management import call_command

        logger.info("Rebuilding vector indexes (parallel)...")
        call_command("create_static_embedding_hnsw_index", parallel_workers=4)
        call_command("create_osmentity_vector_indexes")
        logger.info("Vector indexes rebuilt.")

    def _build_dual_writer(self, cfg, ft_model, tags_storage):
        """Build a DualEncodingWriter for the FastText + NLE path.

        Returns (writer, nle_storage).
        """
        from geovectors_encoder.core.models.nle import NLEModel
        from geovectors_encoder.core.db import DjangoPostgresDB
        from geovectors_encoder.services.geovectors_service import DualEncodingWriter
        from geovectors_encoder.services.vector_storage_service import VectorStorageService

        db_bridge = DjangoPostgresDB()
        nle_model = NLEModel(
            str(Path(cfg.pickle_path).parent),
            njobs=1, db=db_bridge,
        )
        nle_model.load_indexes()
        nle_storage = VectorStorageService(
            model_type="nle", version=cfg.snapshot_date,
            snapshot_id=cfg.snapshot_date, country_code=cfg.iso,
        )
        writer = DualEncodingWriter(
            tag_encoder=ft_model,
            nle_encoder=nle_model,
            tag_storage=tags_storage,
            nle_storage=nle_storage,
        )
        return writer, nle_storage
