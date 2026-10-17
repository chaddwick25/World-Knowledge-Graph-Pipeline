"""Unit tests for the class-drift capability slice
(ISSUE-class-drift-capability):

- classDrift is in the RESEARCH_TOOLS schema and validates zero-arg calls
- _class_drift_report returns the fingerprint summary on a
  snapshot-only country, and pair stats when a drift row exists
- compute_class_drift fills missing consecutive pairs and skips
  existing ones (idempotent)
- factor_availability reports class_drift.available=false on a
  single-snapshot country

Pure-mock — no database.
"""

import os
from types import SimpleNamespace

import pytest

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "backend.settings")
django.setup()

from django.core.management import call_command

import osmsnapshot.models as osm_models
from core.services.planet_init import osm_wikidata_resolver
from semantic_search.management.commands import compute_class_drift as cmd_mod
from semantic_search.services.query_enrichment_service import (
    QueryEnrichmentService,
)
from worldkg_nca.views import factor as factor_view


class _QS:
    """Chainable queryset stub — filter/order_by return self."""

    def __init__(self, items=()):
        self.items = list(items)

    def filter(self, **kwargs):
        return self

    def order_by(self, *args):
        return self

    def first(self):
        return self.items[0] if self.items else None

    def last(self):
        return self.items[-1] if self.items else None

    def exists(self):
        return bool(self.items)

    def count(self):
        return len(self.items)

    def __iter__(self):
        return iter(self.items)


def _snap(date):
    return SimpleNamespace(
        id=f"id-{date}", country_code="IE", snapshot_date=date,
    )


def _fp_row():
    return SimpleNamespace(
        top_5_classes={"wkgs:Highway": 40.0, "wkgs:Amenity": 20.0},
        shannon_entropy=2.31,
        unique_classes=42,
        total_entities=100000,
    )


def _drift_row(snap_from, snap_to):
    return SimpleNamespace(
        snapshot_from=snap_from,
        snapshot_to=snap_to,
        js_divergence=0.42,
        drift_magnitude="high",
        dominant_shift={"wkgs:Amenity": 5.0},
        new_classes=["wkgs:Office"],
        lost_classes=[],
    )


@pytest.fixture
def patch_resolver(monkeypatch):
    monkeypatch.setattr(
        osm_wikidata_resolver, "resolve_iso_code", lambda c: c,
    )


# ── Schema / validation ──────────────────────────────────────────────────

def test_class_drift_in_research_tools_schema():
    schema = QueryEnrichmentService._research_tools_schema()
    names = {s["function"]["name"] for s in schema}
    assert "classDrift" in names
    spec = next(
        s["function"] for s in schema if s["function"]["name"] == "classDrift"
    )
    assert spec["parameters"]["required"] == []


def test_validate_tool_calls_accepts_classdrift():
    calls = [{"name": "classDrift", "arguments": {}}]
    validated = QueryEnrichmentService._validate_tool_calls(calls)
    assert validated == [("classDrift", {})]


# ── Executor report shape ────────────────────────────────────────────────

def _patch_osm_models(monkeypatch, snaps, fp_row, drift_rows):
    monkeypatch.setattr(
        osm_models, "Snapshot",
        SimpleNamespace(
            objects=SimpleNamespace(
                filter=lambda **kw: _QS(snaps),
            ),
        ),
    )
    monkeypatch.setattr(
        osm_models, "WorldKGClassFingerprint",
        SimpleNamespace(
            objects=SimpleNamespace(
                filter=lambda **kw: _QS([fp_row] if fp_row else []),
            ),
        ),
    )
    monkeypatch.setattr(
        osm_models, "WorldKGClassDrift",
        SimpleNamespace(
            objects=SimpleNamespace(
                filter=lambda **kw: _QS(drift_rows),
            ),
        ),
    )


def test_class_drift_report_fingerprint_only(patch_resolver, monkeypatch):
    """Single-snapshot country: fingerprint present, drift is null."""
    snap = _snap("2025_12_31")
    _patch_osm_models(monkeypatch, [snap], _fp_row(), [])
    out = QueryEnrichmentService._class_drift_report("IE", None)
    assert out["fingerprint"]["unique_classes"] == 42
    assert out["fingerprint"]["top_classes"]["wkgs:Highway"] == 40.0
    assert out["drift"] is None


def test_class_drift_report_with_drift(patch_resolver, monkeypatch):
    s1, s2 = _snap("2024_12_31"), _snap("2025_12_31")
    _patch_osm_models(
        monkeypatch, [s1, s2], _fp_row(), [_drift_row(s1, s2)],
    )
    out = QueryEnrichmentService._class_drift_report("ie", "2025_12_31")
    assert out["fingerprint"]["snapshot_date"] == "2025_12_31"
    drift = out["drift"]
    assert drift["pairs"] == 1
    assert drift["latest"]["from"] == "2024_12_31"
    assert drift["latest"]["to"] == "2025_12_31"
    assert drift["latest"]["js_divergence"] == 0.42
    assert drift["latest"]["drift_magnitude"] == "high"


# ── Command idempotency ──────────────────────────────────────────────────

def test_compute_class_drift_fills_missing_pair(monkeypatch):
    s1, s2 = _snap("2024_12_31"), _snap("2025_12_31")
    monkeypatch.setattr(
        cmd_mod, "Snapshot",
        SimpleNamespace(
            objects=SimpleNamespace(
                order_by=lambda *a: _QS([s1, s2]),
            ),
        ),
    )
    monkeypatch.setattr(
        cmd_mod, "WorldKGClassDrift",
        SimpleNamespace(
            objects=SimpleNamespace(
                filter=lambda **kw: _QS([]),
            ),
        ),
    )
    calls = []
    fake_service = SimpleNamespace(
        fill_pair_if_missing=lambda a, b: (
            calls.append((a, b)) or _drift_row(a, b)
        ),
    )
    monkeypatch.setattr(
        cmd_mod, "get_worldkg_drift_service", lambda: fake_service,
    )
    call_command("compute_class_drift", country="IE")
    assert calls == [(s1, s2)]


def test_compute_class_drift_skips_existing_pair(monkeypatch):
    s1, s2 = _snap("2024_12_31"), _snap("2025_12_31")
    monkeypatch.setattr(
        cmd_mod, "Snapshot",
        SimpleNamespace(
            objects=SimpleNamespace(
                order_by=lambda *a: _QS([s1, s2]),
            ),
        ),
    )
    monkeypatch.setattr(
        cmd_mod, "WorldKGClassDrift",
        SimpleNamespace(
            objects=SimpleNamespace(
                filter=lambda **kw: _QS([object()]),
            ),
        ),
    )
    calls = []
    fake_service = SimpleNamespace(
        # Existing pair: service returns None, no compute attempted.
        fill_pair_if_missing=lambda a, b: None,
        compute_drift=lambda a, b: calls.append((a, b)),
    )
    monkeypatch.setattr(
        cmd_mod, "get_worldkg_drift_service", lambda: fake_service,
    )
    call_command("compute_class_drift", country="IE")
    assert calls == []


def test_compute_class_drift_single_snapshot_noop(monkeypatch):
    monkeypatch.setattr(
        cmd_mod, "Snapshot",
        SimpleNamespace(
            objects=SimpleNamespace(
                order_by=lambda *a: _QS([_snap("2025_12_31")]),
            ),
        ),
    )
    calls = []
    fake_service = SimpleNamespace(
        compute_drift=lambda a, b: calls.append((a, b)),
    )
    monkeypatch.setattr(
        cmd_mod, "get_worldkg_drift_service", lambda: fake_service,
    )
    call_command("compute_class_drift", country="IE")
    assert calls == []


# ── Manifest fields ──────────────────────────────────────────────────────

def test_factor_availability_single_snapshot_drift_unavailable(
    patch_resolver, monkeypatch,
):
    class _Cursor:
        def execute(self, sql, params=None):
            return None

        def fetchone(self):
            return None

    import django.db
    monkeypatch.setattr(
        django.db, "connections",
        {"vectors": SimpleNamespace(cursor=lambda: _Cursor())},
    )
    monkeypatch.setattr(factor_view, "resolve_iso_code", lambda c: c)
    _patch_osm_models(monkeypatch, [_snap("2025_12_31")], _fp_row(), [])

    from rest_framework.test import APIRequestFactory
    req = APIRequestFactory().get(
        "/api/nca/factor-availability/",
        {"country_code": "IE", "snapshot_date": "2025_12_31"},
    )
    resp = factor_view.factor_availability(req)
    assert resp.status_code == 200
    assert resp.data["class_profile"]["available"] is True
    assert resp.data["class_profile"]["unique_classes"] == 42
    assert resp.data["class_drift"] == {"available": False, "pairs": 0}
