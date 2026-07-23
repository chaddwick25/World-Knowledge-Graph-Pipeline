import os
import logging
import calendar
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from django.conf import settings
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

from django.utils import timezone
from .extraction_service import ExtractionService, run_pbf_extraction_with_relation
from .regional_path_service import regional_path_service, normalize_country_slug, normalize_continent_slug
from .pbf_bounding_box_service import pbf_bounding_box_service
from .osm_wikidata_resolver import get_country_relations_dict
from .country_override_service import get_country_slug

logger = logging.getLogger(__name__)

class TemporalOrchestratorService:
    """
    Orchestrates the 3-phase temporal extraction pipeline (Sequential Version):
    1. Phase 1: Regional Relation Extraction (History)
    2. Phase 2: Monthly Snapshot Generation
    3. Phase 3: Automated Boundary (Poly) Generation
    4. Phase 4: GeoVectors Spatial Index Generation (Pickle)
    """

    def __init__(self, session_id: str = None):
        self.extraction_service = ExtractionService()
        self.session_id = session_id
        self.channel_layer = get_channel_layer() if session_id else None
        self.group_name = f'pipeline_{session_id}' if session_id else None
        self._step_num = 0

    def _push(self, msg_type, **kwargs):
        """Push a message to the WebSocket channel if available."""
        if not self.channel_layer or not self.group_name:
            return
        try:
            async_to_sync(self.channel_layer.group_send)(
                self.group_name,
                {'type': msg_type, **kwargs}
            )
        except Exception as exc:
            logger.warning('Channel push failed: %s', exc)

    def _step_start(self, name, message):
        """Emit a step_start event via WebSocket."""
        self._step_num += 1
        self._push('step_update',
                   step=self._step_num, total=4,
                   name=name, status='in_progress', message=message, pct=0)
        logger.info('[Temporal %d/4] %s — %s', self._step_num, name, message)

    def _step_done(self, name, message, pct=100):
        """Emit a step_done event via WebSocket."""
        self._push('step_update',
                   step=self._step_num, total=4,
                   name=name, status='completed', message=message, pct=pct)
        logger.info('[Temporal %d/4] %s — DONE', self._step_num, name)

    def _step_fail(self, name, message):
        """Emit a step_fail event via WebSocket."""
        self._push('step_update',
                   step=self._step_num, total=4,
                   name=name, status='failed', message=message, pct=0)
        logger.error('[Temporal %d/4] %s — FAILED: %s', self._step_num, name, message)

    def _get_country_slug(self, country: str, iso: str = None) -> str:
        """Get the country slug for path construction, using JSON-based overrides.

        Delegates to get_country_slug(iso, default_slug) so that overrides.json is
        the authoritative source for special-case slugs.
        """
        default_slug = normalize_country_slug(country)
        if iso:
            return get_country_slug(iso, default_slug)
        return default_slug

    def run_pipeline(self, continent: str, country: str, source_pbf_path: str,
                     start_year: int = 2021, end_year: int = 2025,
                     phases: List[int] = [1, 2, 3, 4], single_snapshot_mode: bool = False,
                     snapshot_date: str = None) -> Dict:
        """Entry point for the temporal pipeline orchestration.

        Args:
            single_snapshot_mode: If True, use SINGLE_SNAPSHOT_YEAR for all phases instead of start_year/end_year range.
            snapshot_date: Override snapshot date in YYYY_MM_DD format. When provided,
                          Phase 1 uses the continent snapshot as source (instead of planet).
        """
        # Initialize results structure first
        results = {'phases': {}}

        # Determine if we are in 'Batch/Continent' mode or 'Specific Country' mode
        country_display = country if country else f"Continent: {continent}"

        # Normalize continent slug for all filesystem operations
        continent = normalize_continent_slug(continent) if continent else continent

        from api.models import ProcessingSession
        from django.conf import settings

        session_name = f"Temporal Pipeline: {country_display}"

        # In single-snapshot mode, override year range with single snapshot year
        if single_snapshot_mode:
            single_year = int(snapshot_date.split('_')[0]) if snapshot_date else getattr(settings, 'SINGLE_SNAPSHOT_YEAR', 2025)
            start_year = single_year
            end_year = single_year
            logger.info(f"[SINGLE-SNAPSHOT MODE] Using year {single_year} for all phases")
        else:
            start_year = start_year or 2021
            end_year = end_year or 2025

        session = ProcessingSession.objects.create(
            session_name=session_name,
            session_type=ProcessingSession.SessionType.TEMPORAL_CORPUS,
            configuration={
                'continent': continent,
                'country': country,
                'start_year': start_year,
                'end_year': end_year,
                'phases': phases,
                'single_snapshot_mode': single_snapshot_mode,
                'snapshot_date': snapshot_date,
            }
        )

        results = {'session_id': str(session.id), 'phases': {}}

        try:
            # Load country relations mapping from OSMWikiDataHierarchy
            country_relations = get_country_relations_dict()

            if not country_relations:
                raise RuntimeError("No country relations found in OSMWikiDataHierarchy. Run 'import_country_relations' first.")

            # Phase 1: Extract Yearly History PBFs
            if 1 in phases:
                self._step_start('extract_region_pbf', f'Extracting region PBF for {country_display}')
                res1 = self._run_phase_1_relation(continent, country, source_pbf_path, start_year, end_year, country_relations, session, single_snapshot_mode)
                results['phases']['1'] = res1
                if not res1.get('success'):
                    self._step_fail('extract_region_pbf', f'Failed: {res1.get("error")}')
                    raise RuntimeError(f"Phase 1 failed: {res1.get('error')}")
                self._step_done('extract_region_pbf', f'Extracted {res1.get("generated", 0)} yearly PBFs')

            # Phase 2: Generate monthly snapshots
            if 2 in phases:
                self._step_start('monthly_snapshots', f'Generating monthly snapshots for {country_display}')
                res2 = self._run_phase_2_snapshots(continent, country, start_year, end_year, session, single_snapshot_mode)
                results['phases']['2'] = res2
                if not res2.get('success'):
                    self._step_fail('monthly_snapshots', f'Failed: {res2.get("error")}')
                    raise RuntimeError(f"Phase 2 failed: {res2.get('error')}")
                self._step_done('monthly_snapshots', f'Generated {res2.get("generated", 0)} monthly snapshots')

            # Phase 3: Generate polyfiles
            if 3 in phases:
                self._step_start('subgraph_generation', f'Generating subgraph polyfiles for {country_display}')
                res3 = self._run_phase_3_polygen(continent, country, start_year, end_year, session, country_relations, single_snapshot_mode)
                results['phases']['3'] = res3
                if not res3.get('success'):
                    self._step_fail('subgraph_generation', f'Failed: {res3.get("error")}')
                    raise RuntimeError(f"Phase 3 failed: {res3.get('error')}")
                self._step_done('subgraph_generation', f'Generated {res3.get("generated", 0)} polyfiles')

            # Phase 4: GeoVectors Pre-processing (Pickle Generation)
            if 4 in phases:
                self._step_start('geovectors_preprocess', f'Generating GeoVectors spatial index for {country_display}')
                logger.info(f"Phase 4 START: GeoVectors for {country}")
                res4 = self._run_phase_4_geovectors(continent, country, session)
                results['phases']['4'] = res4
                if res4.get('success') and not res4.get('skipped'):
                    logger.info(f"Phase 4 SUCCESS: Generated pickle for {country}")
                    self._step_done('geovectors_preprocess', 'GeoVectors spatial index generated')
                elif res4.get('skipped'):
                    logger.info(f"Phase 4 SKIPPED: {country} has no local embeddings.")
                    self._step_done('geovectors_preprocess', 'Skipped (no local embeddings)')
                else:
                    logger.warning(f"Phase 4 (GeoVectors) failed: {res4.get('error')}")
                    self._step_fail('geovectors_preprocess', f'Failed: {res4.get("error")}')

            session.status = ProcessingSession.SessionStatus.COMPLETED
            session.completed_at = timezone.now()
            session.save()
            
        except Exception as e:
            logger.error(f"Pipeline failed for {country}: {e}", exc_info=True)
            session.status = ProcessingSession.SessionStatus.FAILED
            session.results = {'error': str(e)}
            session.save()
            results['success'] = False
            results['error'] = str(e)
            return results

        results['success'] = True
        return results

    def _run_phase_1_relation(self, continent: str, country: str, source_pbf_path: str,
                              start_year: int, end_year: int, country_relations: Dict, session, single_snapshot_mode: bool = False) -> Dict:
        """Step 1: Extract country history yearly extracts in parallel.

        In single_snapshot_mode, only extracts the single year (SINGLE_SNAPSHOT_YEAR).
        """
        # 1. Resolve Country Mapping or Continent Poly
        mapping = self._get_country_mapping(country, country_relations)
        iso = mapping.get('iso') if mapping else None
        country_slug = self._get_country_slug(country, iso)

        rel_id = None
        poly_path = None
            
        if not mapping:
            if not country:
                # Continental Mode: Use the continent's polyfile
                poly_path = Path(settings.POLYGON_FILES_DIR) / f"{continent.lower()}.poly"
                if not poly_path.exists():
                    # Try recursive search if not in root
                    import glob
                    matches = glob.glob(f"{settings.POLYGON_FILES_DIR}/**/{continent.lower()}.poly", recursive=True)
                    if matches:
                        poly_path = Path(matches[0])
                
                if not poly_path or not poly_path.exists():
                    raise RuntimeError(f"Phase 1 (Continent): Polyfile not found for {continent}")
                
                logger.info(f"Phase 1: Continental Mode - Using polyfile {poly_path}")
            else:
                raise RuntimeError(f"Phase 1: Country '{country}' not found in mapping. Please check OSMWikiDataHierarchy")
        else:
            rel_id = mapping.get('relation_id')
            if not rel_id:
                raise RuntimeError(f"Phase 1: Relation ID not found for {country}")

        # 2. HOLISTIC SOURCE RESOLUTION (Continental Shard Preferred)
        continent_clean = continent.replace('_', '-')
        alt_continent_clean = continent.replace('-', '_')

        # Use configured OSM_WIKIDATA_EXTRACTIONS_DIR as canonical root
        osm_wikidata_dir = Path(settings.OSM_WIKIDATA_EXTRACTIONS_DIR)

        # Determine the snapshot date (from single_snapshot_mode or explicit param)
        config = getattr(session, 'configuration', {}) or {}
        snap_date = config.get('snapshot_date') or getattr(settings, 'SINGLE_SNAPSHOT_DATE', '2025_12_31')

        # Preferred: continent snapshot at continents/{snapshot_date}/{continent}.pbf
        possible_sources = [
            # Phase 1: Date-specific continent snapshot (preferred)
            regional_path_service.get_continent_snapshot_pbf_path(continent, snap_date),
            # New structure: continents/{continent}.pbf (legacy continent extraction)
            osm_wikidata_dir / 'continents' / f"{continent}.pbf",
            osm_wikidata_dir / 'continents' / f"{continent_clean}.pbf",
            osm_wikidata_dir / 'continents' / f"{alt_continent_clean}.pbf",
            # Old structure: {continent}/{continent}.pbf (for subgraphs)
            osm_wikidata_dir / continent / f"{continent}.pbf",
            osm_wikidata_dir / continent_clean / f"{continent_clean}.pbf",
            osm_wikidata_dir / alt_continent_clean / f"{alt_continent_clean}.pbf",
            Path(source_pbf_path), # Fallback to Planet
        ]
        
        effective_source = None
        for ps in possible_sources:
            if ps.exists():
                effective_source = str(ps)
                break
        
        if not effective_source:
            raise RuntimeError(f"Phase 1: No source PBF found. Tried continental shards and fallback.")
            
        logger.info(f"Phase 1: Extracting {country} from {effective_source} (Relation: {rel_id})")

        if single_snapshot_mode:
            logger.info(f"Phase 1: Single-snapshot mode - extracting only year {start_year}")

        # 3. Prepare work items
        work_items = []
        if not country:
            # Continental Mode: Single extraction
            output_path = regional_path_service.get_continent_pbf_path(continent)
            if not output_path.exists() or session.configuration.get('force'):
                work_items.append((None, output_path))
            else:
                logger.info(f"Phase 1: Continent PBF already exists at {output_path}")
        else:
            # Country Mode: Yearly extracts (single year in single_snapshot_mode)
            for year in range(start_year, end_year + 1):
                output_path = regional_path_service.get_yearly_pbf_path(continent, country_slug, year)
                if output_path.exists():
                    logger.info(f"Phase 1: Skipping year {year} - File already exists.")
                    continue
                work_items.append((year, output_path))

        if not work_items:
            return {'success': True, 'generated': 0, 'failed': 0}

        # 4. Parallel Extraction
        max_workers = min(8, len(work_items))
        generated = 0
        failed = 0
        shared_used_cores = set()

        def _extract_task(year, out_path):
            if rel_id:
                logger.info(f"Phase 1: Extracting {country} {year} via relation {rel_id}...")
                res = run_pbf_extraction_with_relation(
                    effective_source, rel_id, str(out_path), shared_used_cores, f"rel_{rel_id}_{year}"
                )
            else:
                logger.info(f"Phase 1: Extracting {continent} {year} via polygon...")
                res = self.extraction_service.extract_with_polygon(
                    source_pbf_path=effective_source,
                    polygon_file_path=str(poly_path),
                    output_path=str(out_path)
                )
            return res.get('success', False)

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_extract_task, y, o): y for y, o in work_items}
            for future in as_completed(futures):
                y = futures[future]
                try:
                    if future.result():
                        generated += 1
                    else:
                        failed += 1
                except Exception as e:
                    failed += 1
                    logger.error(f"Phase 1: Year {y} failed: {e}")

        return {'success': failed == 0, 'generated': generated, 'failed': failed}

    def _run_phase_2_snapshots(self, continent: str, country: str, start_year: int, end_year: int, session, single_snapshot_mode: bool = False) -> Dict:
        """Step 2: Generate monthly snapshots in parallel.

        In single_snapshot_mode, generates only one snapshot at SINGLE_SNAPSHOT_DATE.
        """
        from django.conf import settings
        from api.models import PbfFile

        # Get ISO and country slug for path construction
        mapping = self._get_country_mapping(country, {})
        iso = mapping.get('iso') if mapping else None
        country_slug = self._get_country_slug(country, iso)

        if single_snapshot_mode:
            logger.info(f"Phase 2: Single-snapshot mode - generating one snapshot for {country}")
            config = getattr(session, 'configuration', {}) or {}
            snapshot_date = config.get('snapshot_date') or getattr(settings, 'SINGLE_SNAPSHOT_DATE', '2025_12_31')
            year, month, day = map(int, snapshot_date.split('_'))
            snapshot_timestamp = f"{year}-{month:02d}-{day:02d}T23:59:59Z"

            yearly_history_path = regional_path_service.get_yearly_pbf_path(continent, country_slug, year)
            if not yearly_history_path.exists():
                logger.warning(f"Phase 2: Yearly history file not found at {yearly_history_path}")
                return {'success': False, 'error': 'Yearly history file not found'}

            output_path = regional_path_service.get_single_snapshot_pbf_path(continent, country_slug, snapshot_date)

            if output_path.exists():
                logger.info(f"Phase 2: Single snapshot already exists at {output_path}")
                return {'success': True, 'generated': 0, 'failed': 0, 'skipped': True}

            logger.info(f"Phase 2: Creating single snapshot at {snapshot_timestamp}...")
            res = self.extraction_service.create_snapshot(
                str(yearly_history_path), str(output_path), snapshot_timestamp, PbfFile.ExtractionLevel.SNAPSHOT
            )
            if res.get('success'):
                return {'success': True, 'generated': 1, 'failed': 0}
            else:
                return {'success': False, 'generated': 0, 'failed': 1, 'error': res.get('error')}
        else:
            # Legacy mode: Generate monthly snapshots
            logger.info(f"Phase 2: Generating monthly snapshots for {country} (parallel)")
            work_items = []
            for year in range(start_year, end_year + 1):
                yearly_history_path = regional_path_service.get_yearly_pbf_path(continent, country_slug, year)
                if not yearly_history_path.exists():
                    logger.warning(f"Phase 2: Skipping year {year} - Yearly history file not found at {yearly_history_path}")
                    continue

                for month in range(1, 13):
                    last_day = calendar.monthrange(year, month)[1]
                    output_path = regional_path_service.get_snapshot_pbf_path(continent, country_slug, year, month, last_day)

                    if not output_path.exists():
                        timestamp = f"{year}-{month:02d}-{last_day:02d}T23:59:59Z"
                        work_items.append((str(yearly_history_path), str(output_path), timestamp))

            if not work_items:
                logger.info(f"Phase 2: All monthly snapshots already exist for {country}")
                return {'success': True, 'generated': 0, 'failed': 0}

            def _create_snapshot(source, output, timestamp):
                logger.info(f"Creating snapshot for {timestamp}...")
                res = self.extraction_service.create_snapshot(
                    source, output, timestamp, PbfFile.ExtractionLevel.SNAPSHOT
                )
                return res.get('success', True) # create_snapshot returns facade result

            # Snapshots are lighter (time-filter); use more workers
            max_workers = min(8, len(work_items))
            generated = 0
            failed = 0

            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = {pool.submit(_create_snapshot, s, o, t): t for s, o, t in work_items}
                for future in as_completed(futures):
                    ts = futures[future]
                    try:
                        if future.result():
                            generated += 1
                        else:
                            failed += 1
                    except Exception as e:
                        failed += 1
                        logger.error(f"Phase 2: Snapshot {ts} failed: {e}")

            return {'success': failed == 0, 'generated': generated, 'failed': failed}

    def _run_phase_3_polygen(self, continent: str, country: str, start_year: int, end_year: int, session, country_relations: Dict, single_snapshot_mode: bool = False) -> Dict:
        """
        Step 3: Generate .poly boundary files in parallel using ThreadPoolExecutor.
        Each snapshot PBF that lacks a matching .poly is submitted as an independent
        task — following the same pattern as HierarchicalPreprocessingOrchestrator.

        In single_snapshot_mode, generates only one poly for the single snapshot PBF.
        """
        from django.conf import settings

        # Get ISO and country slug for path construction
        mapping = self._get_country_mapping(country, country_relations)
        iso = mapping.get('iso') if mapping else None
        country_slug = self._get_country_slug(country, iso)

        if single_snapshot_mode:
            logger.info(f"Phase 3: Single-snapshot mode - generating one poly for {country}")
            config = getattr(session, 'configuration', {}) or {}
            snapshot_date = config.get('snapshot_date') or getattr(settings, 'SINGLE_SNAPSHOT_DATE', '2025_12_31')
            snapshot_path = regional_path_service.get_single_snapshot_pbf_path(continent, country_slug, snapshot_date)
            poly_path = snapshot_path.with_suffix('.poly')

            if not snapshot_path.exists():
                logger.warning(f"Phase 3: Snapshot not found at {snapshot_path}")
                return {'success': False, 'error': 'Snapshot not found'}

            if poly_path.exists():
                logger.info(f"Phase 3: Poly already exists at {poly_path}")
                return {'success': True, 'generated': 0, 'skipped': 1}

            # Resolve relation ID for this country
            mapping = self._get_country_mapping(country, country_relations)
            relation_id = mapping.get('relation_id') if mapping else None

            logger.info(f"Phase 3 START: continent={continent}, country={country_slug}, mapping={mapping is not None}, relation_id={relation_id}")

            if not relation_id:
                logger.error(f"Phase 3: Relation ID not found for {country}. Mapping: {mapping}")
                return {'success': False, 'error': 'Relation ID missing'}

            logger.info(f"Phase 3: Generating poly for {snapshot_path.name}...")
            success = pbf_bounding_box_service.generate_high_res_poly(
                str(snapshot_path), relation_id, str(poly_path)
            )

            if success:
                logger.info(f"Phase 3: SUCCESS - Generated {poly_path.name}")
                return {'success': True, 'generated': 1, 'failed': 0}
            else:
                logger.error(f"Phase 3: FAILED - Could not generate {poly_path.name}")
                return {'success': False, 'generated': 0, 'failed': 1}
        else:
            # Legacy mode: Generate polys for all monthly snapshots
            logger.info(f"Phase 3: Generating boundary .poly files for {country} (parallel)")

            # Build the work queue: list of (snapshot_path, poly_path) pairs that need processing
            work_items = []
            import calendar
            for year in range(start_year, end_year + 1):
                for month in range(1, 13):
                    last_day = calendar.monthrange(year, month)[1]
                    snapshot_path = regional_path_service.get_snapshot_pbf_path(
                        continent, country_slug, year, month, last_day
                    )
                    logger.info(f"Phase 3: Checking for snapshot at {snapshot_path}")
                    if snapshot_path.exists():
                        poly_path = snapshot_path.with_suffix('.poly')
                        logger.info(f"Phase 3: Snapshot exists. Checking for poly at {poly_path}")
                        if not poly_path.exists():
                            logger.info(f"Phase 3: Adding {snapshot_path.name} to work queue")
                            work_items.append((snapshot_path, poly_path))

            if not work_items:
                logger.info(f"Phase 3: All .poly files already exist for {country}")
                return {'success': True, 'generated': 0, 'skipped': 0}

            logger.info(f"Phase 3: {len(work_items)} .poly files to generate for {country}")

            generated = 0
            failed = 0

            # Resolve relation ID for this country
            mapping = self._get_country_mapping(country, country_relations)
            relation_id = mapping.get('relation_id') if mapping else None

            logger.info(f"Phase 3 START: continent={continent}, country={country_slug}, mapping={mapping is not None}, relation_id={relation_id}")

            if not relation_id:
                logger.error(f"Phase 3: Relation ID not found for {country}. Mapping: {mapping}")
                return {'success': False, 'error': 'Relation ID missing'}

            def _generate_one(snapshot_path, poly_path):
                """Worker: generate a single high-res .poly from a snapshot PBF."""
                success = pbf_bounding_box_service.generate_high_res_poly(
                    str(snapshot_path), relation_id, str(poly_path)
                )
                if success:
                    logger.info(f"Phase 3: SUCCESS - Generated {poly_path.name}")
                else:
                    logger.error(f"Phase 3: FAILED - Could not generate {poly_path.name}")
                return success

            # Fan-out — I/O-bound work; use up to 8 threads (lightweight CLI calls)
            max_workers = min(8, len(work_items))
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = {
                    pool.submit(_generate_one, snap, poly): (snap, poly)
                    for snap, poly in work_items
                }
                for future in as_completed(futures):
                    snap, poly = futures[future]
                    try:
                        if future.result():
                            generated += 1
                        else:
                            failed += 1
                            logger.warning(f"Phase 3: poly generation returned None for {snap.name}")
                    except Exception as exc:
                        failed += 1
                        logger.error(f"Phase 3: poly generation error for {snap.name}: {exc}")

            logger.info(
                f"Phase 3 complete for {country}: {generated} generated, {failed} failed"
            )
            return {'success': failed == 0, 'generated': generated, 'failed': failed}

    def _run_phase_4_geovectors(self, continent: str, country: str, session) -> Dict:
        """
        Step 4: Procedurally generate GeoVectors spatial index (wdw.pickle).
        Only runs if local embeddings are available.
        """
        logger.info(f"Phase 4: Checking GeoVectors availability for {country}")
        
        try:
            from geovectors_encoder.services.geovectors_service import GeoVectorsEncoderService
            gv_service = GeoVectorsEncoderService()
            
            result = gv_service.generate_pickle(country, continent)
            return result
        except Exception as e:
            logger.error(f"Phase 4: GeoVectors pre-processing failed: {e}")
            return {'success': False, 'error': str(e)}


    def _get_country_mapping(self, country: str, country_relations: Dict) -> Optional[Dict]:
        """Robust lookup for country mapping by key, slug, or name."""
        if not country:
            return None
            
        # 1. Try direct key lookup (ISO code or exact slug)
        mapping = country_relations.get(country)
        if mapping:
            return mapping
            
        # 2. Normalize search term
        search_norm = normalize_country_slug(country)
        
        # 3. Iterate through mappings
        for key, data in country_relations.items():
            # Check key
            if normalize_country_slug(key) == search_norm:
                return data
            
            # Check slug
            slug = normalize_country_slug(data.get('slug', ''))
            if slug == search_norm:
                return data
                
            # Check name
            name = normalize_country_slug(data.get('name', ''))
            if name == search_norm:
                return data
                
        return None


temporal_orchestrator = TemporalOrchestratorService()
