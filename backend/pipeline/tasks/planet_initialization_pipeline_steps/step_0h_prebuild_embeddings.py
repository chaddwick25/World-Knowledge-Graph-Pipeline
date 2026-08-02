"""
Celery tasks: Embedding Pre-build Steps

These steps handle embedding-related pre-build work during planet initialization:

  - step_0h_scan_embeddings      — Scan EMBEDDINGS_ROOT, populate EligibleCountry rows
  - step_0h_copy_gb_to_uk        — Copy great-britain TSVs to united-kingdom naming
  - step_0i_prebuild_split_embeddings — Split multi-country TSVs (GB, Malaysia/Singapore/Brunei)
  - step_0j_prebuild_merge_us_embeddings — Merge 5 US regional shards into single US TSVs

They sit conceptually between prebuild_country_paths (0.7) and prebuild_subgraphs (0.8)
because the TSV splits/merges must happen before subgraph profiles need resolved paths.
"""

from __future__ import annotations

import shutil
import gzip
import logging
from pathlib import Path
from django.conf import settings
from pipeline.config import CountryConfig
from django.core.management import call_command
from pipeline.tasks.helper import _log, _push_update
from pipeline.celery_app import (
    celery_app,
    PipelineTask,
    CELERY_AVAILABLE,
)

if CELERY_AVAILABLE:
    logger = logging.getLogger("pipeline")
    @celery_app.task(
        bind=True, base=PipelineTask,
        name="step_0h_scan_embeddings",
        max_retries=1, default_retry_delay=60,
    )
    def step_0h_scan_embeddings(self, config_dict: dict) -> dict:
        """Step 0.75: Scan EMBEDDINGS_ROOT and populate EligibleCountry rows.

        Runs the ``scan_embeddings`` management command to check every
        target country's embedding availability (ready / needs split /
        needs merge / no embeddings), which drives map colouring.
        """
        config_dict = self.setup_pipeline_context(config_dict)
        cfg = CountryConfig.from_dict(config_dict)
        _log(
            logger,
            "info",
            "Step 0.75: Scan embeddings",
            pipeline_run_id=cfg.pipeline_run_id,
        )
        _push_update(
            pipeline_run_id=cfg.pipeline_run_id,
            name="prebuild_scan_embeddings", status="in_progress",
            message="Scanning EMBEDDINGS_ROOT for eligibility...",
            pct=10,
        )
        call_command("scan_embeddings", clear=True)
        _push_update(
            pipeline_run_id=cfg.pipeline_run_id,
            name="prebuild_scan_embeddings", status="completed",
            message="Embedding scan complete — EligibleCountry table populated.",
            pct=100,
        )
        _log(
            logger,
            "info",
            "Step 0.75 complete",
            pipeline_run_id=cfg.pipeline_run_id,
        )
        return cfg.to_dict()


    @celery_app.task(
        bind=True, base=PipelineTask,
        name="step_0h_copy_gb_to_uk",
        max_retries=1, default_retry_delay=60,
    )
    def step_0h_copy_gb_to_uk(self, config_dict: dict) -> dict:
        """Step 0.755: Copy great-britain TSVs → united-kingdom naming.

        GeoVectors uses 'great-britain' as the slug but the pipeline uses
        'united-kingdom' (matching Geofabrik's canonical slug). This step
        copies the location and tags TSVs from great-britain-* to
        united-kingdom-* so the rest of the pipeline can find them.

        Both location and tags TSVs are copied. The operation is idempotent:
        if the target already exists and is the same size as the source,
        the copy is skipped.
        """
        config_dict = self.setup_pipeline_context(config_dict)
        cfg = CountryConfig.from_dict(config_dict)
        _log(
            logger,
            "info",
            "Step 0.755: Copy great-britain → united-kingdom",
            pipeline_run_id=cfg.pipeline_run_id,
        )
        _push_update(
            pipeline_run_id=cfg.pipeline_run_id,
            name="prebuild_copy_gb_to_uk", status="in_progress",
            message="Copying great-britain TSVs to united-kingdom naming...",
            pct=10,
        )



        emb_root = Path(settings.EMBEDDINGS_ROOT)
        europe_dir = emb_root / "europe"
        # TODO: Remove this when we have a better way to handle this we have to reorganize the original embeddings structure
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

        for label, src, tgt, header_src, tgt_dir in copies:
            if not src.exists():
                _log(
                    logger,
                    "info",
                    f"  {label}: source not found at {src} — skipping",
                    pipeline_run_id=cfg.pipeline_run_id,
                )
                continue

            # Check if target exists and is same size (idempotent)
            src_size = src.stat().st_size
            if tgt.exists():
                tgt_size = tgt.stat().st_size
                if src_size == tgt_size:
                    _log(
                        logger,
                        "info",
                        f"  {label}: already exists at {tgt} ({src_size:,} bytes) — skipping",
                        pipeline_run_id=cfg.pipeline_run_id,
                    )
                    continue
                _log(
                    logger,
                    "info",
                    f"  {label}: target exists but size differs ({src_size:,} vs {tgt_size:,}) — re-copying",
                    pipeline_run_id=cfg.pipeline_run_id,
                )

            # Ensure target directory exists
            tgt_dir.mkdir(parents=True, exist_ok=True)

            # Copy the TSV file
            _log(
                logger,
                "info",
                f"  {label}: copying {src} ({src_size:,} bytes) → {tgt}",
                pipeline_run_id=cfg.pipeline_run_id,
            )
            shutil.copy2(str(src), str(tgt))

            # Copy header.tsv if available
            if header_src.exists():
                header_tgt = tgt_dir / "header.tsv"
                if not header_tgt.exists():
                    shutil.copy2(str(header_src), str(header_tgt))
                    _log(
                        logger,
                        "info",
                        f"  {label}: copied header.tsv",
                        pipeline_run_id=cfg.pipeline_run_id,
                    )

            # Verify
            tgt_size = tgt.stat().st_size
            if tgt_size == src_size:
                _log(
                    logger,
                    "info",
                    f"  {label}: verified — {tgt_size:,} bytes",
                    pipeline_run_id=cfg.pipeline_run_id,
                )
            else:
                _log(
                    logger,
                    "info",
                    f"  {label}: WARNING — size mismatch ({src_size:,} vs {tgt_size:,})",
                    pipeline_run_id=cfg.pipeline_run_id,
                )

        # Also copy tags TSV — note that tags files are much smaller
        # (great-britain-tags is ~14GB, the full tag set for GB)

        _push_update(
            pipeline_run_id=cfg.pipeline_run_id,
            name="prebuild_copy_gb_to_uk", status="completed",
            message="Great-Britain → United-Kingdom TSV copy complete.",
            pct=100,
        )

        _log(
            logger,
            "info",
            "Step 0.755 complete",
            pipeline_run_id=cfg.pipeline_run_id,
        )
        return cfg.to_dict()


    @celery_app.task(
        bind=True, base=PipelineTask,
        name="step_0i_prebuild_split_embeddings",
        max_retries=1, default_retry_delay=120,
    )
    def step_0i_prebuild_split_embeddings(self, config_dict: dict) -> dict:
        """Step 0.76: Split multi-country TSVs into individual country TSVs.

        Runs the ``preprocess_embeddings`` management command to split:
          - great-britain-location → scotland, england, wales
          - malaysia-singapore-brunei-location → malaysia, singapore, brunei

        Uses osmium-based extraction (pyosmium) to collect OSM IDs within
        each country's boundary polygon, then filters the TSV by those IDs.
        No DB geometry queries needed — works even on a fresh reset.
        """
        config_dict = self.setup_pipeline_context(config_dict)
        cfg = CountryConfig.from_dict(config_dict)

        _log(
            logger,
            "info",
            "Step 0.76: Split multi-country embeddings",
            pipeline_run_id=cfg.pipeline_run_id,
        )

        _push_update(
            pipeline_run_id=cfg.pipeline_run_id,
            name="prebuild_split_embeddings", status="in_progress",
            message="Splitting multi-country TSVs (GB, MY/SG/BN)...",
            pct=10,
        )

        from django.core.management import call_command
        call_command("preprocess_embeddings")

        _push_update(
            pipeline_run_id=cfg.pipeline_run_id,
            name="prebuild_split_embeddings", status="completed",
            message="Multi-country TSV splits complete (GB, MY/SG/BN).",
            pct=100,
        )

        _log(
            logger,
            "info",
            "Step 0.76 complete",
            pipeline_run_id=cfg.pipeline_run_id,
        )
        return cfg.to_dict()

    @celery_app.task(
        bind=True, base=PipelineTask,
        name="step_0j_prebuild_merge_us_embeddings",
        max_retries=1, default_retry_delay=120,
    )
    def step_0j_prebuild_merge_us_embeddings(self, config_dict: dict) -> dict:
        """Step 0.77: Merge 5 US regional shards into single US location/tags TSVs.

        GeoVectors stores the United States as 5 regional shards:
          us-midwest, us-northeast, us-pacific, us-south, us-west

        Each shard has its own location.tsv.gz. This step concatenates them
        into a single us-location.tsv.gz under EMBEDDINGS_ROOT/north-america/us/,
        making the US eligible for the pipeline as a single country.

        Sequential streaming — reads each shard line-by-line and pipes
        directly into the gzip output. No temp files, no ThreadPool.
        """
        import time as _time
        t0 = _time.time()

        config_dict = self.setup_pipeline_context(config_dict)
        cfg = CountryConfig.from_dict(config_dict)

        _log(
            logger,
            "info",
            "Step 0.77: Merge US regional embeddings",
            pipeline_run_id=cfg.pipeline_run_id,
        )

        _push_update(
            pipeline_run_id=cfg.pipeline_run_id,
            name="prebuild_merge_us_embeddings", status="in_progress",
            message="Merging 5 US regional shards into single TSV...",
            pct=10,
        )
        # (removed ThreadPool — sequential streaming is faster for single HDD)

        from extraction.services.embedding_spatial_split_service import load_embedding_splits_config

        emb_root = Path(settings.EMBEDDINGS_ROOT)
        us_dir = emb_root / "north-america" / "us"
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
            _log(
                logger,
                "info",
                "No US merge configuration found in embedding_splits.json; skipping step 0j.",
                pipeline_run_id=pipeline_cfg.pipeline_run_id,
            )
            return pipeline_cfg.to_dict()

        shards: list = us_merge.get("shards") or []
        output_rel = us_merge.get("output") or "us-location.tsv.gz"

        def _merge_tsv_atomic(shard_paths: list, output_path: Path, label: str):
            """Merge shards sequentially with atomic write and integrity check.

            Optimized: uses binary mode + shutil.copyfileobj with large buffer
            to avoid per-line Python overhead and double gzip decode/encode.
            """
            import os
            import shutil

            tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")

            if output_path.exists():
                # Gzip integrity check before trusting existing file
                try:
                    with gzip.open(str(output_path), "rt") as f:
                        _ = f.readline()
                    _log(logger, "info", f"  {label} already exists at {output_path} — skipping")
                    return
                except OSError:
                    _log(logger, "info", f"  {label} at {output_path} is corrupt; re-merging")

            _log(logger, "info", f"  Merging {label} from {len(shard_paths)} shards (streaming) -> {output_path}")

            # 16 MB buffer for copyfileobj (reduces syscalls dramatically)
            BUF = 16 * 1024 * 1024
            # compresslevel=1 trades ~5% size for ~2x compression speed
            total = 0
            with gzip.open(str(tmp_path), "wb", compresslevel=1) as out:
                for i, rel_path in enumerate(shard_paths):
                    src = Path(emb_root) / rel_path
                    if not src.exists():
                        _log(logger, "info", f"    [{i+1}/{len(shard_paths)}] {src} NOT FOUND — skipping")
                        continue
                    row_count = 0
                    with gzip.open(str(src), "rb") as f:
                        if i > 0:
                            f.readline()  # skip header from shards 2..N
                        # copyfileobj is a C-level loop; much faster than Python per-line
                        shutil.copyfileobj(f, out, BUF)
                    _log(logger, "info", f"    [{i+1}/{len(shard_paths)}] {src.name}: merged ({_time.time()-t0:.0f}s)")

            os.replace(tmp_path, output_path)
            _log(logger, "info", f"  {label} merge complete in {_time.time()-t0:.0f}s")

        # Merge location TSV only; tags TSV merge remains intentionally skipped.
        loc_out = us_dir / output_rel
        _merge_tsv_atomic(shards, loc_out, "location TSV")

        # Copy header.tsv from first available shard directory
        header_sources = [
            emb_root / "north-america" / "us-other-location" / "header.tsv",
            emb_root / "north-america" / "us-south-location" / "header.tsv",
            emb_root / "north-america" / "us-west-tags" / "header.tsv",
        ]
        for hs in header_sources:
            if hs.exists():
                tgt_header = us_dir / "header.tsv"
                if not tgt_header.exists():
                    shutil.copy2(str(hs), str(tgt_header))
                    _log(logger, "info", f"  Copied header.tsv from {hs}")
                break

        _log(logger, "info", "  tags TSV: skipped (not needed for subgraph pickles)")

        _push_update(
            pipeline_run_id=cfg.pipeline_run_id,
            name="prebuild_merge_us_embeddings", status="completed",
            message="US regional embeddings merged.",
            pct=100,
        )

        _log(
            logger,
            "info",
            "Step 0.77 complete ({:.0f}s)".format(_time.time() - t0),
            pipeline_run_id=cfg.pipeline_run_id,
        )
        return cfg.to_dict()


    @celery_app.task(
        bind=True, base=PipelineTask,
        name="step_0k_rescan_embeddings",
        max_retries=1, default_retry_delay=60,
    )
    def step_0k_rescan_embeddings(self, config_dict: dict) -> dict:
        """Step 0.78: Re-scan embeddings after split/merge operations.

        After splits and merges complete, run scan_embeddings again so the
        EligibleCountry rows reflect the new READY statuses instead of
        NEEDS_SPLIT or NEEDS_MERGE.
        """
        config_dict = self.setup_pipeline_context(config_dict)
        cfg = CountryConfig.from_dict(config_dict)

        _log(
            logger,
            "info",
            "Step 0.78: Re-scan embeddings (post split/merge)",
            pipeline_run_id=cfg.pipeline_run_id,
        )

        _push_update(
            pipeline_run_id=cfg.pipeline_run_id,
            name="prebuild_rescan_embeddings", status="in_progress",
            message="Re-scanning embeddings after split/merge...",
            pct=10,
        )

        from django.core.management import call_command
        call_command("scan_embeddings", clear=True)

        _push_update(
            pipeline_run_id=cfg.pipeline_run_id,
            name="prebuild_rescan_embeddings", status="completed",
            message="Embedding re-scan complete — statuses updated.",
            pct=100,
        )

        _log(
            logger,
            "info",
            "Step 0.78 complete",
            pipeline_run_id=cfg.pipeline_run_id,
        )
        return cfg.to_dict()
