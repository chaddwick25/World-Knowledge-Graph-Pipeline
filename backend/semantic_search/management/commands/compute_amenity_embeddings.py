"""Precompute FastText embeddings for the MapQA amenity vocabulary.

Writes one ``AmenityEmbedding`` row per vocabulary entry (vectors DB,
``factor_amenity_embedding`` table).  At query time the executor's
semantic fallback tier (``_search_by_fasttext``) looks the query string up
here first — removing the last runtime FastText call for known amenities
(FACTOR_NODE_RUNTIME_JOINS_PLAN.md §3.4).

Usage::

    python manage.py compute_amenity_embeddings
    python manage.py compute_amenity_embeddings --vocab-path /path/vocab.json
    python manage.py compute_amenity_embeddings --limit 50

Idempotent: upserts on ``amenity_text``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Precompute FastText embeddings for the MapQA amenity vocabulary "
        "into the factor_amenity_embedding table (vectors DB)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--vocab-path", default=None,
            help="Path to amenity_vocab.json. Defaults to "
                 "{MAPQA_PARSER_DATA_DIR}/artifacts/amenity_vocab.json.",
        )
        parser.add_argument(
            "--limit", type=int, default=None,
            help="Only embed the first N vocabulary entries (debug).",
        )

    def handle(self, *args, **options):
        from semantic_search.services.fasttext_service import (
            FastTextEmbeddingService,
        )
        from worldkg_nca.models import AmenityEmbedding

        vocab_path = (
            Path(options["vocab_path"])
            if options["vocab_path"]
            else Path(settings.MAPQA_PARSER_DATA_DIR) / "artifacts" / "amenity_vocab.json"
        )
        if not vocab_path.exists():
            self.stdout.write(self.style.WARNING(
                f"amenity vocab not found at {vocab_path} — "
                f"run `train_mapqa_parser` first."
            ))
            return

        with open(vocab_path) as fh:
            vocab = json.load(fh)
        if not isinstance(vocab, list):
            raise ValueError(f"{vocab_path} is not a JSON list")

        # Normalize: lowercase + dedupe, preserving order
        seen = set()
        entries = []
        for item in vocab:
            text = str(item).strip().lower()
            if text and text not in seen:
                seen.add(text)
                entries.append(text)
        if options["limit"]:
            entries = entries[: options["limit"]]

        self.stdout.write(f"Embedding {len(entries)} amenity vocabulary entries...")

        written = 0
        failed = 0
        for text in entries:
            try:
                emb = FastTextEmbeddingService.calculate_text_embedding(text)
                AmenityEmbedding.objects.using("vectors").update_or_create(
                    amenity_text=text,
                    defaults={"embedding": emb.tolist()},
                )
                written += 1
            except Exception as exc:
                logger.warning("Failed to embed amenity %r: %s", text, exc)
                failed += 1

        self.stdout.write(self.style.SUCCESS(
            f"Done. written={written} failed={failed} → factor_amenity_embedding"
        ))
