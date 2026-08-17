"""Stateless service: copy great-britain TSVs to united-kingdom naming.

GeoVectors uses 'great-britain' as the slug but the pipeline uses
'united-kingdom' (matching Geofabrik's canonical slug). This service
copies the location and tags TSVs from great-britain-* to
united-kingdom-* so the rest of the pipeline can find them.

Idempotent: skips if the target already exists and matches the source size.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Dict, List

logger = logging.getLogger(__name__)


class GbToUkCopyService:
    """Copy great-britain TSVs to united-kingdom naming.

    Stateless — takes its root path in ``__init__`` (dependency injection).
    Returns a summary dict; the caller (task) handles logging and WS.
    """

    def __init__(self, embeddings_root: Path) -> None:
        self.embeddings_root = Path(embeddings_root)

    def run(self) -> Dict[str, List[str]]:
        """Execute the copy.

        Returns:
            ``{"copied": [...], "skipped": [...], "failed": [...]}`` —
            each list contains the labels of the TSVs in that category.
        """
        europe_dir = self.embeddings_root / "europe"

        # Source directories (great-britain naming)
        gb_location_src = europe_dir / "great-britain-location" / "great-britain-location.tsv.gz"
        gb_tags_src = europe_dir / "great-britain-tags" / "great-britain-tags.tsv.gz"
        gb_header_location = europe_dir / "great-britain-location" / "header.tsv"
        gb_header_tags = europe_dir / "great-britain-tags" / "header.tsv"

        # Target directories (united-kingdom naming)
        uk_location_dir = europe_dir / "united-kingdom-location"
        uk_tags_dir = europe_dir / "united-kingdom-tags"
        uk_location_tgt = uk_location_dir / "united-kingdom-location.tsv.gz"
        uk_tags_tgt = uk_tags_dir / "united-kingdom-tags.tsv.gz"

        copies = [
            ("location TSV", gb_location_src, uk_location_tgt, gb_header_location, uk_location_dir),
            ("tags TSV", gb_tags_src, uk_tags_tgt, gb_header_tags, uk_tags_dir),
        ]

        copied: List[str] = []
        skipped: List[str] = []
        failed: List[str] = []

        for label, src, tgt, header_src, tgt_dir in copies:
            if not src.exists():
                logger.info("  %s: source not found at %s — skipping", label, src)
                skipped.append(label)
                continue

            # Check if target exists and is same size (idempotent)
            src_size = src.stat().st_size
            if tgt.exists():
                tgt_size = tgt.stat().st_size
                if src_size == tgt_size:
                    logger.info("  %s: already exists at %s (%s bytes) — skipping",
                                label, tgt, f"{src_size:,}")
                    skipped.append(label)
                    continue
                logger.info("  %s: target exists but size differs (%s vs %s) — re-copying",
                            label, f"{src_size:,}", f"{tgt_size:,}")

            # Ensure target directory exists
            tgt_dir.mkdir(parents=True, exist_ok=True)

            # Copy the TSV file
            logger.info("  %s: copying %s (%s bytes) → %s",
                        label, src, f"{src_size:,}", tgt)
            try:
                shutil.copy2(str(src), str(tgt))
            except Exception as exc:
                logger.error("  %s: copy failed: %s", label, exc)
                failed.append(label)
                continue

            # Copy header.tsv if available
            if header_src.exists():
                header_tgt = tgt_dir / "header.tsv"
                if not header_tgt.exists():
                    shutil.copy2(str(header_src), str(header_tgt))
                    logger.info("  %s: copied header.tsv", label)

            # Verify
            tgt_size = tgt.stat().st_size
            if tgt_size == src_size:
                logger.info("  %s: verified — %s bytes", label, f"{tgt_size:,}")
                copied.append(label)
            else:
                logger.warning("  %s: WARNING — size mismatch (%s vs %s)",
                               label, f"{src_size:,}", f"{tgt_size:,}")
                failed.append(label)

        return {"copied": copied, "skipped": skipped, "failed": failed}
