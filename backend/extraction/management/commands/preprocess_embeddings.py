"""
Preprocess GSGV Embeddings -- Align with Geofabrik Naming (Osmium-based).

Reads override .poly files from osm_polygon_files/overrides/ and splits
multi-country GSGV TSV files by those boundaries using **osmium** to extract
OSM IDs from the continent PBF, then filtering the TSV by those IDs.

This approach does NOT depend on the vectors DB being populated, so it works
even on a fresh planet reset.

Usage:
    poetry run python manage.py preprocess_embeddings --dry-run
    poetry run python manage.py preprocess_embeddings
    poetry run python manage.py preprocess_embeddings --country scotland
    poetry run python manage.py preprocess_embeddings --country malaysia

Supported splits:
    great-britain-location  -> scotland, england, wales
    malaysia-singapore-brunei-location -> malaysia, singapore, brunei

Override poly files are expected under:
    {POLYGON_FILES_DIR}/overrides/{name}.poly

For Malaysia/Singapore/Brunei, the combined malaysia-singapore-brunei.poly is used
alongside individual country polys (downloaded or generated).

Continent PBFs are read from:
    {CONTINENT_PBFS_DIR}/{continent}.pbf
These are history files; osmium time-filter creates a snapshot first.
"""

import csv
import gzip
import logging
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from django.conf import settings
from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


# ── Canonical split definitions ──────────────────────────────────────────

GB_TARGETS = [
    {"name": "scotland", "poly_file": "scotland.poly", "output_tsv": "scotland-location.tsv.gz"},
    {"name": "england",  "poly_file": "england.poly",  "output_tsv": "england-location.tsv.gz"},
    {"name": "wales",    "poly_file": "wales.poly",    "output_tsv": "wales-location.tsv.gz"},
]

MSB_TARGETS = [
    {"name": "malaysia",  "poly_file": "malaysia-singapore-brunei.poly", "output_tsv": "malaysia-location.tsv.gz"},
    {"name": "singapore", "poly_file": "malaysia-singapore-brunei.poly", "output_tsv": "singapore-location.tsv.gz"},
    {"name": "brunei",    "poly_file": "malaysia-singapore-brunei.poly", "output_tsv": "brunei-location.tsv.gz"},
]

SPLIT_MAP = [
    {
        "source_tsv": "great-britain-location.tsv.gz",
        "source_dir": "europe/great-britain-location",
        "continent": "europe",
        "continent_pbf": "europe",
        "targets": GB_TARGETS,
    },
    {
        "source_tsv": "malaysia-singapore-brunei-location.tsv.gz",
        "source_dir": "asia/asia-location",
        "continent": "asia",
        "continent_pbf": "asia",
        "targets": MSB_TARGETS,
    },
]


# ── Osmium binary path ──────────────────────────────────────────────────

_OSMIUM_CANDIDATES = [
    getattr(settings, 'OSMIUM_EXECUTABLE', None),
    os.environ.get('OSMIUM_EXECUTABLE'),
    "/usr/bin/osmium",
    "/usr/local/bin/osmium",
    "osmium",
]
OSMIUM_BIN: Optional[str] = None
for _candidate in _OSMIUM_CANDIDATES:
    if not _candidate:
        continue
    try:
        result = subprocess.run(
            [_candidate, "--version"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            OSMIUM_BIN = _candidate
            break
    except (FileNotFoundError, subprocess.TimeoutExpired):
        continue


class Command(BaseCommand):
    help = (
        "Preprocess GSGV embeddings -- split multi-country TSVs using "
        "osmium-based OSM ID extraction (no DB needed)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--embeddings-root",
            type=str,
            default=None,
            help="Override EMBEDDINGS_ROOT path",
        )
        parser.add_argument(
            "--polygons-root",
            type=str,
            default=None,
            help="Override POLYGON_FILES_DIR path",
        )
        parser.add_argument(
            "--continents-root",
            type=str,
            default=None,
            help="Directory containing continent .pbf files",
        )
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument(
            "--country",
            type=str,
            default=None,
            help="Only process the split for a specific country target name "
                 "(e.g. --country scotland, --country malaysia). "
                 "Runs all splits if omitted.",
        )
        parser.add_argument(
            "--force-snapshot",
            action="store_true",
            help="Force re-creation of continent snapshot even if cached",
        )
        parser.add_argument(
            "--keep-temp-pbfs",
            action="store_true",
            help="Keep temporary per-country PBF extracts (for debugging)",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Force re-processing even if output TSV files already exist",
        )

    def handle(self, *args, **options):
        emb_root = Path(options["embeddings_root"]) if options["embeddings_root"] else Path(settings.EMBEDDINGS_ROOT)
        polys_root = Path(options["polygons_root"]) if options["polygons_root"] else Path(settings.POLYGON_FILES_DIR)
        continents_root = Path(options["continents_root"]) if options["continents_root"] else Path(settings.CONTINENTS_ROOT)
        dry_run = options["dry_run"]
        country_filter = options.get("country")
        force_snapshot = options["force_snapshot"]
        keep_temp = options["keep_temp_pbfs"]
        force = options["force"]

        if OSMIUM_BIN is None:
            self.stdout.write(self.style.ERROR(
                "osmium binary not found. Install osmium-tool and set OSMIUM_BINARY_PATH."
            ))
            return

        if not polys_root.exists():
            self.stdout.write(self.style.ERROR(f"Polygons root not found: {polys_root}"))
            return

        overrides_dir = polys_root / "overrides"
        if not overrides_dir.exists():
            self.stdout.write(self.style.WARNING(
                f"Overrides directory not found: {overrides_dir}\n"
                "  Run: python manage.py download_poly_files --overrides"
            ))
            return

        if not emb_root.exists():
            self.stdout.write(self.style.ERROR(f"Embeddings root not found: {emb_root}"))
            return

        self.stdout.write("=" * 60)
        self.stdout.write("GSGV Embedding Preprocessing (Osmium-based)")
        self.stdout.write(f"  Embeddings: {emb_root}")
        self.stdout.write(f"  Overrides: {overrides_dir}")
        self.stdout.write(f"  Continents: {continents_root}")
        self.stdout.write(f"  Osmium: {OSMIUM_BIN}")
        self.stdout.write(f"  Dry run: {dry_run}")
        if country_filter:
            self.stdout.write(f"  Country filter: {country_filter}")
        self.stdout.write("=" * 60)

        total_start = time.time()

        for sc in SPLIT_MAP:
            if country_filter:
                target_names = {t["name"] for t in sc["targets"]}
                if country_filter not in target_names:
                    continue

            source_tsv = emb_root / sc["source_dir"] / sc["source_tsv"]
            if not source_tsv.exists():
                self.stdout.write(f"\n  Skipping {sc['source_tsv']}: not found at {source_tsv}")
                continue

            filtered_targets = sc["targets"]
            if country_filter:
                filtered_targets = [t for t in sc["targets"] if t["name"] == country_filter]

            # Skip if all output TSVs for this group already exist (unless --force)
            if not force:
                all_exist = all(
                    (emb_root / sc["continent"] / t["name"] / t["output_tsv"]).exists()
                    for t in filtered_targets
                )
                if all_exist:
                    self.stdout.write(
                        f"  All output TSVs already exist for {sc['source_tsv']} "
                        f"— skipping (use --force to re-process)"
                    )
                    continue

            polys = {}
            for tgt in filtered_targets:
                poly_path = overrides_dir / tgt["poly_file"]
                if poly_path.exists():
                    polys[tgt["name"]] = poly_path
                else:
                    self.stdout.write(self.style.WARNING(f"  WARNING: Override poly not found: {poly_path}"))

            if not polys:
                self.stdout.write(f"  No override poly files found for {sc['source_tsv']}")
                continue

            if dry_run:
                self.stdout.write(f"\n  [DRY-RUN] Would split {source_tsv} into:")
                for name, p in polys.items():
                    self.stdout.write(f"    -> {name} ({p.name})")
                continue

            self._process_split(
                sc, source_tsv, polys, emb_root, continents_root,
                force_snapshot=force_snapshot, keep_temp=keep_temp, force=force,
            )

        elapsed = time.time() - total_start
        self.stdout.write(f"\n{'=' * 60}")
        self.stdout.write(f"Preprocessing complete in {elapsed:.1f}s")
        self.stdout.write(f"{'=' * 60}")

    # ═══════════════════════════════════════════════════════════════════
    # Core split logic
    # ═══════════════════════════════════════════════════════════════════

    def _process_split(
        self,
        sc: dict,
        source_tsv: Path,
        polys: Dict[str, Path],
        emb_root: Path,
        continents_root: Path,
        force_snapshot: bool = False,
        keep_temp: bool = False,
        force: bool = False,
    ):
        continent = sc["continent"]
        continent_pbf = continents_root / f"{sc['continent_pbf']}.pbf"

        if not continent_pbf.exists():
            self.stdout.write(self.style.ERROR(f"  Continent PBF not found: {continent_pbf}"))
            return

        # ── Step 1: Ensure continent snapshot exists ────────────────
        snapshot_path = continents_root / f"{continent}_snapshot.pbf"
        if not snapshot_path.exists() or force_snapshot:
            self.stdout.write(f"\n  Creating continent snapshot: {continent}")
            self.stdout.write(f"    Source: {continent_pbf}")
            t0 = time.time()
            self._run_osmium([
                "time-filter",
                str(continent_pbf),
                "2025-12-31T00:00:00Z",
                "-o", str(snapshot_path),
                "-O",
            ], label="time-filter")
            elapsed = time.time() - t0
            if snapshot_path.exists():
                size_mb = snapshot_path.stat().st_size / (1024 * 1024)
                self.stdout.write(f"    Snapshot created: {size_mb:.1f} MB ({elapsed:.0f}s)")
            else:
                self.stdout.write(self.style.ERROR(f"    Snapshot creation FAILED"))
                return
        else:
            size_mb = snapshot_path.stat().st_size / (1024 * 1024)
            self.stdout.write(f"\n  Using cached snapshot: {snapshot_path} ({size_mb:.1f} MB)")

        # ── Step 2: Group targets by poly file to avoid redundant extractions ──
        poly_to_targets: Dict[str, List[dict]] = {}
        for name, poly_path in polys.items():
            poly_key = str(poly_path)
            if poly_key not in poly_to_targets:
                poly_to_targets[poly_key] = []
            for tgt in sc["targets"]:
                if tgt["name"] == name:
                    poly_to_targets[poly_key].append({**tgt, "poly_path": poly_path})
                    break

        temp_dir = Path(tempfile.mkdtemp(prefix="osmium_split_"))
        try:
            for poly_key, targets in poly_to_targets.items():
                poly_path = Path(poly_key)

                # Skip if all output TSVs for this poly already exist (unless --force)
                if not force:
                    all_exist = all(
                        (emb_root / continent / t["name"] / t["output_tsv"]).exists()
                        for t in targets
                    )
                    if all_exist:
                        self.stdout.write(
                            f"  All outputs already exist for poly {poly_path.name} "
                            f"— skipping (use --force to re-process)"
                        )
                        continue

                self.stdout.write(f"\n  Processing poly: {poly_path.name}")

                # Extract PBF for this polygon
                country_pbf = temp_dir / f"{poly_path.stem}.pbf"
                if not country_pbf.exists():
                    t0 = time.time()
                    self._run_osmium([
                        "extract",
                        "--polygon", str(poly_path),
                        str(snapshot_path),
                        "-o", str(country_pbf),
                        "-O",
                    ], label=f"extract {poly_path.stem}")
                    elapsed = time.time() - t0
                    if country_pbf.exists():
                        size_mb = country_pbf.stat().st_size / (1024 * 1024)
                        self.stdout.write(f"    Extracted PBF: {size_mb:.1f} MB ({elapsed:.0f}s)")
                    else:
                        self.stdout.write(self.style.ERROR(f"    Extraction FAILED"))
                        continue

                # Collect OSM IDs from the extracted PBF
                t0 = time.time()
                osm_ids = self._collect_osm_ids(country_pbf)
                elapsed = time.time() - t0
                self.stdout.write(f"    OSM IDs collected: {len(osm_ids):,} ({elapsed:.1f}s)")

                if not osm_ids:
                    self.stdout.write(self.style.WARNING(f"    No OSM IDs found -- skipping"))
                    continue

                # Simple case: one target per poly
                if len(targets) == 1:
                    self._filter_tsv(source_tsv, targets[0], osm_ids, emb_root, continent)
                else:
                    # MSB case (all targets share one poly)
                    self._split_msb_tsv(
                        source_tsv, targets, country_pbf, osm_ids, emb_root, continent,
                    )
        finally:
            if not keep_temp:
                shutil.rmtree(str(temp_dir), ignore_errors=True)

    # ═══════════════════════════════════════════════════════════════════
    # Osmium helper
    # ═══════════════════════════════════════════════════════════════════

    def _run_osmium(self, args: List[str], label: str = ""):
        """Run an osmium command and stream output."""
        cmd = [OSMIUM_BIN] + args
        self.stdout.write(f"    $ {' '.join(cmd)}")
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=7200,
        )
        if result.returncode != 0:
            err = result.stderr.strip()
            self.stdout.write(self.style.ERROR(
                f"    osmium {label} FAILED (rc={result.returncode}): {err[:500]}"
            ))
            raise RuntimeError(f"osmium {label} failed: {err[:500]}")
        return result

    # ═══════════════════════════════════════════════════════════════════
    # Pyosmium OSM ID collection
    # ═══════════════════════════════════════════════════════════════════

    def _collect_osm_ids(self, pbf_path: Path) -> Set[Tuple[str, int]]:
        """Read a PBF with pyosmium and collect all (osm_type, osm_id) pairs."""
        import osmium

        class IDCollector(osmium.SimpleHandler):
            def __init__(self):
                super().__init__()
                self.ids: Set[Tuple[str, int]] = set()

            def node(self, n):
                self.ids.add(('n', n.id))

            def way(self, w):
                self.ids.add(('w', w.id))

            def relation(self, r):
                self.ids.add(('r', r.id))

        collector = IDCollector()
        collector.apply_file(str(pbf_path))
        return collector.ids

    # ═══════════════════════════════════════════════════════════════════
    # TSV filtering
    # ═══════════════════════════════════════════════════════════════════

    def _filter_tsv(
        self,
        source_tsv: Path,
        target: dict,
        osm_ids: Set[Tuple[str, int]],
        emb_root: Path,
        continent: str,
    ):
        name = target["name"]
        output_tsv_name = target["output_tsv"]
        out_dir = emb_root / continent / name
        out_dir.mkdir(parents=True, exist_ok=True)
        out_tsv = out_dir / output_tsv_name

        if out_tsv.exists():
            line_count = self._gz_line_count(out_tsv)
            self.stdout.write(f"    {name}: already exists at {out_tsv} ({line_count:,} rows)")
            return

        src_header = source_tsv.parent / "header.tsv"
        if src_header.exists():
            tgt_header = out_dir / "header.tsv"
            if not tgt_header.exists():
                shutil.copy2(str(src_header), str(tgt_header))

        self.stdout.write(f"    {name}: filtering TSV -> {out_tsv}")

        matched = 0
        total = 0
        start = time.time()

        with gzip.open(str(out_tsv), "wt") as out_gz:
            writer = csv.writer(out_gz, delimiter="\t", quoting=csv.QUOTE_MINIMAL)

            with gzip.open(str(source_tsv), "rt") as f:
                reader = csv.reader(f, delimiter="\t")
                for row in reader:
                    if not row or row[0].startswith("#") or len(row) < 3:
                        continue
                    try:
                        key = (row[0].strip().lower(), int(row[1]))
                    except (ValueError, IndexError):
                        continue

                    if key in osm_ids:
                        writer.writerow(row)
                        matched += 1

                    total += 1
                    if total % 500000 == 0:
                        elapsed = time.time() - start
                        rate = total / elapsed if elapsed > 0 else 0
                        self.stdout.write(
                            f"      ... {total:,} processed, {matched:,} matched ({rate:.0f} rows/s)"
                        )

        elapsed = time.time() - start
        rate = total / elapsed if elapsed > 0 else 0
        self.stdout.write(f"    {name}: {total:,} processed, {matched:,} matched ({rate:.0f} rows/s)")

    # ═══════════════════════════════════════════════════════════════════
    # MSB-specific handling (all share one poly)
    # ═══════════════════════════════════════════════════════════════════

    def _split_msb_tsv(
        self,
        source_tsv: Path,
        targets: List[dict],
        combined_pbf: Path,
        all_osm_ids: Set[Tuple[str, int]],
        emb_root: Path,
        continent: str,
    ):
        """Split MSB TSV using admin boundary relations from the combined PBF."""
        import osmium

        # OSM boundary relation IDs for MSB countries
        MSB_RELATIONS = {
            2108121: "malaysia",
            536780: "singapore",
            2103120: "brunei",
        }

        self.stdout.write("    Reading combined PBF to assign OSM IDs by relation membership...")

        class MSBRelationCollector(osmium.SimpleHandler):
            def __init__(self):
                super().__init__()
                self.country_ids: Dict[str, Set[Tuple[str, int]]] = {
                    "malaysia": set(),
                    "singapore": set(),
                    "brunei": set(),
                }

            def relation(self, r):
                country = MSB_RELATIONS.get(r.id)
                if not country:
                    return
                for member in r.members:
                    mtype = member.type
                    otype = "n" if mtype == "n" else ("w" if mtype == "w" else "r")
                    self.country_ids[country].add((otype, member.ref))

        collector = MSBRelationCollector()
        collector.apply_file(str(combined_pbf))

        total_assigned = sum(len(v) for v in collector.country_ids.values())
        if total_assigned == 0:
            self.stdout.write(
                self.style.WARNING(
                    "    No relation-based assignments found. Using all IDs "
                    "for Malaysia (largest share). Singapore and Brunei skipped."
                )
            )
            target = next((t for t in targets if t["name"] == "malaysia"), None)
            if target:
                self._filter_tsv(source_tsv, target, all_osm_ids, emb_root, continent)
            return

        for tgt_def in targets:
            name = tgt_def["name"]
            ids = collector.country_ids.get(name, set())
            if not ids:
                self.stdout.write(f"      {name}: no OSM IDs found")
                continue
            self.stdout.write(f"      {name}: {len(ids):,} OSM IDs from relation membership")
            self._filter_tsv(source_tsv, tgt_def, ids, emb_root, continent)

    # ═══════════════════════════════════════════════════════════════════
    # Utilities
    # ═══════════════════════════════════════════════════════════════════

    def _gz_line_count(self, path: Path) -> int:
        count = 0
        with gzip.open(str(path), "rt") as f:
            for _ in f:
                count += 1
        return count
