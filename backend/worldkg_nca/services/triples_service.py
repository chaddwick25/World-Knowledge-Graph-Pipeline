import logging
import subprocess
import os
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

WKGS_BASE_URI = "http://schema.worldkg.org/"
OSMN_BASE_URI = "https://www.openstreetmap.org/node/"

WORLDKG_REPO_URL = "https://github.com/alishiba14/WorldKG-Knowledge-Graph"


class WorldKGTriplesService:
    """
    Integrates the official WorldKG CreateTriples.py pipeline.

    The original WorldKG pipeline (Dsouza et al. CIKM 2021) processes a PBF file and
    produces RDF Turtle (.ttl) triples via CreateTriples.py. This service wraps that
    pipeline and parses the output to enrich OsmEntity records in the database.
    
    [CODE AUDIT TRACEABILITY]
    Maps to logic from: https://github.com/alishiba14/WorldKG-Knowledge-Graph
    Implementation: This acts as the direct wrapper to run their original academic pipeline.
    It shells out the subprocess explicitly to their codebase, catching the Virtuoso TTL 
    RDF files and converting them dynamically into PostgreSQL vector parameters.

    Pipeline:
        1. Clone / locate alishiba14/WorldKG-Knowledge-Graph repository
        2. Run CreateTriples.py on a .osm.pbf file -> output.ttl
        3. Parse the .ttl using rdflib
        4. Return per-node enrichment dicts keyed by OSM node ID

    Usage:
        service = WorldKGTriplesService(worldkg_dir='/path/to/WorldKG-Knowledge-Graph')
        ok = service.run_create_triples('/path/to/region.osm.pbf', '/tmp/region.ttl')
        if ok:
            enrichments = service.parse_ttl_to_enrichment('/tmp/region.ttl')
            # enrichments: {osm_id: {wkg_class, wkg_type_key, wkg_type_value, ...}}

    Prerequisites:
        - Python >= 3.7 in the worldkg_dir virtualenv
        - osmium Python library installed (pip install osmium)
        - rdflib installed (already in pyproject.toml)
        - WorldKG repo cloned: git clone https://github.com/alishiba14/WorldKG-Knowledge-Graph
    """

    def __init__(self, worldkg_dir: Optional[str] = None):
        self.worldkg_dir = Path(worldkg_dir) if worldkg_dir else None

    def run_create_triples(
        self,
        pbf_path: str,
        output_ttl: str,
        python_bin: str = "python3"
    ) -> bool:
        """
        Run the official WorldKG CreateTriples.py script on a PBF file.

        Args:
            pbf_path:   Path to input .osm.pbf file
            output_ttl: Path to write output RDF Turtle file
            python_bin: Python interpreter to use (default: python3)

        Returns:
            True if the script completed successfully
        """
        if not self.worldkg_dir or not self.worldkg_dir.exists():
            logger.error(
                f"WorldKG repository not found at {self.worldkg_dir}. "
                f"Clone it: git clone {WORLDKG_REPO_URL}"
            )
            return False

        create_triples_script = self.worldkg_dir / "CreateTriples.py"
        if not create_triples_script.exists():
            logger.error(f"CreateTriples.py not found at {create_triples_script}")
            return False

        try:
            result = subprocess.run(
                [python_bin, str(create_triples_script), pbf_path, output_ttl],
                cwd=str(self.worldkg_dir),
                capture_output=True,
                text=True,
                timeout=3600  # 1 hour max for large PBF files
            )

            if result.returncode != 0:
                logger.error(
                    f"CreateTriples.py failed (exit {result.returncode}):\n{result.stderr}"
                )
                return False

            logger.info(
                f"CreateTriples.py succeeded. Output: {output_ttl}. "
                f"Stdout: {result.stdout[:500]}"
            )
            return True

        except subprocess.TimeoutExpired:
            logger.error("CreateTriples.py timed out after 1 hour")
            return False
        except Exception as e:
            logger.error(f"Error running CreateTriples.py: {e}")
            return False

    def parse_ttl_to_enrichment(
        self,
        ttl_path: str,
        ontology_service=None
    ) -> Dict[int, Dict]:
        """
        Parse a WorldKG-generated .ttl file into per-node enrichment dicts.

        Extracts the rdf:type assertion and wkgs:osmLink for each entity,
        mapping OSM node ID -> enrichment data for OsmEntity.batch_update.

        Args:
            ttl_path:         Path to .ttl file from CreateTriples.py
            ontology_service: Optional WorldKGOntologyService for superclass resolution

        Returns:
            Dict mapping osm_node_id (int) -> {
                'wkg_class':        'wkgs:Restaurant',
                'wkg_superclasses': ['wkgs:Amenity', 'wkgs:WKGObject'],
                'wkg_depth':        2,
                'wkg_type_key':     'amenity',
                'wkg_type_value':   'restaurant',
                'wikidata_uri':     'http://www.wikidata.org/entity/Q11707' or None,
                'source':           'create_triples'
            }
        """
        try:
            from rdflib import Graph, RDF, OWL, Namespace
            from rdflib.namespace import RDFS
        except ImportError:
            logger.error("rdflib not installed. Run: pip install rdflib")
            return {}

        WKGS = Namespace(WKGS_BASE_URI)

        g = Graph()
        logger.info(f"Parsing WorldKG TTL: {ttl_path}")
        g.parse(ttl_path, format='turtle')
        logger.info(f"Loaded {len(g)} triples from {ttl_path}")

        enrichments: Dict[int, Dict] = {}

        for entity, wkgs_class_uri in g.subject_objects(RDF.type):
            wkgs_uri_str = str(wkgs_class_uri)
            if not wkgs_uri_str.startswith(WKGS_BASE_URI):
                continue

            class_short = 'wkgs:' + wkgs_uri_str[len(WKGS_BASE_URI):]

            # Get OSM node ID via wkgs:osmLink
            osm_id = None
            for osm_link in g.objects(entity, WKGS.osmLink):
                link_str = str(osm_link)
                if link_str.startswith(OSMN_BASE_URI):
                    try:
                        osm_id = int(link_str[len(OSMN_BASE_URI):])
                    except ValueError:
                        pass
                    break

            if osm_id is None:
                continue

            # Resolve class hierarchy and metadata from ontology service if available
            superclasses = []
            depth = 2 if class_short != 'wkgs:WKGObject' else 0
            wkg_type_key = None
            wkg_type_value = None
            wikidata_uri = None

            if ontology_service:
                superclasses = ontology_service.get_superclasses(class_short)
                depth = ontology_service.get_depth(class_short)
                wkg_type_key = ontology_service.get_canonical_osm_key(class_short)
                wkg_type_value = ontology_service.get_canonical_osm_value(class_short)
                wikidata_uri = ontology_service.get_wikidata_equivalent(class_short)
            else:
                # Try to infer from owl:equivalentClass in the TTL itself
                for equiv in g.objects(wkgs_class_uri, OWL.equivalentClass):
                    equiv_str = str(equiv)
                    if 'wikidata.org' in equiv_str:
                        wikidata_uri = equiv_str
                        break

            enrichments[osm_id] = {
                'wkg_class': class_short,
                'wkg_superclasses': superclasses,
                'wkg_depth': depth,
                'wkg_type_key': wkg_type_key,
                'wkg_type_value': wkg_type_value,
                'wikidata_uri': wikidata_uri,
                'source': 'create_triples',
            }

        logger.info(f"Parsed {len(enrichments)} entity enrichments from {ttl_path}")
        return enrichments

    def bulk_apply_enrichment(
        self,
        enrichments: Dict[int, Dict],
        batch_size: int = 1000
    ) -> Dict:
        """
        Apply parsed TTL enrichments to OsmEntity records in the database.

        Args:
            enrichments: Dict from parse_ttl_to_enrichment()
            batch_size:  DB update batch size

        Returns:
            Stats dict with 'updated', 'not_found' counts
        """
        from django.utils import timezone
        from worldkg_nca.models import OsmEntity

        stats = {'updated': 0, 'not_found': 0}
        osm_ids = list(enrichments.keys())

        for offset in range(0, len(osm_ids), batch_size):
            batch_ids = osm_ids[offset:offset + batch_size]
            entities = {
                e.osm_id: e
                for e in OsmEntity.objects.using('vectors').filter(
                    osm_id__in=batch_ids, osm_type='node'
                )
            }

            to_update = []
            for osm_id in batch_ids:
                entity = entities.get(osm_id)
                if not entity:
                    stats['not_found'] += 1
                    continue

                data = enrichments[osm_id]
                entity.wkg_class = data['wkg_class']
                entity.wkg_superclasses = data['wkg_superclasses']
                entity.wkg_depth = data['wkg_depth']
                entity.wkg_type_key = data.get('wkg_type_key')
                entity.wkg_type_value = data.get('wkg_type_value')
                entity.wikidata_uri = data.get('wikidata_uri')
                entity.wkg_enriched_at = timezone.now()
                to_update.append(entity)

            if to_update:
                OsmEntity.objects.using('vectors').bulk_update(
                    to_update,
                    ['wkg_class', 'wkg_superclasses', 'wkg_depth',
                     'wkg_type_key', 'wkg_type_value', 'wikidata_uri', 'wkg_enriched_at']
                )
                stats['updated'] += len(to_update)

            logger.info(
                f"Batch {offset // batch_size + 1}: {len(to_update)} entities updated"
            )

        return stats
