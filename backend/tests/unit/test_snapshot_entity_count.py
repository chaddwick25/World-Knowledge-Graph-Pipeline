"""``_extract_entity_count`` reads the right JSON shape.

Regression cover for the k80 v2 run (2026-11-05): the pre-processing log
reported ``entity_count=0`` for a 21 MB snapshot that actually held 307,938
entities.  The parser looked for ``info["objects"]``, which ``osmium fileinfo
--json`` never emits — counts live under ``data.count`` and only appear with
``--extended``.
"""

import pytest

from core.services.snapshot.snapshot_extraction_service import (
    SnapshotExtractionService,
)

pytestmark = pytest.mark.unit


def _service():
    """Bare instance — the parser needs no collaborators."""
    return SnapshotExtractionService.__new__(SnapshotExtractionService)


def test_counts_are_summed_from_data_count():
    info = {
        "info": {
            "data": {
                "count": {"nodes": 3267413, "ways": 318831, "relations": 6648},
            },
        },
    }
    assert _service()._extract_entity_count(info) == 3592892


def test_changesets_are_not_counted_as_entities():
    info = {
        "info": {
            "data": {
                "count": {"nodes": 10, "ways": 2, "relations": 1, "changesets": 500},
            },
        },
    }
    assert _service()._extract_entity_count(info) == 13


def test_plain_fileinfo_without_counts_returns_zero():
    """Non-extended fileinfo has only file/header — the old failure mode."""
    assert _service()._extract_entity_count({"info": {"header": {}}}) == 0


def test_malformed_input_returns_zero():
    assert _service()._extract_entity_count({}) == 0
    assert _service()._extract_entity_count({"info": {"data": {"count": None}}}) == 0
