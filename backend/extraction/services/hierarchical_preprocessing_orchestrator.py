"""
Hierarchical Preprocessing Orchestrator

Orchestrates continent-level preprocessing using RegionHierarchy tree traversal
with intelligent E-core allocation for parallel extraction.

Strategy:
  1. User selects countries → backend groups by continent
  2. Extract continent PBFs from planet file (if needed)
  3. Parallel extract countries from continent PBFs using E-cores
  4. Register all PbfFile records with proper parent_pbf links
"""
import logging
from pathlib import Path
from typing import Dict, List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

from django.conf import settings

from api.models import PbfFile, RegionHierarchy, ProcessingSession, Task
from extraction.models import OSMWikiDataHierarchy
from .extraction_service import (
    run_pbf_extraction_with_caching,
    get_e_core_list,
)
from .pbf_hierarchy_resolver import pbf_hierarchy_resolver
from .osm_wikidata_resolver import get_country_by_name

logger = logging.getLogger(__name__)


class HierarchicalPreprocessingOrchestrator:
    """
    Orchestrates continent preprocessing using RegionHierarchy tree traversal
    with intelligent E-core allocation for parallel extraction.
    """

    def __init__(self):
        self.e_cores = get_e_core_list()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def process_country_selection(
        self,
        selected_countries: List[str],
        build_name: str,
        session: Optional[ProcessingSession] = None,
    ) -> Dict:
        """
        Main orchestration entry point.

        Args:
            selected_countries: Country names (e.g. ['germany', 'france'])
            build_name: Build identifier (e.g. 'europe_ml_dataset')
            session: Optional ProcessingSession for progress tracking

        Returns:
            Dict keyed by continent with continent_pbf and country_pbfs.
        """
        base_dir = Path(
            getattr(settings, 'PREPROCESSED_DIR', str(Path(settings.BASE_DATA_DIR) / 'preprocessed'))
        )

        continent_groups = self._group_by_continent(selected_countries)
        if not continent_groups:
            raise ValueError("No valid countries found in RegionHierarchy")

        logger.info(
            "Preprocessing %d countries across %d continents for build '%s'",
            len(selected_countries), len(continent_groups), build_name,
        )

        if session:
            session.status = 'RUNNING'
            session.configuration['progress'] = {
                'current_stage': 'Starting continent extraction',
                'completed_tasks': 0,
                'total_tasks': len(continent_groups) + len(selected_countries),
            }
            session.save()

        results: Dict = {}
        completed = 0

        for continent, countries in continent_groups.items():
            # Step 1 — ensure continent PBF exists
            logger.info("=== Continent: %s (%d countries) ===", continent, len(countries))
            continent_pbf = self._ensure_continent_pbf(continent, build_name, base_dir)
            completed += 1
            self._update_progress(session, f'Extracted continent {continent}', completed)

            # Step 2 — parallel country extraction from continent PBF
            country_pbfs = self._parallel_extract_countries(
                continent_pbf=continent_pbf,
                countries=countries,
                build_name=build_name,
                base_dir=base_dir,
                continent_name=continent,
            )
            completed += len(countries)
            self._update_progress(session, f'Completed {continent} countries', completed)

            results[continent] = {
                'continent_pbf': continent_pbf,
                'country_pbfs': country_pbfs,
            }

        if session:
            session.status = 'COMPLETED'
            session.results = self._serialize_results(results)
            session.save()

        logger.info("Hierarchical preprocessing complete for build '%s'", build_name)
        return results

    def generate_config(
        self,
        countries: List[str],
        build_name: str,
        template: Optional[str] = None,
    ) -> dict:
        """Generate a preprocessing configuration dict (stored in ProcessingSession)."""
        continent_groups = self._group_by_continent(countries)
        return {
            'build_name': build_name,
            'template': template,
            'countries': countries,
            'continent_groups': {k: v for k, v in continent_groups.items()},
            'extraction': {
                'parallel_workers': len(self.e_cores),
                'cpu_cores': self.e_cores,
                'strategy': 'complete_ways',
            },
            'output': {
                'base_dir': str(
                    getattr(settings, 'PREPROCESSED_DIR', str(Path(settings.BASE_DATA_DIR) / 'preprocessed'))
                ),
                'structure': '{build_name}/{continent}/{country}.pbf',
            },
        }

    def estimate_duration(self, countries: List[str]) -> dict:
        """Rough duration estimate based on hierarchy grouping."""
        continent_groups = self._group_by_continent(countries)
        continent_time = len(continent_groups) * 0.5   # ~30 min per continent
        country_time = len(countries) * 0.25 / max(len(self.e_cores), 1)
        return {
            'total_hours': round(continent_time + country_time, 2),
            'continent_extraction_hours': round(continent_time, 2),
            'country_extraction_hours': round(country_time, 2),
            'continents': len(continent_groups),
            'countries': len(countries),
        }

    def get_available_countries(self) -> Dict[str, List[dict]]:
        """Return available countries grouped by continent with metadata using OSMWikiDataHierarchy."""
        continents: Dict[str, List[dict]] = {}
        
        # Query countries from OSMWikiDataHierarchy (admin_level=2)
        countries = OSMWikiDataHierarchy.objects.filter(admin_level=2)
        
        for country in countries:
            continent = country.continent_name or 'Unknown'
            continents.setdefault(continent, []).append({
                'name': country.name,
                'has_pbf': bool(country.pbf_url),  # Check if PBF URL exists
                'pbf_size_gb': None,  # Size info not available in OSMWikiDataHierarchy
            })
        return continents

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _group_by_continent(self, countries: List[str]) -> Dict[str, List[str]]:
        """Use OSMWikiDataHierarchy to bucket countries under their continent parent."""
        groups: Dict[str, List[str]] = {}
        for country in countries:
            country_data = get_country_by_name(country)
            if country_data:
                continent = country_data.get('continent_name') or 'Unknown'
                groups.setdefault(continent, []).append(country)
            else:
                logger.warning("No OSMWikiDataHierarchy entry for country: %s", country)
        return groups

    def _ensure_continent_pbf(
        self, continent: str, build_name: str, base_dir: Path,
    ) -> PbfFile:
        """Extract continent PBF from planet file if it doesn't already exist."""
        output_path = str(base_dir / build_name / f"{continent}.pbf")

        # Re-use existing completed PBF
        existing = PbfFile.objects.filter(path=output_path, status=PbfFile.PbfStatus.COMPLETED).first()
        if existing and Path(output_path).exists():
            logger.info("Continent PBF already exists: %s", output_path)
            return existing

        # Find planet PBF
        planet_pbf = PbfFile.objects.filter(
            pbf_file_type=PbfFile.PbfType.PLANET,
            has_history=True,
            status=PbfFile.PbfStatus.COMPLETED,
        ).first()
        if not planet_pbf:
            raise ValueError("No completed planet PBF file found in database")

        # Find continent polygon
        continent_region = RegionHierarchy.objects.filter(
            name__iexact=continent,
            region_type=RegionHierarchy.RegionType.CONTINENT,
        ).first()
        if not continent_region or not continent_region.poly_file_path:
            raise ValueError(f"No polygon file found for continent: {continent}")

        # Extract
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        used_cores: set = set()
        logger.info("Extracting continent %s from planet file …", continent)

        metrics = run_pbf_extraction_with_caching(
            source_pbf_path=planet_pbf.path,
            poly_file_path=continent_region.poly_file_path,
            output_pbf_path=output_path,
            used_cores=used_cores,
            job_id=f"continent_{continent}",
        )

        if metrics.get('status') != 'completed':
            raise RuntimeError(
                f"Continent extraction failed for {continent}: {metrics.get('error')}"
            )

        # Register PbfFile
        continent_pbf, _ = PbfFile.objects.update_or_create(
            path=output_path,
            defaults={
                'pbf_file_type': PbfFile.PbfType.CONTINENT,
                'extraction_level': PbfFile.ExtractionLevel.CONTINENT,
                'parent_pbf': planet_pbf,
                'has_history': True,
                'status': PbfFile.PbfStatus.COMPLETED,
                'size_bytes': Path(output_path).stat().st_size if Path(output_path).exists() else 0,
                'min_timestamp': planet_pbf.min_timestamp,
                'max_timestamp': planet_pbf.max_timestamp,
                'temporal_metadata_source': PbfFile.TemporalMetadataSource.INHERITED,
            },
        )

        # Link to RegionHierarchy
        continent_region.corresponding_pbf = continent_pbf
        continent_region.save()
        logger.info("Continent PBF registered: %s (%s)", continent_pbf.id, output_path)
        return continent_pbf

    def _parallel_extract_countries(
        self,
        continent_pbf: PbfFile,
        countries: List[str],
        build_name: str,
        base_dir: Path,
        continent_name: str,
    ) -> List[PbfFile]:
        """Extract multiple countries in parallel using E-cores."""
        used_cores: set = set()
        results: List[PbfFile] = []

        def _extract_one(country: str) -> Optional[PbfFile]:
            output_path = str(base_dir / build_name / continent_name / f"{country}.pbf")

            # Skip if already done
            existing = PbfFile.objects.filter(
                path=output_path, status=PbfFile.PbfStatus.COMPLETED,
            ).first()
            if existing and Path(output_path).exists():
                logger.info("Country PBF already exists: %s", output_path)
                return existing

            # Resolve poly file
            country_region = RegionHierarchy.objects.filter(name__iexact=country).first()
            if not country_region or not country_region.poly_file_path:
                logger.error("No polygon file for country: %s", country)
                return None

            Path(output_path).parent.mkdir(parents=True, exist_ok=True)

            metrics = run_pbf_extraction_with_caching(
                source_pbf_path=continent_pbf.path,
                poly_file_path=country_region.poly_file_path,
                output_pbf_path=output_path,
                used_cores=used_cores,
                job_id=f"country_{country}",
            )

            if metrics.get('status') != 'completed':
                logger.error("Extraction failed for %s: %s", country, metrics.get('error'))
                return None

            # Register PbfFile
            country_pbf, _ = PbfFile.objects.update_or_create(
                path=output_path,
                defaults={
                    'pbf_file_type': PbfFile.PbfType.REGION,
                    'extraction_level': PbfFile.ExtractionLevel.REGION,
                    'parent_pbf': continent_pbf,
                    'has_history': True,
                    'status': PbfFile.PbfStatus.COMPLETED,
                    'size_bytes': Path(output_path).stat().st_size if Path(output_path).exists() else 0,
                    'min_timestamp': continent_pbf.min_timestamp,
                    'max_timestamp': continent_pbf.max_timestamp,
                    'temporal_metadata_source': PbfFile.TemporalMetadataSource.INHERITED,
                },
            )

            # Link to hierarchy
            country_region.corresponding_pbf = country_pbf
            country_region.save()
            logger.info("Country PBF registered: %s/%s", continent_name, country)
            return country_pbf

        # Fan-out across E-cores
        max_workers = min(len(self.e_cores), len(countries))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_extract_one, c): c for c in countries}
            for future in as_completed(futures):
                country = futures[future]
                try:
                    pbf = future.result()
                    if pbf:
                        results.append(pbf)
                except Exception:
                    logger.exception("Country extraction failed: %s", country)

        return results

    # ------------------------------------------------------------------
    # Progress / serialisation helpers
    # ------------------------------------------------------------------

    def _update_progress(
        self, session: Optional[ProcessingSession], stage: str, completed: int,
    ):
        if not session:
            return
        progress = session.configuration.get('progress', {})
        progress['current_stage'] = stage
        progress['completed_tasks'] = completed
        session.configuration['progress'] = progress
        session.save(update_fields=['configuration'])

    @staticmethod
    def _serialize_results(results: Dict) -> dict:
        """Convert PbfFile objects to JSON-safe dicts for session.results."""
        out = {}
        for continent, data in results.items():
            out[continent] = {
                'continent_pbf_id': str(data['continent_pbf'].id),
                'country_pbf_ids': [str(p.id) for p in data['country_pbfs']],
            }
        return out


# Singleton
hierarchical_preprocessing_orchestrator = HierarchicalPreprocessingOrchestrator()
