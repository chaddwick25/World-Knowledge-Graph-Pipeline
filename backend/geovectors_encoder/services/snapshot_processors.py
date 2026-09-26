"""Snapshot processing workers for GeoVectors encoding.

Extracted from ``geovectors_service.py`` (monolith split, Phase 5 of
PIPELINE_CONTROL_PLANE_AND_CLEANUP_PLAN).  Module-level so they can be
passed to ``multiprocessing.Pool`` workers (fork/pickle by reference).

NOTE (pre-existing, moved faithfully): ``_process_subgraph_snapshot``
calls ``GeoVectorsEncoderService._parse_poly_file(poly_path)`` — that
method does not exist on the service (latent ``AttributeError`` whenever a
subgraph .poly file is found).  Kept as-is to preserve behavior; flagged
for owner decision.
"""

import logging
import time
from pathlib import Path

from django.conf import settings

from core.services.snapshot.regional_path_service import (
    regional_path_service,
    normalize_country_slug,
    normalize_continent_slug,
)

logger = logging.getLogger(__name__)


class DBOnlyWriter:
    """
    A minimal writer that mimics the AsyncWrite interface
    but sends data directly to VectorStorageService instead of disk.
    """
    def __init__(self, encoder, storage_service, boundary=None):
        self.encoder = encoder
        self.storage = storage_service
        self.boundary = boundary  # shapely Polygon for spatial filtering

    def add_line(self, record):
        # record: (id, type, tags, lat, lon)
        # Apply spatial filtering if boundary is set
        from shapely.geometry import Point
        if self.boundary and record[3] is not None and record[4] is not None:
            point = Point(record[4], record[3])  # lon, lat
            if not self.boundary.contains(point):
                return  # Skip entity outside boundary

        vector = self.encoder.encode_instance(record)
        if vector is not None:
            self.storage.add(record, vector)


def process_single_snapshot_combined(country_name, continent, snap_date, snap_path, session_id, force=False):
    from django.db import connections
    for conn in connections.all():
        conn.close()

    from .vector_storage_service import VectorStorageService
    from ..core.models.fasttext import FastTextModel
    from ..core.util import read_from_snapshot
    import itertools

    for conn in connections.all():
        conn.close()

    logger.info(f"[{snap_date}] (single-pass) Processing snapshot for {country_name}")

    try:
        cont_norm = normalize_country_slug(continent)
        country_norm = normalize_country_slug(country_name)

        from worldkg_nca.models import OsmEntity

        # Use a country-specific version key (date first) to avoid cross-country conflicts
        # Example: '2025_12_31_ireland_and_northern_ireland'
        version_key = f"{snap_date}_{country_norm}"

        if not force:
            if OsmEntity.objects.using("vectors").filter(gv_tags_version=version_key).exists():
                logger.info(f"[{version_key}] (single-pass) Already exists in DB for {country_name}. Skipping.")
                return
        else:
            logger.info(f"[{version_key}] (single-pass) Force mode enabled - skipping duplicate check.")

        ft_load_start = time.time()
        ft_model = FastTextModel()
        ft_load_duration = time.time() - ft_load_start
        logger.info(f"[{snap_date}] (single-pass) FastText model ready in {ft_load_duration:.2f}s")

        tags_storage = VectorStorageService(model_type="tags", version=version_key)

        # TODO(two-axis-removal): the NLE-from-pickle phase of this legacy
        # single-pass path was removed with DualEncodingWriter 2026-09-10
        # (docs/issues/TICKET_REMOVE_DUAL_ENCODER.md).  GV-NLE comes from
        # Step 5 training + the inductive query-time path, so this endpoint
        # is FastText-only regardless of pickle availability.
        tags_writer = DBOnlyWriter(ft_model, tags_storage)
        snapshot_start = time.time()
        n_data, w_data, r_data = read_from_snapshot(snap_path, writer=tags_writer, max_runs=2)
        snapshot_duration = time.time() - snapshot_start
        try:
            n_count = len(n_data) if n_data is not None else 0
        except TypeError:
            n_count = 0
        try:
            w_count = len(w_data) if w_data is not None else 0
        except TypeError:
            w_count = 0
        try:
            r_count = len(r_data) if r_data is not None else 0
        except TypeError:
            r_count = 0
        logger.info(
            f"[{snap_date}] (single-pass) FastText-only read_from_snapshot in {snapshot_duration:.2f}s "
            f"(nodes={n_count}, ways={w_count}, relations={r_count})"
        )
        for record in itertools.chain(w_data, r_data):
            tags_writer.add_line(record)
        tags_storage.flush()
        logger.info(f"[{snap_date}] (single-pass) FastText-only complete.")
    except Exception as e:
        logger.error(f"[{snap_date}] (single-pass) Failed: {e}", exc_info=True)
        raise
    finally:
        for conn in connections.all():
            conn.close()


def process_single_snapshot(country_name, continent, snap_date, snap_path, session_id, force=False):
    """
    Worker function: streams a PBF snapshot, encodes inline, writes directly to DB.
    Uses a DBOnlyWriter to prevent OOM on large countries.

    Args:
        force: If True, skip duplicate checks and process regardless
    """
    # Prevent child processes from sharing the parent's database connection socket
    from django.db import connections
    for conn in connections.all():
        conn.close()

    from .vector_storage_service import VectorStorageService
    from ..core.db import DjangoPostgresDB
    from ..core.models.fasttext import FastTextModel
    from ..core.models.nle import NLEModel
    from ..core.util import read_from_snapshot
    import itertools
    import traceback

    # Re-initialize Django connections in the child process
    for conn in connections.all():
        conn.close()

    logger.info(f"[{snap_date}] Processing snapshot for {country_name}")

    try:
        # --- Path resolution ---
        cont_norm    = normalize_country_slug(continent)
        country_norm = normalize_country_slug(country_name)
        pickle_dir   = regional_path_service.get_pickle_dir(cont_norm, country_norm)

        # --- Check if already processed (Resume Capability) ---
        from worldkg_nca.models import OsmEntity

        # Use a country-specific version key here as well (date first)
        version_key = f"{snap_date}_{country_norm}"

        # Skip duplicate check if force=True
        if not force:
            # Simple check: if any entity has this gv_tags_version, skip
            # This prevents reprocessing the same date for the same country
            if OsmEntity.objects.using('vectors').filter(gv_tags_version=version_key).exists():
                logger.info(f"[{version_key}] Already exists in DB for {country_name}. Skipping.")
                return
        else:
            logger.info(f"[{version_key}] Force mode enabled - skipping duplicate check.")

        # --- Phase 1: Tag Encoding (FastText) ---
        logger.info(f"[{snap_date}] Phase 1: FastText tag encoding...")

        phase1_start = time.time()

        # Load FastText model (can be heavy if using full cc.en.300.bin)
        ft_load_start = time.time()
        ft_model = FastTextModel()  # Uses ModelRegistryService internally
        ft_load_duration = time.time() - ft_load_start
        logger.info(
            f"[{snap_date}] Phase 1: FastText model ready in {ft_load_duration:.2f}s"
        )

        tags_storage = VectorStorageService(model_type='tags', version=version_key)

        # Use our streaming writer to prevent OOM
        tags_writer = DBOnlyWriter(ft_model, tags_storage)

        # Stream PBF — nodes stream via tags_writer.add_line()
        # w_data and r_data are still returned as lists, but we handle them next
        snapshot_start = time.time()
        n_data, w_data, r_data = read_from_snapshot(snap_path, writer=tags_writer, max_runs=2)
        snapshot_duration = time.time() - snapshot_start
        try:
            n_count = len(n_data) if n_data is not None else 0
        except TypeError:
            n_count = 0
        try:
            w_count = len(w_data) if w_data is not None else 0
        except TypeError:
            w_count = 0
        try:
            r_count = len(r_data) if r_data is not None else 0
        except TypeError:
            r_count = 0

        logger.info(
            f"[{snap_date}] Phase 1: read_from_snapshot finished in {snapshot_duration:.2f}s "
            f"(nodes={n_count}, ways={w_count}, relations={r_count})"
        )

        # Process ways and relations (nodes already done via writer)
        for record in itertools.chain(w_data, r_data):
            tags_writer.add_line(record)

        tags_storage.flush()

        phase1_duration = time.time() - phase1_start
        logger.info(f"[{snap_date}] Phase 1 complete in {phase1_duration:.2f}s.")

        # --- Phase 2: NLE Encoding (DeepWalk / wdw.pickle) ---
        # Check for both country-level pickle and subgraph pickles
        pickle_file = pickle_dir / 'wdw.pickle'
        has_pickle = pickle_file.exists()

        # If no country-level pickle, check for subgraph pickles
        if not has_pickle:
            pickles_dir = regional_path_service.get_country_dir(cont_norm, country_norm) / 'pickles'
            if pickles_dir.exists():
                # Check if any subgraph has a wdw.pickle
                for subgraph_path in pickles_dir.iterdir():
                    if subgraph_path.is_dir():
                        subgraph_pickle = subgraph_path / 'wdw.pickle'
                        if subgraph_pickle.exists():
                            has_pickle = True
                            logger.info(f"[{snap_date}] Found subgraph pickle at {subgraph_pickle}")
                            # Use the first subgraph pickle found
                            pickle_file = subgraph_pickle
                            break

        if not has_pickle:
            logger.warning(f"[{snap_date}] wdw.pickle not found (checked country-level and subgraph pickles). Skipping Phase 2.")
        else:
            logger.info(f"[{snap_date}] Phase 2: NLE location encoding using {pickle_file}...")
            db_bridge = DjangoPostgresDB()
            nle_model = NLEModel(str(pickle_file.parent), njobs=1, db=db_bridge)
            nle_model.load_indexes()

            nle_storage = VectorStorageService(model_type='nle', version=snap_date)
            nle_writer = DBOnlyWriter(nle_model, nle_storage)

            # Re-parse PBF for NLE
            n_data, w_data, r_data = read_from_snapshot(snap_path, writer=nle_writer, max_runs=2)

            for record in itertools.chain(w_data, r_data):
                nle_writer.add_line(record)

            nle_storage.flush()
            nle_model.destroy()
            logger.info(f"[{snap_date}] Phase 2 complete.")

        logger.info(f"[{snap_date}] Finished.")

    except Exception as e:
        logger.error(f"[{snap_date}] Failed: {e}", exc_info=True)
        raise
    finally:
        for conn in connections.all():
            conn.close()


def process_subgraph_snapshot(country_name, continent, subgraph_slug, subgraph_name, snap_date, snap_path, session_id, force=False):
    """
    Worker function: processes a single subgraph snapshot.

    Key differences from country-level processing:
    - Filters entities by subgraph .poly boundary
    - Uses subgraph-specific pickle for NLE encoding
    - Uses unique version identifier (snap_date + subgraph_slug)
    """
    # Close parent connections
    from django.db import connections
    for conn in connections.all():
        conn.close()

    # Re-initialize connections in child process
    for conn in connections.all():
        conn.close()

    logger.info(f"[{subgraph_name}] Processing subgraph snapshot for {country_name}")

    try:
        # Resolve paths
        cont_norm = normalize_country_slug(continent)
        country_norm = normalize_country_slug(country_name)
        country_dir = regional_path_service.get_country_dir(cont_norm, country_norm)

        # Load subgraph .poly file for spatial filtering
        # Try multiple possible locations for .poly files
        poly_path = None

        # Try 1: Look in data/osm_polygon_files (project data directory)
        base_data_dir = Path(settings.BASE_DIR) / 'data' / 'osm_polygon_files'
        poly_candidates = [
            base_data_dir / cont_norm / country_norm / f'{subgraph_slug}.poly',
            base_data_dir / cont_norm / country_norm / f'{subgraph_slug.replace("_province", "")}.poly',
            base_data_dir / cont_norm / country_norm / f'{subgraph_slug.replace("_city", "")}.poly',
        ]

        # Try 2: Look in osm_wikidata_extractions subgraphs directory using regional_path_service
        # We also check variations without _province and _city
        poly_candidates.extend([
            regional_path_service.get_subgraph_poly_path(cont_norm, country_norm, subgraph_slug),
            regional_path_service.get_subgraph_poly_path(cont_norm, country_norm, subgraph_slug.replace("_province", "")),
            regional_path_service.get_subgraph_poly_path(cont_norm, country_norm, subgraph_slug.replace("_city", "")),
        ])

        for candidate in poly_candidates:
            if candidate.exists():
                poly_path = candidate
                break

        if not poly_path:
            logger.warning(f"[{subgraph_name}] .poly file not found, skipping spatial filtering")
            boundary = None
        else:
            # Lazy import keeps the module import-order acyclic (geovectors_service
            # imports snapshot_processors).  NOTE: GeoVectorsEncoderService has no
            # _parse_poly_file method — pre-existing AttributeError, moved faithfully.
            from geovectors_encoder.services.geovectors_service import GeoVectorsEncoderService
            boundary = GeoVectorsEncoderService._parse_poly_file(poly_path)
            logger.info(f"[{subgraph_name}] Using boundary for spatial filtering from {poly_path}")

        # Subgraph-specific pickle path
        pickle_dir = country_dir / 'pickles' / subgraph_slug
        pickle_file = pickle_dir / 'wdw.pickle'

        # Unique version identifier for this subgraph
        subgraph_version = f"{snap_date}_{subgraph_slug}"

        # Phase 1: FastText tag encoding (with spatial filtering)
        logger.info(f"[{subgraph_name}] Phase 1: FastText tag encoding with spatial filtering...")

        from .vector_storage_service import VectorStorageService
        from ..core.db import DjangoPostgresDB
        from ..core.models.fasttext import FastTextModel
        from ..core.models.nle import NLEModel
        from ..core.util import read_from_snapshot
        from worldkg_nca.models import OsmEntity
        import itertools

        ft_model = FastTextModel()
        tags_storage = VectorStorageService(model_type='tags', version=subgraph_version)
        tags_writer = DBOnlyWriter(ft_model, tags_storage, boundary=boundary)

        n_data, w_data, r_data = read_from_snapshot(snap_path, writer=tags_writer, max_runs=2)

        for record in itertools.chain(w_data, r_data):
            tags_writer.add_line(record)

        tags_storage.flush()
        logger.info(f"[{subgraph_name}] Phase 1 complete.")

        # Phase 2: NLE encoding (if pickle exists)
        if not pickle_file.exists():
            logger.warning(f"[{subgraph_name}] Pickle not found at {pickle_file}, skipping Phase 2")
        else:
            logger.info(f"[{subgraph_name}] Phase 2: NLE location encoding...")
            db_bridge = DjangoPostgresDB()
            nle_model = NLEModel(str(pickle_dir), njobs=1, db=db_bridge)
            nle_model.load_indexes()

            nle_storage = VectorStorageService(model_type='nle', version=subgraph_version)
            nle_writer = DBOnlyWriter(nle_model, nle_storage, boundary=boundary)

            n_data, w_data, r_data = read_from_snapshot(snap_path, writer=nle_writer, max_runs=2)

            for record in itertools.chain(w_data, r_data):
                nle_writer.add_line(record)

            nle_storage.flush()
            nle_model.destroy()
            logger.info(f"[{subgraph_name}] Phase 2 complete.")

        logger.info(f"[{subgraph_name}] Finished.")

    except Exception as e:
        logger.error(f"[{subgraph_name}] Failed: {e}", exc_info=True)
        raise
    finally:
        for conn in connections.all():
            conn.close()
