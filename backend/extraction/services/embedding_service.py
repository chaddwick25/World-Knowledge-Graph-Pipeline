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

    def run(self, cfg: "CountryEnvelope") -> Dict:
        """Run the embedding pipeline for a country.

        Args:
            cfg: CountryEnvelope with snapshot_pbf_path, pickle_path,
                 has_pretrained_nle, iso, snapshot_date, etc.

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

        ft_model = FastTextModel()
        tags_storage = VectorStorageService(
            model_type="tags", version=cfg.snapshot_date,
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

        n_data, w_data, r_data = read_from_snapshot(
            cfg.snapshot_pbf_path, writer=writer, max_runs=2,
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

        # Enrich WorldKG classes + compute entropy (v2 helpers)
        from pipeline.tasks.helper import enrich_worldkg_classes, compute_entropy
        enrich_worldkg_classes(cfg, logger=logger)
        entropy = compute_entropy(cfg, logger=logger)

        entity_count = len(n_data) + len(w_data) + len(r_data)
        return {
            "entropy": entropy,
            "has_nle": cfg.has_pretrained_nle,
            "entity_count": entity_count,
        }

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
        )
        writer = DualEncodingWriter(
            tag_encoder=ft_model,
            nle_encoder=nle_model,
            tag_storage=tags_storage,
            nle_storage=nle_storage,
        )
        return writer, nle_storage
