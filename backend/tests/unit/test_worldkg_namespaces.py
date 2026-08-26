"""
Regression tests for WorldKG RDF namespace alignment (2026-08-26).

The ontology TTL under ``settings.WORLDKG_ONTOLOGY_PATH`` declares
``wkgs: <http://www.worldkg.org/schema/>``. Two services previously hardcoded
``http://schema.worldkg.org/`` — which matches ZERO triples in the data
(proven: 1171 classes parse under the canonical namespace, 0 under the bogus
one). The GeoVectors v2 namespaces (``geovectors.l3s.uni-hannover.de/...``)
are the OLD corpus and also match zero triples in WorldKG 1.0 dumps.

This test locks the namespace constants to the actual data and guards
against re-introducing the mismatch.

Sources:
- WorldKG 1.0 dumps: ``settings.WORLDKG_ONTOLOGY_PATH`` (on-disk TTL files)
- WorldKG project: https://www.worldkg.org/
- WorldKG GitHub: https://github.com/alishiba14/WorldKG-Knowledge-Graph
- Zenodo ontology record: https://zenodo.org/record/4953986
- GeoVectors paper (old corpus): arXiv:2108.13092 (Fig. 2 namespaces)
"""

import pytest
from pathlib import Path

from django.conf import settings

CANONICAL_WKGS = "http://www.worldkg.org/schema/"
CANONICAL_WKG = "http://www.worldkg.org/resource/"
BOGUS_WKGS = "http://schema.worldkg.org/"
GEOVECTORS_WKGS = "http://geovectors.l3s.uni-hannover.de/schema/"


def _ttl_path() -> Path:
    return Path(getattr(settings, "WORLDKG_ONTOLOGY_PATH", ""))


def _declared_wkgs_prefix(ttl_path: Path):
    """Extract the ``@prefix wkgs:`` declaration from the TTL header."""
    with open(ttl_path) as f:
        for line in f:
            if line.startswith("@prefix wkgs:"):
                return line.split("<")[1].split(">")[0]
            if not line.startswith("@"):
                break
    return None


def _count_classes_under(ttl_path: Path, namespace: str) -> int:
    """Count distinct classes (rdfs:subClassOf subjects/objects) under a URI base."""
    from rdflib import Graph
    from rdflib.namespace import RDFS

    g = Graph()
    g.parse(ttl_path, format="turtle")
    cls = set()
    for s, o in g.subject_objects(RDFS.subClassOf):
        if str(s).startswith(namespace):
            cls.add(str(s))
        if str(o).startswith(namespace):
            cls.add(str(o))
    return len(cls)


def _has_ontology_ttl():
    return _ttl_path().exists() and _ttl_path().stat().st_size > 0


requires_ttl = pytest.mark.skipif(
    not _has_ontology_ttl(),
    reason=f"WorldKG ontology TTL not found at {getattr(settings, 'WORLDKG_ONTOLOGY_PATH', '?')}",
)


class TestWorldKGNamespaceAlignment:
    """Namespace constants MUST match the actual WorldKG 1.0 data."""

    @pytest.mark.unit
    @requires_ttl
    def test_ttl_declares_canonical_wkgs_namespace(self):
        declared = _declared_wkgs_prefix(_ttl_path())
        assert declared == CANONICAL_WKGS, (
            f"Ontology TTL declares wkgs: {declared!r}; "
            f"code constants must use {CANONICAL_WKGS!r}"
        )
        assert declared != BOGUS_WKGS
        assert declared != GEOVECTORS_WKGS

    @pytest.mark.unit
    @requires_ttl
    def test_ontology_loader_parses_classes_with_canonical_namespace(self):
        from worldkg_nca.services.ontology_loader import WorldKGOntologyLoader

        ontology = WorldKGOntologyLoader().load_from_ttl(str(_ttl_path()))
        assert len(ontology) >= 1000, (
            f"Expected >=1000 classes under {CANONICAL_WKGS}, got {len(ontology)}"
        )
        for expected in ("wkgs:WKGObject", "wkgs:Amenity", "wkgs:Restaurant"):
            assert expected in ontology, f"Missing expected class {expected}"

    @pytest.mark.unit
    @requires_ttl
    def test_bogus_namespaces_match_zero_classes(self):
        """Guard: neither the bogus variant nor the GeoVectors v2 namespace
        match any triples in the WorldKG 1.0 dump."""
        ttl = _ttl_path()
        assert _count_classes_under(ttl, CANONICAL_WKGS) > 1000
        assert _count_classes_under(ttl, BOGUS_WKGS) == 0
        assert _count_classes_under(ttl, GEOVECTORS_WKGS) == 0

    @pytest.mark.unit
    def test_service_namespace_constants_match_data(self):
        """Regression for the 2026-08-26 bug: two services hardcoded
        http://schema.worldkg.org/ while the data uses www.worldkg.org/schema/."""
        from semantic_search.services import worldkg_enrichment_service
        from worldkg_nca.services import triples_service
        from worldkg_nca.services.ontology_loader import (
            WKGS_BASE_URI as ONTOLOGY_LOADER_WKGS,
        )

        assert worldkg_enrichment_service.WKGS_BASE_URI == CANONICAL_WKGS
        assert worldkg_enrichment_service.WKG_BASE_URI == CANONICAL_WKG
        assert triples_service.WKGS_BASE_URI == CANONICAL_WKGS
        assert ONTOLOGY_LOADER_WKGS == CANONICAL_WKGS

        # All three modules must agree on the schema namespace.
        namespaces = {
            worldkg_enrichment_service.WKGS_BASE_URI,
            triples_service.WKGS_BASE_URI,
            ONTOLOGY_LOADER_WKGS,
        }
        assert len(namespaces) == 1, (
            f"WKGS_BASE_URI diverges across modules: {namespaces}"
        )
