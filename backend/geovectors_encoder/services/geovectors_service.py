import os
import logging
import multiprocessing
import time
from datetime import datetime
from pathlib import Path
from django.conf import settings
from django.utils import timezone
import gzip
import numpy as np
import pickle
from django.db import connections, models
from shapely.geometry import Point

from core.models import ProcessingSession, Task, CountryPipelineProfile, SubgraphProfile
from core.models import RegionHierarchy
from core.services.snapshot.regional_path_service import regional_path_service, normalize_country_slug, normalize_continent_slug
from core.services.planet_init.osm_wikidata_resolver import get_country_by_name, get_country_relations_dict
# Internal imports from core
from ..core.encoder import run_on_dump
from ..core.db import DjangoPostgresDB
from ..core.models.fasttext import FastTextModel
from ..core.models.nle import NLEModel

logger = logging.getLogger(__name__)

# Feature flag: enable single-pass PBF encoding (FastText+NLE in one traversal)
GEOVECTORS_SINGLE_PASS = getattr(settings, "GEOVECTORS_SINGLE_PASS", True)

# Simple class to mimic the NLEModel expectation
class WDWStore(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.n_components = 100
        
    def predict(self, key):
        import numpy as np
        return self.get(key, np.zeros(self.n_components, dtype=np.float32))
        
    def predict(self, key):
        import numpy as np
        return self.get(key, np.zeros(self.n_components, dtype=np.float32))

class GeoVectorsEncoderService:
    """
    Service to orchestrate parallel encoding of OSM snapshots using GeoVectors.
    Also handles pre-processing (pickle generation) from local pre-trained embeddings.
    """

    def __init__(self, n_jobs=None):
        self.n_jobs = n_jobs or 4

    def _resolve_iso_code(self, country_name):
        """Resolve ISO code from country name using OSMWikiDataHierarchy."""
        country_data = get_country_by_name(country_name)
        if country_data:
            iso_code = country_data.get('wikidata_id')
            if iso_code:
                return iso_code.upper()
        return None

    def _has_subgraphs(self, country_name, continent, iso_code):
        """Check if country has subgraphs.

        Priority:
          1. SubgraphProfile rows (DB metadata).
          2. Filesystem fallback via subgraph_list_service (disk directory structure).
        """
        iso = (iso_code or "").upper()
        if not iso:
            return False

        # ── 1. DB check using CountryPipelineProfile/SubgraphProfile ──
        try:
            country_profile = (
                CountryPipelineProfile.objects.filter(
                    models.Q(iso2__iexact=iso) | models.Q(iso3__iexact=iso)
                ).first()
            )
            if not country_profile:
                slug = normalize_country_slug(country_name)
                country_profile = (
                    CountryPipelineProfile.objects.filter(
                        canonical_slug__iexact=slug
                    ).first()
                )

            if country_profile and SubgraphProfile.objects.filter(
                country_profile=country_profile,
                has_subgraph_pbf=True,
            ).exists():
                return True
        except Exception as e:
            logger.warning(
                "GeoVectors: DB subgraph check failed for %s (%s): %s",
                country_name,
                iso,
                e,
            )

        # ── 2. Filesystem fallback ──
        try:
            from core.services.snapshot.subgraph_list_service import build_subgraph_list
            subgraphs = build_subgraph_list(country_name, auto_all=True)
            if subgraphs:
                logger.info(
                    "GeoVectors: Discovered %d subgraphs for %s via filesystem",
                    len(subgraphs), country_name,
                )
                return True
        except Exception as e:
            logger.warning(
                "GeoVectors: Filesystem subgraph check failed for %s: %s",
                country_name, e,
            )

        return False

    def _get_subgraphs(self, country_name, iso_code):
        """Return list of subgraphs with names and normalized slugs.

        Priority:
          1. SubgraphProfile rows (DB metadata).
          2. Filesystem fallback via subgraph_list_service (disk directory structure).
        """
        iso = (iso_code or "").upper()
        subgraphs = []

        # ── 1. DB resolution ──
        try:
            country_profile = (
                CountryPipelineProfile.objects.filter(
                    models.Q(iso2__iexact=iso) | models.Q(iso3__iexact=iso)
                ).first()
            )
            if not country_profile:
                slug = normalize_country_slug(country_name)
                country_profile = (
                    CountryPipelineProfile.objects.filter(
                        canonical_slug__iexact=slug
                    ).first()
                )

            if country_profile:
                for profile in SubgraphProfile.objects.filter(
                    country_profile=country_profile,
                    has_subgraph_pbf=True,
                ):
                    subgraphs.append(
                        {
                            "name": profile.name,
                            "slug": normalize_country_slug(profile.name),
                            "relation_id": profile.osm_relation_id,
                        }
                    )
                if subgraphs:
                    return subgraphs
        except Exception as e:
            logger.warning(
                "GeoVectors: DB subgraph listing failed for %s (%s): %s",
                country_name,
                iso,
                e,
            )

        # ── 2. Filesystem fallback ──
        try:
            from core.services.snapshot.subgraph_list_service import build_subgraph_list
            fs_subgraphs = build_subgraph_list(country_name, auto_all=True)
            for sg in fs_subgraphs:
                subgraphs.append({
                    "name": sg["name"],
                    "slug": sg["slug"],
                    "relation_id": sg.get("relation_id"),
                })
            if subgraphs:
                logger.info(
                    "GeoVectors: Loaded %d subgraphs for %s via filesystem",
                    len(subgraphs), country_name,
                )
        except Exception as e:
            logger.warning(
                "GeoVectors: Filesystem subgraph listing failed for %s: %s",
                country_name, e,
            )

        return subgraphs

    def generate_subgraph_pickle(self, country_name: str, subgraph_name: str, continent: str = None, overwrite: bool = False) -> dict:
        """
        Generate a GeoVectors pickle for a specific subgraph (e.g., a city/admin division).

        The pickle is stored under the country's osm_wikidata_extractions path:
        OSM_WIKIDATA_EXTRACTIONS_DIR/{continent}/{country}/pickles/{subgraph}/wdw.pickle

        DB-first resolution: use CountryPipelineProfile + SubgraphProfile for TSV and
        subgraph PBF/poly paths; fall back to the legacy hierarchy JSON only when
        DB metadata is incomplete, for backward compatibility.

        Args:
            country_name: Country slug/name (e.g., "Ireland" or "ireland").
            subgraph_name: Subgraph name (e.g., "Dublin").
            continent: Optional continent (auto-resolved if not provided).
            overwrite: If True, regenerate existing pickle instead of skipping.

        Returns:
            {
                "success": bool,
                "path": str or None,
                "entities": int or None,
                "country": str,
                "subgraph": str,
                "error": str or None,
            }
        """
        from pathlib import Path
        from django.db import models

        # Normalized identifiers
        country_slug = normalize_country_slug(country_name)
        subgraph_normalized = normalize_country_slug(subgraph_name)

        country_profile = None
        subgraph_profile = None
        tsv_path = None
        subgraph_pbf = None
        poly_file = None

        # 1. DB-first resolution: CountryPipelineProfile + SubgraphProfile
        try:
            country_profile = (
                CountryPipelineProfile.objects.filter(
                    models.Q(canonical_slug__iexact=country_slug)
                    | models.Q(iso2__iexact=country_slug)
                    | models.Q(iso3__iexact=country_slug)
                ).first()
            )
            if country_profile:
                subgraph_profile = (
                    SubgraphProfile.objects.filter(country_profile=country_profile)
                    .filter(slug__iexact=subgraph_normalized)
                    .first()
                )
        except Exception as exc:
            logger.warning(
                "GeoVectors: DB lookup for subgraph %s/%s failed: %s",
                country_name,
                subgraph_name,
                exc,
            )

        if country_profile and subgraph_profile:
            # Resolve TSV path from CountryPipelineProfile payload when available
            rel_payload = country_profile.country_relations_payload or {}
            tsv_rel = rel_payload.get("geovectors_location_tsv")
            if tsv_rel:
                tsv_path = Path(tsv_rel)
                if not tsv_path.is_absolute():
                    tsv_path = Path(settings.EMBEDDINGS_ROOT) / tsv_path
            else:
                # Fallback: derive from embedding_root_path conventionally
                try:
                    root_rel = country_profile.embedding_root_path or ""
                    if root_rel:
                        candidate = Path(settings.EMBEDDINGS_ROOT) / root_rel / "locations.tsv.gz"
                        if candidate.exists():
                            tsv_path = candidate
                except Exception:
                    tsv_path = None

            # Resolve subgraph PBF/poly from SubgraphProfile
            if subgraph_profile.subgraph_pbf_path:
                subgraph_pbf = Path(subgraph_profile.subgraph_pbf_path)
            if subgraph_profile.subgraph_poly_path:
                poly_file = Path(subgraph_profile.subgraph_poly_path)

            # Resolve continent from DB if not provided
            if not continent:
                continent = country_profile.continent_name or None

        # 2. Resolve any remaining TSV path from country_relations overlay (DB-backed)
        if tsv_path is None:
            country_relations = get_country_relations_dict()
            search_term = country_slug
            for k, v in country_relations.items():
                slug = normalize_country_slug(v.get("slug", ""))
                name = normalize_country_slug(v.get("name", ""))
                if (
                    slug == search_term
                    or name == search_term
                    or search_term in slug
                    or slug in search_term
                ):
                    tsv_rel = v.get("geovectors_location_tsv")
                    if tsv_rel:
                        tsv_path = Path(tsv_rel)
                        if not tsv_path.is_absolute():
                            tsv_path = Path(settings.EMBEDDINGS_ROOT) / tsv_path
                    break

        # If DB did not provide subgraph PBF/poly, derive paths from regional_path_service
        if subgraph_pbf is None:
            if not continent:
                region = RegionHierarchy.objects.filter(name__iexact=country_name).first()
                continent = region.parent.name if region and region.parent else "central-america"
            subgraph_pbf = regional_path_service.get_subgraph_pbf_path(
                continent, country_name, subgraph_normalized
            )
        if poly_file is None:
            if not continent:
                region = RegionHierarchy.objects.filter(name__iexact=country_name).first()
                continent = region.parent.name if region and region.parent else "central-america"
            poly_file = regional_path_service.get_subgraph_poly_path(
                continent, country_name, subgraph_normalized
            )

        # 3. Compute pickle path: {OSM_WIKIDATA_EXTRACTIONS_DIR}/{continent}/{country}/pickles/{subgraph}/wdw.pickle
        if not continent:
            region = RegionHierarchy.objects.filter(name__iexact=country_name).first()
            continent = region.parent.name if region and region.parent else "central-america"

        base_extractions = Path(settings.OSM_WIKIDATA_EXTRACTIONS_DIR)
        country_dir = base_extractions / continent.lower() / country_slug
        pickle_dir = country_dir / "pickles" / subgraph_normalized
        pickle_dir.mkdir(parents=True, exist_ok=True)
        output_pickle = pickle_dir / "wdw.pickle"

        # Skip if already exists (unless overwrite is True)
        if not overwrite and output_pickle.exists():
            logger.info("GeoVectors: Subgraph pickle already exists at %s, skipping", output_pickle)
            return {
                "success": True,
                "skipped": True,
                "path": str(output_pickle),
                "country": country_name,
                "subgraph": subgraph_name,
            }

        # 6. Ensure subgraph poly file for geo-fencing is available
        if poly_file is None:
            poly_file = regional_path_service.get_subgraph_poly_path(
                continent, country_name, subgraph_normalized
            )

        if not poly_file.exists():
            logger.warning("GeoVectors: Subgraph poly file not found at %s, skipping geo-fenced pickle", poly_file)
            return {"success": True, "skipped": True, "country": country_name, "subgraph": subgraph_name}

        # 7. Resolve TSV path (country-level embeddings)
        if tsv_path is None:
            logger.info("GeoVectors: No TSV for country %s, cannot generate subgraph pickle", country_name)
            return {"success": True, "skipped": True, "country": country_name, "subgraph": subgraph_name}

        if not tsv_path.exists():
            logger.warning("GeoVectors: TSV file not found at %s", tsv_path)
            return {"success": False, "error": "TSV file not found"}

        # 8. Filter embeddings using subgraph PBF IDs (Existing Pattern: ID-based filtering)
        # Instead of parsing poly files (which requires coordinates not in TSV),
        # we use the subgraph PBF itself as the filter. If it exists in the PBF, it's in the subgraph.
        if not subgraph_pbf or not subgraph_pbf.exists():
            logger.warning(
                "GeoVectors: Subgraph PBF not found at %s, cannot filter. Falling back to country-wide.",
                subgraph_pbf,
            )
            subgraph_ids = None
        else:
            logger.info("GeoVectors: Extracting entity IDs from subgraph PBF: %s", subgraph_pbf)
            try:
                import osmium
                class IdHandler(osmium.SimpleHandler):
                    def __init__(self):
                        super().__init__()
                        self.ids = set()
                    def node(self, n):
                        self.ids.add(f"node_{n.id}")
                    def way(self, w):
                        self.ids.add(f"way_{w.id}")
                    def relation(self, r):
                        self.ids.add(f"relation_{r.id}")
                
                handler = IdHandler()
                handler.apply_file(str(subgraph_pbf))
                subgraph_ids = handler.ids
                logger.info("GeoVectors: Found %d unique IDs in subgraph PBF", len(subgraph_ids))
            except Exception as e:
                logger.error("GeoVectors: Failed to extract IDs from PBF: %s. Falling back to country-wide.", e)
                subgraph_ids = None

        # 9. Build pickle by filtering country-level embeddings
        logger.info(
            "GeoVectors: Building filtered subgraph pickle for %s/%s from %s...",
            country_name,
            subgraph_name,
            tsv_path,
        )

        wdw_store = WDWStore()
        filtered_count = 0
        total_count = 0

        with gzip.open(tsv_path, "rt") as f:
            for line in f:
                parts = line.strip().split("\t")
                if len(parts) < 3:
                    continue

                osm_type_code, osm_id = parts[0], parts[1]
                
                # Map short 'n', 'w', 'r' to 'node', 'way', 'relation' to match PBF IDs
                type_map = {'n': 'node', 'w': 'way', 'r': 'relation'}
                osm_type = type_map.get(osm_type_code.lower(), osm_type_code)
                
                key = f"{osm_type}_{osm_id}"
                total_count += 1
                
                # Filter by IDs if available
                if subgraph_ids is not None and key not in subgraph_ids:
                    continue

                vector = np.array(parts[2:], dtype=np.float32)
                wdw_store[key] = vector
                filtered_count += 1
                
        logger.info(
            "GeoVectors: Filtered %d/%d embeddings for subgraph %s",
            filtered_count,
            total_count,
            subgraph_name,
        )

        with open(output_pickle, "wb") as f:
            pickle.dump(wdw_store, f)

        logger.info(
            "GeoVectors: SUCCESS - Subgraph pickle for %s/%s with %d entities at %s",
            country_name,
            subgraph_name,
            filtered_count,
            output_pickle,
        )

        # Best-effort DB update for SubgraphProfile
        if country_profile and subgraph_profile:
            try:
                subgraph_profile.subgraph_pickle_path = str(output_pickle)
                subgraph_profile.has_subgraph_pickle = True
                subgraph_profile.metadata_status = SubgraphProfile.MetadataStatus.OK
                subgraph_profile.save(update_fields=[
                    "subgraph_pickle_path",
                    "has_subgraph_pickle",
                    "metadata_status",
                    "updated_at",
                ])
            except Exception as exc:
                logger.warning(
                    "GeoVectors: Failed to update SubgraphProfile for %s/%s: %s",
                    country_name,
                    subgraph_name,
                    exc,
                )

        return {
            "success": True,
            "path": str(output_pickle),
            "entities": len(wdw_store),
            "country": country_name,
            "subgraph": subgraph_name,
        }


    def run_for_country(self, country_name, continent=None, force=False):
        """
        Triggers encoding for all monthly snapshots of a country.
        
        Args:
            force: If True, skip duplicate checks and process all snapshots
        """
        # Resolve ISO code
        iso_code = self._resolve_iso_code(country_name)
        
        # Normalize name for DB lookup (e.g. "Turks and Caicos Islands" -> "turks_and_caicos_islands")
        norm_name = normalize_country_slug(country_name)

        # 1. Identify continent if not provided
        if not continent:
            from core.models import RegionHierarchy
            # Try exact match first
            region = RegionHierarchy.objects.filter(name__iexact=norm_name).first()
            if not region:
                region = RegionHierarchy.objects.filter(name__iexact=country_name).first()
            
            # Try partial match if still not found
            if not region:
                region = RegionHierarchy.objects.filter(name__icontains=country_name.split(' ')[0]).first()

            if region and region.parent:
                continent = region.parent.name
            elif region and not region.parent:
                # If it's a top-level node, it might BE the continent
                continent = region.name
        
        # 2. Hardcoded fallbacks for common regions if DB lookup fails
        if not continent:
            if any(x in norm_name for x in ['islands', 'jamaica', 'cuba', 'haiti', 'dominican']):
                continent = 'central-america'
            elif any(x in norm_name for x in ['canada', 'usa', 'united_states', 'mexico']):
                continent = 'north-america'

        if not continent:
            raise ValueError(f"Could not identify continent for {country_name}. Please specify --continent.")

        # 3. Check if country has subgraphs
        if iso_code and self._has_subgraphs(country_name, continent, iso_code):
            logger.info(f"Country {country_name} has subgraphs, running per-subgraph encoding")
            return self.run_for_subgraphs(country_name, continent, iso_code, force)
        else:
            logger.info(f"Country {country_name} has no subgraphs, running country-level encoding")
            return self._run_country_level_encoding(country_name, continent, force)

    def _run_country_level_encoding(self, country_name, continent, force=False):
        """
        Run country-level encoding (original implementation).
        Used as fallback for countries without subgraphs.
        """
        # Normalize name for DB lookup
        norm_name = normalize_country_slug(country_name)

        # Find snapshots
        snapshots = self._find_snapshots(continent, country_name)

        if not snapshots:
            logger.warning(f"No snapshots found for {country_name} in {continent}")
            return None

        # Create Processing Session
        session = ProcessingSession.objects.create(
            session_name=f"GeoVectors Encoding: {country_name}",
            session_type='GEOVECTORS_ENCODING',
            status='IN_PROGRESS',
            configuration={
                'country_name': country_name,
                'continent': continent,
                'snapshot_count': len(snapshots)
            }
        )

        # Run in parallel
        logger.info(f"Starting GeoVectors encoding for {country_name} with {self.n_jobs} jobs")
        
        tasks_args = []
        for snap_date, snap_path in snapshots.items():
            tasks_args.append((
                country_name,
                continent,
                snap_date,
                str(snap_path),
                str(session.id),
                force
            ))

        with multiprocessing.Pool(processes=self.n_jobs) as pool:
            try:
                if GEOVECTORS_SINGLE_PASS:
                    worker = self._process_single_snapshot_combined
                else:
                    worker = self._process_single_snapshot
                pool.starmap(worker, tasks_args)
            except Exception as e:
                logger.error(f"Multiprocessing pool failed: {e}", exc_info=True)
                session.status = 'FAILED'
                session.completed_at = timezone.now()
                session.save()
                raise

        # Close old connections that might have been broken by child processes
        from django.db import connections
        for conn in connections.all():
            conn.close()

        # Complete Session
        session.status = 'COMPLETED'
        session.completed_at = timezone.now()
        session.save()

        return session

    def run_for_subgraphs(self, country_name, continent, iso_code, force=False):
        """
        Run GeoVectors encoding for each subgraph in parallel.
        
        Args:
            country_name: Country name
            continent: Continent name
            iso_code: ISO code (e.g., "CL")
            force: If True, skip duplicate checks
        """
        # Get subgraph list
        subgraphs = self._get_subgraphs(country_name, iso_code)
        
        if not subgraphs:
            logger.warning(f"No subgraphs found for {country_name}")
            return None
        
        # Create Processing Session for subgraph encoding
        session = ProcessingSession.objects.create(
            session_name=f"GeoVectors Subgraph Encoding: {country_name}",
            session_type='GEOVECTORS_SUBGRAPH_ENCODING',
            status='IN_PROGRESS',
            configuration={
                'country_name': country_name,
                'continent': continent,
                'subgraph_count': len(subgraphs)
            }
        )
        
        # Find country snapshot date (used for versioning)
        snapshots = self._find_snapshots(continent, country_name)
        if not snapshots:
            logger.warning(f"No snapshots found for {country_name}")
            session.status = 'FAILED'
            session.completed_at = timezone.now()
            session.save()
            return session
        
        snap_date = list(snapshots.items())[0][0]
        
        # Build task args for multiprocessing
        # Use subgraph-specific PBF files instead of country-level snapshot
        tasks_args = []
        for subgraph in subgraphs:
            # Get subgraph-specific PBF path
            subgraph_pbf_path = regional_path_service.get_subgraph_pbf_path(
                continent, country_name, subgraph["slug"], snap_date
            )
            
            # Fallback to country snapshot if subgraph PBF doesn't exist
            if not subgraph_pbf_path.exists():
                logger.warning(f"Subgraph PBF not found at {subgraph_pbf_path}, using country snapshot")
                snap_path = list(snapshots.items())[0][1]
            else:
                snap_path = subgraph_pbf_path
            
            tasks_args.append((
                country_name,
                continent,
                subgraph["slug"],
                subgraph["name"],
                snap_date,
                str(snap_path),
                str(session.id),
                force
            ))
        
        # Run subgraphs sequentially to prevent memory exhaustion
        # Each subgraph worker loads the entire country PBF + FastText + NLE models
        logger.info(f"Starting subgraph encoding for {country_name} sequentially for {len(subgraphs)} subgraphs")
        
        for task_arg in tasks_args:
            try:
                self._process_subgraph_snapshot(*task_arg)
            except Exception as e:
                logger.error(f"Subgraph processing failed: {e}", exc_info=True)
                session.status = 'FAILED'
                session.completed_at = timezone.now()
                session.save()
                raise
        
        # Close connections
        for conn in connections.all():
            conn.close()
        
        # Complete session
        session.status = 'COMPLETED'
        session.completed_at = timezone.now()
        session.save()
        
        return session

    def _find_snapshots(self, continent, country_name):
        """Find only the 2025_12_31 snapshot."""
        snapshots = {}

        # Try raw names, then normalized names
        continent_options = [continent, normalize_continent_slug(continent), continent.lower().replace('_', '-')]
        country_options = [country_name, normalize_country_slug(country_name), country_name.lower().replace('_', '-')]

        # Deduplicate while preserving order
        continent_options = list(dict.fromkeys(continent_options))
        country_options = list(dict.fromkeys(country_options))

        # Also check the single snapshot path (for single_snapshot_mode preprocessing)
        for c_cont in continent_options:
            for c_count in country_options:
                # Check monthly snapshot path
                snap_path = regional_path_service.get_snapshot_pbf_path(c_cont, c_count, 2025, 12, 31)
                if snap_path.exists():
                    snap_date = "2025_12_31"
                    snapshots[snap_date] = snap_path
                    logger.info(f"Found snapshot using continent='{c_cont}' and country='{c_count}'")
                    return snapshots

                # Check single snapshot path (temporal_snapshots/)
                single_snap_path = regional_path_service.get_single_snapshot_pbf_path(c_cont, c_count, '2025_12_31')
                if single_snap_path.exists():
                    snap_date = "2025_12_31"
                    snapshots[snap_date] = single_snap_path
                    logger.info(f"Found single snapshot using continent='{c_cont}' and country='{c_count}'")
                    return snapshots

        logger.warning(f"No snapshots found for {country_name} in {continent}. Tried continents: {continent_options}, countries: {country_options}")
        return snapshots

    @staticmethod
    def _process_single_snapshot_combined(country_name, continent, snap_date, snap_path, session_id, force=False):
        from django.db import connections
        for conn in connections.all():
            conn.close()

        from .vector_storage_service import VectorStorageService
        from ..core.db import DjangoPostgresDB
        from ..core.models.fasttext import FastTextModel
        from ..core.models.nle import NLEModel
        from ..core.util import read_from_snapshot
        import itertools

        for conn in connections.all():
            conn.close()

        logger.info(f"[{snap_date}] (single-pass) Processing snapshot for {country_name}")

        try:
            cont_norm = normalize_country_slug(continent)
            country_norm = normalize_country_slug(country_name)
            pickle_dir = regional_path_service.get_pickle_dir(cont_norm, country_norm)

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

            pickle_file = pickle_dir / "wdw.pickle"
            has_pickle = pickle_file.exists()

            if not has_pickle:
                pickles_dir = regional_path_service.get_country_dir(cont_norm, country_norm) / "pickles"
                if pickles_dir.exists():
                    for subgraph_path in pickles_dir.iterdir():
                        if subgraph_path.is_dir():
                            subgraph_pickle = subgraph_path / "wdw.pickle"
                            if subgraph_pickle.exists():
                                has_pickle = True
                                logger.info(f"[{snap_date}] (single-pass) Found subgraph pickle at {subgraph_pickle}")
                                pickle_file = subgraph_pickle
                                break

            ft_load_start = time.time()
            ft_model = FastTextModel()
            ft_load_duration = time.time() - ft_load_start
            logger.info(f"[{snap_date}] (single-pass) FastText model ready in {ft_load_duration:.2f}s")

            tags_storage = VectorStorageService(model_type="tags", version=version_key)

            if not has_pickle:
                logger.warning(f"[{snap_date}] (single-pass) wdw.pickle not found; running FastText-only.")
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
                return

            logger.info(f"[{snap_date}] (single-pass) NLE location encoding using {pickle_file}...")
            db_bridge = DjangoPostgresDB()
            nle_model = NLEModel(str(pickle_file.parent), njobs=1, db=db_bridge)
            nle_model.load_indexes()

            nle_storage = VectorStorageService(model_type="nle", version=version_key)
            # TODO(two-axis-removal): legacy single-pass two-axis writer — only
            # active when a pickle exists (country-level or first subgraph).
            # Dormant today (0 country-level pickles; see embedding_service gate).
            dual_writer = DualEncodingWriter(
                tag_encoder=ft_model,
                nle_encoder=nle_model,
                tag_storage=tags_storage,
                nle_storage=nle_storage,
                boundary=None,
            )

            snapshot_start = time.time()
            n_data, w_data, r_data = read_from_snapshot(snap_path, writer=dual_writer, max_runs=2)
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
                f"[{snap_date}] (single-pass) read_from_snapshot finished in {snapshot_duration:.2f}s "
                f"(nodes={n_count}, ways={w_count}, relations={r_count})"
            )
            for record in itertools.chain(w_data, r_data):
                dual_writer.add_line(record)
            tags_storage.flush()
            nle_storage.flush()
            nle_model.destroy()
            logger.info(f"[{snap_date}] (single-pass) FastText+NLE complete.")
        except Exception as e:
            logger.error(f"[{snap_date}] (single-pass) Failed: {e}", exc_info=True)
            raise
        finally:
            for conn in connections.all():
                conn.close()

    @staticmethod
    def _process_single_snapshot(country_name, continent, snap_date, snap_path, session_id, force=False):
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

    @staticmethod
    def _process_subgraph_snapshot(country_name, continent, subgraph_slug, subgraph_name, snap_date, snap_path, session_id, force=False):
        """
        Worker function: processes a single subgraph snapshot.
        
        Key differences from country-level processing:
        - Filters entities by subgraph .poly boundary
        - Uses subgraph-specific pickle for NLE encoding
        - Uses unique version identifier (snap_date + subgraph_slug)
        """
        # Close parent connections
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
            from django.conf import settings
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
        if self.boundary and record[3] is not None and record[4] is not None:
            point = Point(record[4], record[3])  # lon, lat
            if not self.boundary.contains(point):
                return  # Skip entity outside boundary
        
        vector = self.encoder.encode_instance(record)
        if vector is not None:
            self.storage.add(record, vector)

# TODO(two-axis-removal): dormant legacy — the Step-1 two-axis writer. Never
# constructed in production (no country-level wdw.pickle exists; verified
# 2026-08-31). "Dual" is a misnomer — this writer never fuses the axes; the
# fused 400D static_embedding (compute_static_embeddings) is the real
# "dual". Candidate for removal with
# embedding_service._build_dual_writer / _run_parallel_dual.
class DualEncodingWriter:

    class _CombinedStorage:
        def __init__(self, tag_storage, nle_storage):
            self._tag_storage = tag_storage
            self._nle_storage = nle_storage

        def flush(self):
            if hasattr(self._tag_storage, "flush"):
                self._tag_storage.flush()
            if hasattr(self._nle_storage, "flush"):
                self._nle_storage.flush()

    def __init__(self, tag_encoder, nle_encoder, tag_storage, nle_storage, boundary=None):
        self.tag_encoder = tag_encoder
        self.nle_encoder = nle_encoder
        self.tag_storage = tag_storage
        self.nle_storage = nle_storage
        self.boundary = boundary
        self.storage = self._CombinedStorage(tag_storage, nle_storage)

    def add_line(self, record):
        if self.boundary and record[3] is not None and record[4] is not None:
            point = Point(record[4], record[3])
            if not self.boundary.contains(point):
                return
        if self.tag_encoder is not None:
            tag_vec = self.tag_encoder.encode_instance(record)
        else:
            tag_vec = None
        if tag_vec is not None:
            self.tag_storage.add(record, tag_vec)
        if self.nle_encoder is not None:
            nle_vec = self.nle_encoder.encode_instance(record)
        else:
            nle_vec = None
        if nle_vec is not None:
            self.nle_storage.add(record, nle_vec)
