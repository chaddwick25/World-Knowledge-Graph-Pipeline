"""Unit tests for Step 5c GraphML serialization guard and run reports.

Validates:
- The ``GRAPHML_NODE_THRESHOLD`` guard in ``_serialize_graph``
- The ``_write_report`` function for JSON run reports

These tests do NOT require a database — they build synthetic
``networkx`` graphs in-memory and mock the Django settings.
"""

import json
import os
import tempfile
from unittest import mock

import networkx as nx
import pytest

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "backend.settings")
django.setup()

from pipeline.tasks.country_pipeline_steps.step_5c_graph_spectral import (
    GRAPHML_NODE_THRESHOLD,
    _serialize_graph,
    _write_report,
)


# --------------------------------------------------------------------------- #
# Fix A: GraphML skip guard
# --------------------------------------------------------------------------- #

def test_serialize_graph_skipped_for_large_graph(tmp_path):
    """Graphs ≥ GRAPHML_NODE_THRESHOLD nodes → no file written, info log."""
    G = nx.path_graph(GRAPHML_NODE_THRESHOLD + 1)
    with mock.patch("django.conf.settings.GRAPH_ARTIFACT_DIR", str(tmp_path)):
        result = _serialize_graph(G, "XX", "2025_12_31")
    assert result == "skipped_large"
    graphml_files = list(tmp_path.glob("*.graphml"))
    assert graphml_files == [], f"Unexpected GraphML files: {graphml_files}"


def test_serialize_graph_written_for_small_graph(tmp_path):
    """Graphs < GRAPHML_NODE_THRESHOLD nodes → GraphML file written."""
    G = nx.path_graph(100)  # well below threshold
    with mock.patch("django.conf.settings.GRAPH_ARTIFACT_DIR", str(tmp_path)):
        result = _serialize_graph(G, "XX", "2025_12_31")
    assert result == "written"
    graphml_files = list(tmp_path.glob("*.graphml"))
    assert len(graphml_files) == 1, f"Expected 1 GraphML file, got {graphml_files}"
    assert graphml_files[0].name == "xx_2025_12_31.graphml"


def test_serialize_graph_threshold_boundary_skipped(tmp_path):
    """Graph with exactly GRAPHML_NODE_THRESHOLD nodes → skipped (≥)."""
    G = nx.path_graph(GRAPHML_NODE_THRESHOLD)
    with mock.patch("django.conf.settings.GRAPH_ARTIFACT_DIR", str(tmp_path)):
        result = _serialize_graph(G, "XX", "2025_12_31")
    assert result == "skipped_large"
    graphml_files = list(tmp_path.glob("*.graphml"))
    assert graphml_files == [], "Boundary case should be skipped"


def test_serialize_graph_no_graph_artifact_dir(tmp_path):
    """GRAPH_ARTIFACT_DIR not configured → no crash, no file."""
    G = nx.path_graph(100)
    with mock.patch("django.conf.settings.GRAPH_ARTIFACT_DIR", None):
        result = _serialize_graph(G, "XX", "2025_12_31")
    assert result == "skipped_no_dir"


# --------------------------------------------------------------------------- #
# Run report
# --------------------------------------------------------------------------- #

class _MockEnv:
    """Minimal stand-in for CountryEnvelope used by _write_report."""
    def __init__(self, iso="BZ", snapshot_date="2025_12_31", run_id="test_001"):
        self.iso = iso
        self.snapshot_date = snapshot_date
        self.pipeline_run_id = run_id


def test_write_report_creates_json(tmp_path):
    """_write_report writes a JSON file with the report content."""
    report = {
        "country": "BZ",
        "status": "completed",
        "stages": {
            "eigensolve": {"solver": "gpu_lobpcg", "time_s": 15.4},
        },
    }
    with mock.patch("django.conf.settings.SPECTRAL_REPORT_DIR", str(tmp_path)):
        _write_report(report, _MockEnv())
    files = list(tmp_path.glob("step5c_bz_*.json"))
    assert len(files) == 1, f"Expected 1 report, got {files}"
    with open(files[0]) as f:
        loaded = json.load(f)
    assert loaded["country"] == "BZ"
    assert loaded["status"] == "completed"
    assert loaded["stages"]["eigensolve"]["solver"] == "gpu_lobpcg"


def test_write_report_no_dir_does_not_crash(tmp_path):
    """SPECTRAL_REPORT_DIR not configured → no crash, report logged."""
    report = {"country": "BZ", "status": "completed"}
    with mock.patch("django.conf.settings.SPECTRAL_REPORT_DIR", None):
        _write_report(report, _MockEnv())
    # Should not crash — just log the report inline


def test_serialize_graph_returns_status_string(tmp_path):
    """_serialize_graph returns a status string, not None."""
    G = nx.path_graph(50)
    with mock.patch("django.conf.settings.GRAPH_ARTIFACT_DIR", str(tmp_path)):
        result = _serialize_graph(G, "XX", "2025_12_31")
    assert isinstance(result, str)
    assert result == "written"
