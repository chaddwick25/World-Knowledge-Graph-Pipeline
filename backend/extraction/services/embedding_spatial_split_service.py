import csv
import gzip
import json
import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import osmium
from django.conf import settings
from shapely.geometry import MultiPolygon, Polygon, Point
from shapely.ops import unary_union
from shapely.prepared import prep

from extraction.services.geofabrik_index_service import geofabrik_index_service
from extraction.services.country_override_service import load_overrides


logger = logging.getLogger(__name__)


@dataclass
class SplitTargetConfig:
    """Configuration for a single split target from the JSON config."""

    slug: str
    poly: str
    output: str


@dataclass
class TargetGeometry:
    """Resolved geometry and paths for a target country."""

    config: SplitTargetConfig
    poly_path: Path
    geometry: MultiPolygon
    prepared: object  # shapely prepared geometry


class AssignmentIndex:
    """Compact lookup structure for (id -> target bitmask).

    Stores sorted int64 ids with a parallel uint16 mask array and provides
    O(log N) lookups via numpy.searchsorted, avoiding a large Python dict.
    """

    def __init__(self, mapping: Dict[int, int]):
        if not mapping:
            self.ids = np.empty(0, dtype=np.int64)
            self.masks = np.empty(0, dtype=np.uint16)
            return

        ids = np.fromiter(mapping.keys(), dtype=np.int64)
        masks = np.fromiter(mapping.values(), dtype=np.uint16)
        order = np.argsort(ids)
        self.ids = ids[order]
        self.masks = masks[order]

    def lookup(self, osm_id: int) -> int:
        if self.ids.size == 0:
            return 0
        idx = np.searchsorted(self.ids, osm_id)
        if idx < self.ids.size and self.ids[idx] == osm_id:
            return int(self.masks[idx])
        return 0


def load_embedding_splits_config() -> Dict[str, List[dict]]:
    """Load the embedding splits/merges config from overrides.json.

    Reads the "embedding_splits" section from the shared overrides.json
    (via OVERRIDES_JSON_PATH). Returns a dict with ``splits`` and ``merges`` lists.
    Missing/invalid configs are treated as "no splits/merges configured"
    rather than fatal errors so that commands can degrade gracefully.
    """

    overrides = load_overrides()
    embedding_splits = overrides.get("embedding_splits", {}) if isinstance(overrides, dict) else {}

    if not embedding_splits:
        logger.info(
            "No 'embedding_splits' section found in overrides.json; no embedding splits/merges will run.",
        )
        return {"splits": [], "merges": []}

    if not isinstance(embedding_splits, dict):
        logger.error("'embedding_splits' in overrides.json is not a JSON object")
        return {"splits": [], "merges": []}

    splits = embedding_splits.get("splits", [])
    merges = embedding_splits.get("merges", [])

    # Ensure both keys are lists for downstream use
    if not isinstance(splits, list):
        logger.error("'splits' key in embedding_splits must be a list")
        splits = []
    if not isinstance(merges, list):
        logger.error("'merges' key in embedding_splits must be a list")
        merges = []

    return {"splits": splits, "merges": merges}


class EmbeddingSpatialSplitService:
    """Single-pass spatial splitter for GeoVectors location TSVs.

    Responsibilities per split group from the JSON config:

    * Ensure continent snapshot PBF exists (using osmium time-filter).
    * Resolve target polygons from POLYGON_FILES_DIR/overrides/ or root.
    * Build per-target shapely geometries and prepared PIP predicates.
    * Stream the snapshot PBF with pyosmium to assign (type, id) → targets.
    * Dispatch a single pass over the source TSV, writing per-target outputs
      atomically (``.tmp`` → ``os.rename``) with per-target stats.
    """

    def __init__(
        self,
        embeddings_root: Path,
        polygons_root: Path,
        continents_root: Path,
        stdout=None,
    ) -> None:
        self.embeddings_root = Path(embeddings_root)
        self.polygons_root = Path(polygons_root)
        self.continents_root = Path(continents_root)
        self.stdout = stdout

    # ──────────────────────────────────────────────────────────────────────
    # Logging helpers
    # ──────────────────────────────────────────────────────────────────────

    def _print(self, msg: str) -> None:
        if self.stdout is not None:
            self.stdout.write(msg)
        else:
            logger.info(msg)

    # ──────────────────────────────────────────────────────────────────────
    # Poly resolution & parsing
    # ──────────────────────────────────────────────────────────────────────

    @staticmethod
    def resolve_poly_path(polygons_root: Path, poly_name: str) -> Optional[Path]:
        """Resolve a poly filename against overrides/ then root.

        Returns a Path if found, otherwise None.
        """

        overrides_dir = polygons_root / "overrides"
        candidates = [overrides_dir / poly_name, polygons_root / poly_name]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return None

    @staticmethod
    def _parse_poly_file(poly_path: Path) -> Optional[MultiPolygon]:
        """Parse an OSM .poly file into a shapely MultiPolygon.

        Supports multiple rings and holes. The standard .poly format is:

            name
            1
              lon lat
              ...
            END
            2
              lon lat
              ...
            END
            END

        The first ring of each block is treated as the outer shell; subsequent
        rings before a trailing ``END`` are treated as holes.
        """

        if not poly_path or not poly_path.exists():
            return None

        shells: List[List[Tuple[float, float]]] = []
        holes_per_shell: List[List[List[Tuple[float, float]]]] = []

        current_ring: List[Tuple[float, float]] = []
        current_shell: Optional[List[Tuple[float, float]]] = None
        current_holes: List[List[Tuple[float, float]]] = []

        try:
            with poly_path.open("r") as f:
                lines = f.readlines()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to read poly file %s: %s", poly_path, exc)
            return None

        # Skip the first line (name) if present
        idx = 0
        if lines:
            first = lines[0].strip()
            if first and not any(ch.isdigit() for ch in first.split()):
                idx = 1

        def flush_ring() -> None:
            nonlocal current_ring, current_shell, current_holes, shells, holes_per_shell
            if not current_ring:
                return
            if current_shell is None:
                current_shell = current_ring
            else:
                current_holes.append(current_ring)
            current_ring = []

        def flush_polygon() -> None:
            nonlocal current_shell, current_holes, shells, holes_per_shell
            if current_shell is not None:
                if len(current_shell) >= 3:
                    shells.append(current_shell)
                    holes_per_shell.append(current_holes)
            current_shell = None
            current_holes = []

        for raw in lines[idx:]:
            line = raw.strip()
            if not line:
                continue
            if line == "END":
                if current_ring:
                    flush_ring()
                    continue
                # Empty ring + END → end of polygon block
                flush_polygon()
                continue

            parts = line.split()
            # Block labels like "1", "2" – start of a new ring
            if len(parts) == 1 and parts[0].isdigit():
                flush_ring()
                continue

            if len(parts) != 2:
                continue
            try:
                lon = float(parts[0])
                lat = float(parts[1])
            except ValueError:
                continue
            current_ring.append((lon, lat))

        # Flush any trailing data
        flush_ring()
        flush_polygon()

        if not shells:
            logger.warning("No rings parsed from poly file %s", poly_path)
            return None

        polygons: List[Polygon] = []
        for shell, holes in zip(shells, holes_per_shell):
            if len(shell) < 3:
                continue
            try:
                poly = Polygon(shell, holes or None)
                if not poly.is_valid:
                    poly = poly.buffer(0)
                polygons.append(poly)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to build polygon from %s: %s", poly_path, exc)

        if not polygons:
            return None

        if len(polygons) == 1:
            return MultiPolygon([polygons[0]])

        try:
            union = unary_union(polygons)
        except Exception:  # noqa: BLE001
            union = MultiPolygon(polygons)
        if isinstance(union, Polygon):
            return MultiPolygon([union])
        return union

    # ──────────────────────────────────────────────────────────────────────
    # Snapshot PBF handling (osmium time-filter)
    # ──────────────────────────────────────────────────────────────────────

    def _run_osmium(self, args: List[str], label: str = "") -> None:
        osmium_exec = getattr(settings, "OSMIUM_EXECUTABLE", None)
        if not osmium_exec:
            raise RuntimeError("OSMIUM_EXECUTABLE is not configured in settings")

        cmd = [osmium_exec] + args
        self._print(f"    $ {' '.join(cmd)}")
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=7200,
            )
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"osmium {label} failed to run: {exc}") from exc

        if result.returncode != 0:
            err = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(
                f"osmium {label} failed (rc={result.returncode}): {err[:500]}",
            )

    def ensure_continent_snapshot(
        self,
        continent: str,
        continent_pbf_name: str,
        force_snapshot: bool = False,
    ) -> Path:
        """Ensure a time-filtered continent snapshot PBF exists and return its path."""

        snapshot_path = self.continents_root / f"{continent}_snapshot.pbf"
        continent_pbf = self.continents_root / f"{continent_pbf_name}.pbf"

        if not continent_pbf.exists():
            raise FileNotFoundError(f"Continent PBF not found: {continent_pbf}")

        if snapshot_path.exists() and not force_snapshot:
            size_mb = snapshot_path.stat().st_size / (1024 * 1024)
            self._print(
                f"  Using cached snapshot: {snapshot_path} ({size_mb:.1f} MB)",
            )
            return snapshot_path

        self._print(f"\n  Creating continent snapshot: {continent}")
        self._print(f"    Source: {continent_pbf}")

        from time import time as _time

        t0 = _time()
        # Use SINGLE_SNAPSHOT_DATE year for consistency with the rest of the pipeline
        snapshot_date = getattr(settings, "SINGLE_SNAPSHOT_DATE", "2025_12_31")
        iso_ts = snapshot_date.replace("_", "-") + "T00:00:00Z"

        self._run_osmium(
            [
                "time-filter",
                str(continent_pbf),
                iso_ts,
                "-o",
                str(snapshot_path),
                "-O",
            ],
            label="time-filter",
        )

        if not snapshot_path.exists():
            raise RuntimeError("Snapshot creation FAILED for continent %s" % continent)

        elapsed = _time() - t0
        size_mb = snapshot_path.stat().st_size / (1024 * 1024)
        self._print(f"    Snapshot created: {size_mb:.1f} MB ({elapsed:.0f}s)")
        return snapshot_path

    # ──────────────────────────────────────────────────────────────────────
    # Pyosmium assignment pass
    # ──────────────────────────────────────────────────────────────────────

    def _build_assignments(
        self,
        snapshot_path: Path,
        target_geoms: List[TargetGeometry],
        bbox_padding: float = 0.0,
    ) -> Tuple[AssignmentIndex, AssignmentIndex, AssignmentIndex]:
        """Stream the snapshot PBF and compute per-type assignment indexes."""

        if not target_geoms:
            return AssignmentIndex({}), AssignmentIndex({}), AssignmentIndex({})

        # Union bbox for node pre-filtering
        union_geom = unary_union([tg.geometry for tg in target_geoms])
        minx, miny, maxx, maxy = union_geom.bounds
        minx -= bbox_padding
        miny -= bbox_padding
        maxx += bbox_padding
        maxy += bbox_padding

        prepared_geoms = [tg.prepared for tg in target_geoms]

        class AssignmentHandler(osmium.SimpleHandler):
            def __init__(self):  # type: ignore[no-untyped-def]
                super().__init__()
                self.node_assign: Dict[int, int] = {}
                self.way_assign: Dict[int, int] = {}
                self.relation_members: Dict[int, List[Tuple[str, int]]] = {}

            def node(self, n):  # type: ignore[no-untyped-def]
                if not n.location.valid():
                    return
                lon = n.location.lon
                lat = n.location.lat
                if lon < minx or lon > maxx or lat < miny or lat > maxy:
                    return
                pt = Point(lon, lat)
                mask = 0
                for idx, pg in enumerate(prepared_geoms):
                    try:
                        if pg.contains(pt):
                            mask |= 1 << idx
                    except Exception:  # noqa: BLE001
                        continue
                if mask:
                    self.node_assign[int(n.id)] = mask

            def way(self, w):  # type: ignore[no-untyped-def]
                mask = 0
                for nref in w.nodes:
                    mid = int(nref.ref)
                    m = self.node_assign.get(mid)
                    if m:
                        mask |= m
                if mask:
                    self.way_assign[int(w.id)] = mask

            def relation(self, r):  # type: ignore[no-untyped-def]
                members: List[Tuple[str, int]] = []
                for m in r.members:
                    mtype = getattr(m, "type", None)
                    ref = getattr(m, "ref", None)
                    if ref is None or mtype is None:
                        continue
                    # In pyosmium, member.type is usually 'n', 'w', or 'r'
                    if mtype == "n":
                        t = "n"
                    elif mtype == "w":
                        t = "w"
                    else:
                        t = "r"
                    members.append((t, int(ref)))
                if members:
                    self.relation_members[int(r.id)] = members

        handler = AssignmentHandler()
        handler.apply_file(str(snapshot_path))

        # Second pass: relation assignments by majority-vote over members
        relation_assign: Dict[int, int] = {}
        num_targets = len(target_geoms)

        for rel_id, members in handler.relation_members.items():
            counts = [0] * num_targets
            for t, mid in members:
                mask = 0
                if t == "n":
                    mask = handler.node_assign.get(mid, 0)
                elif t == "w":
                    mask = handler.way_assign.get(mid, 0)
                elif t == "r":
                    mask = relation_assign.get(mid, 0)
                if not mask:
                    continue
                for idx in range(num_targets):
                    if mask & (1 << idx):
                        counts[idx] += 1
            best_idx = max(range(num_targets), key=lambda i: counts[i])
            if counts[best_idx] > 0:
                relation_assign[rel_id] = 1 << best_idx

        node_index = AssignmentIndex(handler.node_assign)
        way_index = AssignmentIndex(handler.way_assign)
        relation_index = AssignmentIndex(relation_assign)
        return node_index, way_index, relation_index

    # ──────────────────────────────────────────────────────────────────────
    # TSV dispatch
    # ──────────────────────────────────────────────────────────────────────

    def _dispatch_tsv(
        self,
        source_tsv: Path,
        continent: str,
        target_geoms: List[TargetGeometry],
        node_index: AssignmentIndex,
        way_index: AssignmentIndex,
        relation_index: AssignmentIndex,
        force: bool = False,
    ) -> None:
        if not target_geoms:
            return

        if not source_tsv.exists():
            raise FileNotFoundError(f"Source TSV not found: {source_tsv}")

        # Skip if all outputs already exist (unless force)
        if not force:
            all_exist = True
            for tg in target_geoms:
                out_dir = self.embeddings_root / continent / tg.config.slug
                out_path = out_dir / tg.config.output
                if not out_path.exists():
                    all_exist = False
                    break
            if all_exist:
                self._print(
                    f"  All outputs already exist for {source_tsv.name} — skipping (use --force to re-process)",
                )
                return

        # Prepare output writers to .tmp paths
        writers: Dict[str, Tuple[gzip.GzipFile, csv.writer, Path, Path]] = {}
        for tg in target_geoms:
            out_dir = self.embeddings_root / continent / tg.config.slug
            out_dir.mkdir(parents=True, exist_ok=True)
            final_path = out_dir / tg.config.output
            tmp_path = out_dir / f"{tg.config.output}.tmp"

            f = gzip.open(str(tmp_path), "wt")
            writer = csv.writer(f, delimiter="\t", quoting=csv.QUOTE_MINIMAL)
            writers[tg.config.slug] = (f, writer, tmp_path, final_path)

            # Copy header.tsv when present
            src_header = source_tsv.parent / "header.tsv"
            if src_header.exists():
                tgt_header = out_dir / "header.tsv"
                if not tgt_header.exists():
                    try:
                        from shutil import copy2

                        copy2(str(src_header), str(tgt_header))
                    except Exception:  # noqa: BLE001
                        logger.warning("Failed to copy header.tsv from %s", src_header)

        total_rows = 0
        matched_per_target: Dict[str, int] = {tg.config.slug: 0 for tg in target_geoms}

        with gzip.open(str(source_tsv), "rt") as f:
            reader = csv.reader(f, delimiter="\t")
            for row in reader:
                if not row or row[0].startswith("#") or len(row) < 2:
                    continue
                total_rows += 1
                osm_type = row[0].strip().lower()
                try:
                    osm_id = int(row[1])
                except (ValueError, IndexError):
                    continue

                if osm_type == "n":
                    mask = node_index.lookup(osm_id)
                elif osm_type == "w":
                    mask = way_index.lookup(osm_id)
                elif osm_type == "r":
                    mask = relation_index.lookup(osm_id)
                else:
                    mask = 0

                if not mask:
                    continue

                for idx, tg in enumerate(target_geoms):
                    if mask & (1 << idx):
                        _, writer, _, _ = writers[tg.config.slug]
                        writer.writerow(row)
                        matched_per_target[tg.config.slug] += 1

        # Close and atomically rename
        for slug, (f, _writer, tmp_path, final_path) in writers.items():
            f.close()
            os.replace(tmp_path, final_path)
            self._print(
                f"    {slug}: {matched_per_target[slug]:,} matched rows -> {final_path}",
            )

        self._print(
            f"  TSV dispatch complete for {source_tsv.name}: {total_rows:,} rows processed",
        )

    # ──────────────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────────────

    def process_split_group(
        self,
        split_def: dict,
        country_filter: Optional[str] = None,
        force: bool = False,
        force_snapshot: bool = False,
    ) -> None:
        """Run the full split pipeline for a single config entry."""

        source_rel = split_def.get("source_tsv")
        continent = split_def.get("continent")
        continent_pbf = split_def.get("continent_pbf", continent)
        targets_cfg = split_def.get("targets") or []

        if not source_rel or not continent or not targets_cfg:
            return

        source_tsv = self.embeddings_root / source_rel

        target_geoms: List[TargetGeometry] = []
        for t in targets_cfg:
            slug = t.get("slug")
            poly_name = t.get("poly")
            output = t.get("output")
            if not slug or not poly_name or not output:
                continue
            if country_filter and slug != country_filter:
                continue

            poly_path = self.resolve_poly_path(self.polygons_root, poly_name)
            if not poly_path:
                raise FileNotFoundError(
                    f"Poly file not found for target {slug}: {poly_name} "
                    f"(look under {self.polygons_root}/overrides or {self.polygons_root})",
                )

            geom = self._parse_poly_file(poly_path)
            if geom is None:
                raise RuntimeError(f"Failed to parse poly file for target {slug}: {poly_path}")

            prepared = prep(geom)
            cfg = SplitTargetConfig(slug=slug, poly=poly_name, output=output)
            target_geoms.append(TargetGeometry(cfg, poly_path, geom, prepared))

        if not target_geoms:
            self._print("  No targets matched country filter; nothing to do.")
            return

        snapshot_path = self.ensure_continent_snapshot(
            continent=continent,
            continent_pbf_name=continent_pbf,
            force_snapshot=force_snapshot,
        )

        node_index, way_index, relation_index = self._build_assignments(
            snapshot_path=snapshot_path,
            target_geoms=target_geoms,
            bbox_padding=0.0,
        )

        self._dispatch_tsv(
            source_tsv=source_tsv,
            continent=continent,
            target_geoms=target_geoms,
            node_index=node_index,
            way_index=way_index,
            relation_index=relation_index,
            force=force,
        )
