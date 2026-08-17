import json
import logging
import subprocess
import tempfile
import shutil
from pathlib import Path
from typing import Dict, List, Optional

from django.conf import settings

from core.services.snapshot.regional_path_service import regional_path_service, normalize_country_slug
from core.services.snapshot.pbf_bounding_box_service import pbf_bounding_box_service
from core.services.planet_init.osm_wikidata_resolver import (
    parse_poly_bbox,
    get_country_relations_dict,
)
from core.services.planet_init.osm_relation_hierarchy_service import (
    osm_relation_hierarchy_service,
)

logger = logging.getLogger(__name__)


class SubgraphPbfService:
    """
    Service for generating per-subgraph PBF files and poly files
    from a country's single snapshot PBF using Wikidata hierarchy data.
    """

    def __init__(self):
        pass

    def generate_subgraph_pbfs(
        self,
        continent: str,
        country: str,
        iso: str,
        snapshot_date: Optional[str] = None,
        max_subgraphs: Optional[int] = None,
        overwrite: bool = False,
        relation_id: Optional[int] = None,
        wikidata_uri: Optional[str] = None,
        country_name: Optional[str] = None,
    ) -> Dict:
        """
        Generate PBF files for subgraphs (admin divisions) of a country.

        Args:
            continent: Continent name (e.g., 'europe')
            country: Country name (e.g., 'ireland')
            iso: ISO code (e.g., 'IE')
            snapshot_date: Snapshot date string (e.g., '2025_12_31'), defaults to SINGLE_SNAPSHOT_DATE
            max_subgraphs: Maximum number of subgraphs to process (None = all)
            overwrite: If True, regenerate existing PBFs and polys

        Returns:
            {
                'success': bool,
                'total': int,
                'generated': int,
                'failed': int,
                'skipped': int,
                'subgraphs': List[Dict]  # per-subgraph results
            }
        """
        if snapshot_date is None:
            snapshot_date = getattr(settings, 'SINGLE_SNAPSHOT_DATE', '2025_12_31')

        iso = (iso or '').upper()

        # Resolve country metadata if relation_id / wikidata_uri / country_name were
        # not explicitly provided.
        if not relation_id or not wikidata_uri or not country_name:
            relations = get_country_relations_dict()
            entry = (
                relations.get(iso)
                or relations.get(iso.upper())
                or relations.get(iso.lower())
            )

            # Fallback: check non-sovereign synthetic registry for ISOs like WL, XS, EN
            if not entry:
                try:
                    from core.services.planet_init.non_sovereign_territories import (
                        is_non_sovereign_synthetic_iso,
                        synthetic_iso_to_info,
                    )
                    if is_non_sovereign_synthetic_iso(iso):
                        syn_info = synthetic_iso_to_info(iso)
                        if syn_info:
                            qid = syn_info.get('wikidata_qid', '')
                            entry = {
                                'name': syn_info.get('name', country),
                                'relation_id': syn_info.get('osm_relation_id'),
                                'slug': syn_info.get('slug', iso.lower()),
                                'continent_name': syn_info.get('continent', continent),
                                'iso_code': iso,
                                'wkg_uri': f'http://www.wikidata.org/entity/{qid}' if qid else None,
                            }
                            logger.info(
                                "[SUBGRAPH-PBF] Resolved synthetic ISO %s from registry: %s",
                                iso, syn_info.get('name'),
                            )
                except Exception:
                    pass

            if not entry:
                logger.error(
                    "[SUBGRAPH-PBF] ISO %s not found in OSMWikiDataHierarchy/country resolver",
                    iso,
                )
                return {
                    'success': False,
                    'error': f'Country metadata missing for ISO {iso}',
                    'total': 0,
                    'generated': 0,
                    'failed': 0,
                    'skipped': 0,
                    'subgraphs': [],
                }

            relation_id = relation_id or entry.get('relation_id')
            wikidata_uri = wikidata_uri or entry.get('wkg_uri')
            country_name = country_name or entry.get('name') or country

        if not relation_id or not wikidata_uri or not country_name:
            logger.error(
                "[SUBGRAPH-PBF] Missing relation_id/wikidata_uri/country_name for ISO %s",
                iso,
            )
            return {
                'success': False,
                'error': f'Missing relation_id/wikidata_uri/country_name for ISO {iso}',
                'total': 0,
                'generated': 0,
                'failed': 0,
                'skipped': 0,
                'subgraphs': [],
            }

        # Build hierarchy in-memory via Wikidata P150/P402 (no JSON cache)
        hierarchy = osm_relation_hierarchy_service.build_hierarchy_for_country(
            iso=iso,
            root_relation_id=relation_id,
            wikidata_uri=wikidata_uri,
            country_name=country_name,
            continent=continent,
            country_slug=country,
        )

        admin_tree = hierarchy.get('admin_tree', [])
        root = admin_tree[0] if admin_tree else {}
        children = root.get('children', [])

        if not children:
            logger.warning(f"No subgraph children found in hierarchy for {iso}")
            return {
                'success': True,
                'total': 0,
                'generated': 0,
                'failed': 0,
                'skipped': 0,
                'subgraphs': [],
            }

        # Limit to max_subgraphs if specified
        if max_subgraphs is not None and max_subgraphs > 0:
            children = children[:max_subgraphs]

        # Get country snapshot PBF as source
        source_pbf = regional_path_service.get_single_snapshot_pbf_path(
            continent, country, snapshot_date
        )
        if not source_pbf.exists():
            logger.error(f"Source snapshot PBF not found at {source_pbf}")
            return {
                'success': False,
                'error': f'Source snapshot PBF not found at {source_pbf}',
                'total': len(children),
                'generated': 0,
                'failed': 0,
                'skipped': 0,
                'subgraphs': [],
            }

        logger.info(
            f"Generating {len(children)} subgraph PBFs from {source_pbf}..."
        )

        results = []
        generated = 0
        failed = 0
        skipped = 0

        for child in children:
            subgraph_name = child.get('name')
            relation_id = child.get('osm_relation_id')
            wikidata_qid = child.get('wikidata_qid')
            subgraph_slug = normalize_country_slug(subgraph_name) if subgraph_name else ''

            if not subgraph_name or not relation_id:
                logger.warning(f"Skipping subgraph with missing name or relation_id: {child}")
                skipped += 1
                results.append({
                    'name': subgraph_name,
                    'slug': subgraph_slug,
                    'success': False,
                    'skipped': True,
                    'error': 'Missing name or relation_id',
                    'wikidata_qid': wikidata_qid,
                })
                continue

            output_pbf = regional_path_service.get_subgraph_pbf_path(
                continent, country, subgraph_slug, snapshot_date
            )
            output_poly = regional_path_service.get_subgraph_poly_path(
                continent, country, subgraph_slug, snapshot_date
            )

            # Skip if already exists (unless overwrite is True)
            if not overwrite and output_pbf.exists() and output_poly.exists():
                logger.info(f"Skipping {subgraph_name} - files already exist")
                # Still compute bbox for skipped subgraphs so SubgraphProfile
                # can be populated with spatial metadata.
                bbox = None
                try:
                    bbox = parse_poly_bbox(str(output_poly))
                except Exception:
                    bbox = None
                skipped += 1
                results.append({
                    'name': subgraph_name,
                    'slug': subgraph_slug,
                    'success': True,
                    'skipped': True,
                    'pbf_path': str(output_pbf),
                    'poly_path': str(output_poly),
                    'relation_id': relation_id,
                    'wikidata_qid': wikidata_qid,
                    'bbox': bbox,
                })
                continue

            # Generate subgraph PBF
            pbf_success = self._extract_relation_pbf(
                str(source_pbf), relation_id, str(output_pbf)
            )

            if not pbf_success:
                failed += 1
                results.append({
                    'name': subgraph_name,
                    'slug': subgraph_slug,
                    'success': False,
                    'error': 'PBF extraction failed',
                    'relation_id': relation_id,
                    'wikidata_qid': wikidata_qid,
                })
                continue

            # Generate subgraph poly
            poly_success = pbf_bounding_box_service.generate_high_res_poly(
                str(output_pbf), relation_id, str(output_poly)
            )

            if poly_success:
                bbox = None
                try:
                    bbox = parse_poly_bbox(str(output_poly))
                except Exception:
                    bbox = None
                generated += 1
                results.append({
                    'name': subgraph_name,
                    'slug': subgraph_slug,
                    'success': True,
                    'pbf_path': str(output_pbf),
                    'poly_path': str(output_poly),
                    'relation_id': relation_id,
                    'wikidata_qid': wikidata_qid,
                    'bbox': bbox,
                })
                logger.info(f"Generated subgraph {subgraph_name}: PBF + poly")
            else:
                failed += 1
                results.append({
                    'name': subgraph_name,
                    'slug': subgraph_slug,
                    'success': False,
                    'error': 'Poly generation failed',
                    'relation_id': relation_id,
                    'wikidata_qid': wikidata_qid,
                    'pbf_path': str(output_pbf),
                })

        return {
            'success': failed == 0,
            'total': len(children),
            'generated': generated,
            'failed': failed,
            'skipped': skipped,
            'subgraphs': results,
        }

    def _extract_relation_pbf(
        self, source_pbf: str, relation_id: int, output_pbf: str
    ) -> bool:
        """
        Extract a single relation (and dependencies) from a PBF file using osmium getid.

        Args:
            source_pbf: Path to source PBF file
            relation_id: OSM relation ID to extract
            output_pbf: Path to output PBF file

        Returns:
            bool: True if successful, False otherwise
        """
        import tempfile

        try:
            # Extract relation into a temp PBF
            with tempfile.NamedTemporaryFile(suffix='.pbf', delete=False) as tmp:
                tmp_pbf = tmp.name

            getid_cmd = [
                settings.OSMIUM_BINARY_PATH, 'getid',
                '--with-history',
                '--add-referenced',
                source_pbf, f"r{relation_id}",
                '-o', tmp_pbf,
                '--overwrite'
            ]

            logger.debug(f"Extracting r{relation_id} from {source_pbf}...")
            result = subprocess.run(
                getid_cmd, capture_output=True, text=True
            )

            if result.returncode != 0:
                logger.error(f"Osmium getid FAILED for r{relation_id}: {result.stderr}")
                return False

            # Move temp file to final location (handle cross-device move)
            try:
                Path(tmp_pbf).rename(output_pbf)
            except OSError as e:
                if "Invalid cross-device link" in str(e):
                    # Cross-device move: copy then delete
                    shutil.copy2(tmp_pbf, output_pbf)
                    Path(tmp_pbf).unlink()
                else:
                    raise e

            logger.info(f"Extracted r{relation_id} to {output_pbf}")
            return True

        except Exception as e:
            logger.error(f"Failed to extract relation {relation_id}: {e}")
            return False
        finally:
            if 'tmp_pbf' in locals() and Path(tmp_pbf).exists():
                Path(tmp_pbf).unlink()


subgraph_pbf_service = SubgraphPbfService()
