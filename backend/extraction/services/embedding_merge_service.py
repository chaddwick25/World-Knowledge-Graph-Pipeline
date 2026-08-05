"""Stateless service: merge US regional GeoVectors TSV shards.

GeoVectors stores the United States as 5 regional shards:
  us-midwest, us-northeast, us-pacific, us-south, us-west

Each shard has its own location.tsv.gz. This service concatenates them
into a single us-location.tsv.gz under EMBEDDINGS_ROOT/north-america/us/,
making the US eligible for the pipeline as a single country.

Sequential streaming — reads each shard line-by-line and pipes
directly into the gzip output. No temp files, no ThreadPool.

Idempotent: skips if the output already exists and passes gzip integrity check.
"""

from __future__ import annotations

import gzip
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Dict, List

logger = logging.getLogger(__name__)

# Candidate header.tsv sources (first existing one is copied)
_HEADER_SOURCES = [
    "north-america/us-other-location/header.tsv",
    "north-america/us-south-location/header.tsv",
    "north-america/us-west-tags/header.tsv",
]


class EmbeddingMergeService:
    """Merge US regional shards into single TSVs.

    Stateless — takes its root path in ``__init__`` (dependency injection).
    """

    def __init__(self, embeddings_root: Path) -> None:
        self.embeddings_root = Path(embeddings_root)

    def run(self) -> Dict[str, str]:
        """Execute the merge.

        Returns:
            ``{"status": "merged"|"skipped"|"no_config", "output": str, "elapsed_s": float}``.
        """
        from extraction.services.embedding_spatial_split_service import (
            load_embedding_splits_config,
        )

        t0 = time.time()
        us_dir = self.embeddings_root / "north-america" / "us"
        us_dir.mkdir(parents=True, exist_ok=True)

        # Load merges configuration (US merge is the first entry in "merges")
        splits_cfg = load_embedding_splits_config()
        merges = splits_cfg.get("merges", [])
        us_merge = None
        for m in merges:
            if m.get("slug") == "us":
                us_merge = m
                break

        if not us_merge:
            logger.info("No US merge configuration found in embedding_splits.json; skipping.")
            return {"status": "no_config", "output": "", "elapsed_s": time.time() - t0}

        shards: List[str] = us_merge.get("shards") or []
        output_rel: str = us_merge.get("output") or "us-location.tsv.gz"

        # Merge location TSV only; tags TSV merge remains intentionally skipped.
        loc_out = us_dir / output_rel
        self._merge_tsv_atomic(shards, loc_out, "location TSV", t0)

        # Copy header.tsv from first available shard directory
        for hs_rel in _HEADER_SOURCES:
            hs = self.embeddings_root / hs_rel
            if hs.exists():
                tgt_header = us_dir / "header.tsv"
                if not tgt_header.exists():
                    shutil.copy2(str(hs), str(tgt_header))
                    logger.info("  Copied header.tsv from %s", hs)
                break

        logger.info("  tags TSV: skipped (not needed for subgraph pickles)")

        return {
            "status": "merged",
            "output": str(loc_out),
            "elapsed_s": time.time() - t0,
        }

    def _merge_tsv_atomic(
        self,
        shard_paths: List[str],
        output_path: Path,
        label: str,
        t0: float,
    ) -> None:
        """Merge shards sequentially with atomic write and integrity check.

        Optimized: uses binary mode + shutil.copyfileobj with large buffer
        to avoid per-line Python overhead and double gzip decode/encode.
        """
        tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")

        if output_path.exists():
            # Gzip integrity check before trusting existing file
            try:
                with gzip.open(str(output_path), "rt") as f:
                    _ = f.readline()
                logger.info("  %s already exists at %s — skipping", label, output_path)
                return
            except OSError:
                logger.info("  %s at %s is corrupt; re-merging", label, output_path)

        logger.info("  Merging %s from %d shards (streaming) -> %s",
                    label, len(shard_paths), output_path)

        # 16 MB buffer for copyfileobj (reduces syscalls dramatically)
        BUF = 16 * 1024 * 1024
        # compresslevel=1 trades ~5% size for ~2x compression speed
        with gzip.open(str(tmp_path), "wb", compresslevel=1) as out:
            for i, rel_path in enumerate(shard_paths):
                src = self.embeddings_root / rel_path
                if not src.exists():
                    logger.info("    [%d/%d] %s NOT FOUND — skipping",
                                i + 1, len(shard_paths), src)
                    continue
                with gzip.open(str(src), "rb") as f:
                    if i > 0:
                        f.readline()  # skip header from shards 2..N
                    # copyfileobj is a C-level loop; much faster than Python per-line
                    shutil.copyfileobj(f, out, BUF)
                logger.info("    [%d/%d] %s: merged (%.0fs)",
                            i + 1, len(shard_paths), src.name, time.time() - t0)

        os.replace(tmp_path, output_path)
        logger.info("  %s merge complete in %.0fs", label, time.time() - t0)
