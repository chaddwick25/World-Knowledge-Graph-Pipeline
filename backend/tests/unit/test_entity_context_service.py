"""Unit tests for EntityContextService (deterministic enrichment context).

Hermetic — no DB, no LLM. Verifies:
  - per-template source routing (TEMPLATE_CONTEXT_MAP)
  - unknown / missing template → all three sources (safe default)
  - empty osm_ids → all-empty context, no queries run
  - per-source query construction (USLP / communities / classes)
  - the G4 availability gate on the communities source
  - fail-soft per source (exceptions → empty, never raise)
"""

import pytest
from django.db.models import Count

from semantic_search.services.entity_context_service import (
    ALL_SOURCES,
    TEMPLATE_CONTEXT_MAP,
    EntityContextService,
)


# ── Helpers ──────────────────────────────────────────────────────────────

class _FakeQS:
    """Chainable queryset fake that records calls and returns canned rows."""

    def __init__(self, rows):
        self._rows = list(rows)
        self.calls = []

    def using(self, db):
        self.calls.append(("using", db))
        return self

    def filter(self, **kw):
        self.calls.append(("filter", kw))
        return self

    def exclude(self, **kw):
        self.calls.append(("exclude", kw))
        return self

    def values(self, *fields):
        self.calls.append(("values", fields))
        return self

    def annotate(self, **kw):
        self.calls.append(("annotate", kw))
        return self

    def order_by(self, *fields):
        self.calls.append(("order_by", fields))
        return self

    def __getitem__(self, item):
        return self._rows[item]

    def __iter__(self):
        return iter(self._rows)


class _FakeManager:
    def __init__(self, rows):
        self.qs = _FakeQS(rows)

    def using(self, db):
        return self.qs.using(db)


def _never(*a, **k):
    raise AssertionError("must not be called")


# ── Routing (TEMPLATE_CONTEXT_MAP) ───────────────────────────────────────

class TestRouting:
    def _patch_sources(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            EntityContextService, "_uslp_links",
            classmethod(lambda cls, ids, snap: calls.append("uslp") or [{"head_osm_id": 1}]),
        )
        monkeypatch.setattr(
            EntityContextService, "_communities",
            classmethod(lambda cls, ids, snap, cc: calls.append("communities") or {1: {"community": 3}}),
        )
        monkeypatch.setattr(
            EntityContextService, "_class_distribution",
            classmethod(lambda cls, ids, snap, cc: calls.append("classes") or [{"wkg_class": "wkgs:Cafe", "count": 2}]),
        )
        return calls

    def test_full_template_fetches_all_three(self, monkeypatch):
        calls = self._patch_sources(monkeypatch)
        out = EntityContextService.get_context(
            [1, 2], "BZ", "2025_12_31", template="FILTER-AGGREGATE-MEASURE (#1)",
        )
        assert set(calls) == {"uslp", "communities", "classes"}
        assert out["uslp_links"] == [{"head_osm_id": 1}]
        assert out["communities"] == {1: {"community": 3}}
        assert out["class_distribution"] == [{"wkg_class": "wkgs:Cafe", "count": 2}]

    def test_bearing_template_only_classes(self, monkeypatch):
        calls = []
        monkeypatch.setattr(EntityContextService, "_uslp_links", classmethod(_never))
        monkeypatch.setattr(EntityContextService, "_communities", classmethod(_never))
        monkeypatch.setattr(
            EntityContextService, "_class_distribution",
            classmethod(lambda cls, ids, snap, cc: calls.append("classes") or [{"wkg_class": "wkgs:Bar", "count": 1}]),
        )
        out = EntityContextService.get_context(
            [1], "BZ", "2025_12_31", template="LOCATION-BEARING-CLASSIFY (#5)",
        )
        assert calls == ["classes"]
        assert out["uslp_links"] == [] and out["communities"] == {}
        assert out["class_distribution"] == [{"wkg_class": "wkgs:Bar", "count": 1}]

    def test_graph_template_only_communities(self, monkeypatch):
        calls = []
        monkeypatch.setattr(EntityContextService, "_uslp_links", classmethod(_never))
        monkeypatch.setattr(
            EntityContextService, "_communities",
            classmethod(lambda cls, ids, snap, cc: calls.append("communities") or {1: {"community": 2}}),
        )
        monkeypatch.setattr(EntityContextService, "_class_distribution", classmethod(_never))
        out = EntityContextService.get_context(
            [1], "BZ", "2025_12_31", template="EVENT-DIFFUSION (#14)",
        )
        assert calls == ["communities"]
        assert out["communities"] == {1: {"community": 2}}
        assert out["uslp_links"] == [] and out["class_distribution"] == []

    def test_unknown_template_fetches_all_three(self, monkeypatch):
        calls = self._patch_sources(monkeypatch)
        EntityContextService.get_context(
            [1], "BZ", "2025_12_31", template="NOT-A-TEMPLATE",
        )
        assert set(calls) == {"uslp", "communities", "classes"}

    def test_no_template_fetches_all_three(self, monkeypatch):
        calls = self._patch_sources(monkeypatch)
        EntityContextService.get_context([1], "BZ", "2025_12_31")
        assert set(calls) == {"uslp", "communities", "classes"}

    def test_template_map_has_expected_keys(self):
        assert TEMPLATE_CONTEXT_MAP["FILTER-AGGREGATE-MEASURE (#1)"] == ALL_SOURCES
        assert TEMPLATE_CONTEXT_MAP["LOCATION-BEARING-CLASSIFY (#5)"] == {"classes"}
        assert "OBJECT-FIELD-MEASURE (#2)" in TEMPLATE_CONTEXT_MAP
        assert "PLACE-ATTRIBUTE-QUERY (#8)" in TEMPLATE_CONTEXT_MAP

    def test_empty_osm_ids_returns_empty_without_queries(self, monkeypatch):
        monkeypatch.setattr(EntityContextService, "_uslp_links", classmethod(_never))
        monkeypatch.setattr(EntityContextService, "_communities", classmethod(_never))
        monkeypatch.setattr(EntityContextService, "_class_distribution", classmethod(_never))
        assert EntityContextService.get_context([], "BZ", "2025_12_31") == {
            "uslp_links": [], "communities": {}, "class_distribution": [],
        }

    def test_trace_appended(self, monkeypatch):
        calls = self._patch_sources(monkeypatch)
        trace = []
        EntityContextService.get_context(
            [1], "BZ", "2025_12_31",
            template="PLACE-ATTRIBUTE-QUERY (#8)", trace=trace,
        )
        step = trace[-1]
        assert step["step"] == "entity_context"
        assert step["sources"] == sorted({"uslp", "classes"})
        assert step["uslp_links"] == 1
        assert step["class_distribution"] == 1
        assert calls == ["uslp", "classes"]


# ── Per-source query construction ────────────────────────────────────────

class TestUslpLinks:
    def test_query_scoping_and_cap(self, monkeypatch):
        rows = [{"head_osm_id": 1, "relation": "within_50m_of",
                 "tail_osm_id": 9, "normalized_score": 0.9}]
        fake = _FakeManager(rows)
        monkeypatch.setattr("igea.models.SpatialTripletScore.objects", fake)
        out = EntityContextService._uslp_links([1, 2], "2025_12_31")
        assert out == rows
        qs = fake.qs
        filter_kwargs = [c[1] for c in qs.calls if c[0] == "filter"][0]
        assert filter_kwargs == {
            "head_osm_id__in": [1, 2],
            "predicted": True,
            "snapshot_id": "2025_12_31",
        }
        assert ("using", "vectors") in qs.calls
        assert ("values", ("head_osm_id", "relation", "tail_osm_id", "normalized_score")) in qs.calls
        assert qs.calls[-1] == ("order_by", ("-normalized_score",))

    def test_fail_soft_on_error(self, monkeypatch):
        monkeypatch.setattr("igea.models.SpatialTripletScore.objects", object())
        assert EntityContextService._uslp_links([1], "2025_12_31") == []


class TestCommunities:
    def _patch_svc(self, monkeypatch, availability, metrics=None):
        from semantic_search.services.factor_resolution_service import (
            FactorResolutionService,
        )
        monkeypatch.setattr(
            FactorResolutionService, "check_availability",
            lambda self, ids, snap, cc: availability,
        )
        monkeypatch.setattr(
            FactorResolutionService, "resolve_metrics",
            lambda self, ids, snap, cc, subgraph_slug=None: metrics or {},
        )

    def test_g4_gate_skips_resolve_when_no_rows(self, monkeypatch):
        from semantic_search.services.factor_resolution_service import (
            FactorResolutionService,
        )
        monkeypatch.setattr(
            FactorResolutionService, "check_availability",
            lambda self, ids, snap, cc: {1: False, 2: False},
        )
        monkeypatch.setattr(FactorResolutionService, "resolve_metrics", _never)
        assert EntityContextService._communities([1, 2], "2025_12_31", "BZ") == {}

    def test_compact_metric_shape(self, monkeypatch):
        self._patch_svc(
            monkeypatch, {1: True},
            metrics={
                1: {"fiedler_component": 3, "louvain_community": 7, "degree": 12,
                    "clustering_coeff": 0.4, "component_id": "c1",
                    "component_size": 30, "dirichlet_contrib": 0.1,
                    "subgraph_slug": "bz-belize-city"},
            },
        )
        out = EntityContextService._communities([1], "2025_12_31", "BZ")
        assert out == {1: {"community": 7, "fiedler": 3, "degree": 12,
                           "component_size": 30, "subgraph_slug": "bz-belize-city"}}

    def test_no_country_skips(self, monkeypatch):
        from semantic_search.services.factor_resolution_service import (
            FactorResolutionService,
        )
        monkeypatch.setattr(FactorResolutionService, "check_availability", _never)
        assert EntityContextService._communities([1], "2025_12_31", None) == {}

    def test_fail_soft_on_error(self, monkeypatch):
        from semantic_search.services.factor_resolution_service import (
            FactorResolutionService,
        )
        def boom(*a, **k):
            raise RuntimeError("vectors DB down")
        monkeypatch.setattr(FactorResolutionService, "check_availability", boom)
        assert EntityContextService._communities([1], "2025_12_31", "BZ") == {}


class TestClassDistribution:
    def test_query_scoping_and_nulls(self, monkeypatch):
        rows = [{"wkg_class": "wkgs:Cafe", "count": 2}]
        fake = _FakeManager(rows)
        monkeypatch.setattr("worldkg_nca.models.OsmEntity.objects", fake)
        out = EntityContextService._class_distribution([1, 2], "2025_12_31", "bz")
        assert out == rows
        qs = fake.qs
        filter_kwargs = [c[1] for c in qs.calls if c[0] == "filter"]
        assert filter_kwargs[0] == {"osm_id__in": [1, 2], "snapshot_id": "2025_12_31"}
        assert filter_kwargs[1] == {"country_code": "BZ"}  # uppercased
        excludes = [c[1] for c in qs.calls if c[0] == "exclude"]
        assert {"wkg_class__isnull": True} in excludes
        assert {"wkg_class": ""} in excludes
        assert ("values", ("wkg_class",)) in qs.calls
        assert ("annotate", {"count": Count("osm_id")}) in qs.calls
        assert ("order_by", ("-count",)) in qs.calls
        assert ("filter", {"country_code": "BZ"}) in qs.calls

    def test_no_country_skips_country_filter(self, monkeypatch):
        rows = [{"wkg_class": "wkgs:Cafe", "count": 1}]
        fake = _FakeManager(rows)
        monkeypatch.setattr("worldkg_nca.models.OsmEntity.objects", fake)
        EntityContextService._class_distribution([1], "2025_12_31", None)
        filter_kwargs = [c[1] for c in fake.qs.calls if c[0] == "filter"]
        assert len(filter_kwargs) == 1  # no country_code filter

    def test_fail_soft_on_error(self, monkeypatch):
        monkeypatch.setattr("worldkg_nca.models.OsmEntity.objects", object())
        assert EntityContextService._class_distribution([1], "2025_12_31", "BZ") == []
