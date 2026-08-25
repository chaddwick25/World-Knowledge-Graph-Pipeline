"""Unit tests for LLM-driven answer enrichment via tool research.

The LLM is stubbed throughout — no Ollama/network required. Verifies:
  - fail-soft behavior (LLM down / no results / synthesis fail)
  - tool decision validation against the research-tool set
  - the FORCED default research action when the LLM selects nothing
  - deterministic tool execution + the enrich() contract
"""

import json

import pytest

from semantic_search.services.query_enrichment_service import (
    QueryEnrichmentService,
)


# ── Helpers ──────────────────────────────────────────────────────────────

class FakeSearchResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

class _StubLLM:
    """chat_json controls the research selection; chat the synthesis."""

    def __init__(self, available=True, selection=None, synthesized="enriched!"):
        self.enabled = True
        self._available = available
        self._selection = selection
        self._synthesized = synthesized
        self.chat_calls = []

    def is_available(self):
        return self._available

    def chat_json(self, *args, **kwargs):
        return self._selection

    def chat(self, messages, *args, **kwargs):
        self.chat_calls.append(messages)
        return self._synthesized


def _patch_llm(monkeypatch, stub):
    monkeypatch.setattr(
        "core.services.llm_service.LLMService.get_instance", lambda: stub,
    )


def _results():
    return [
        {"name": "Cafe A", "wkg_class": "wkgs:Cafe", "tags": {"amenity": "cafe"}, "distance_m": 100.0},
        {"name": "Cafe B", "wkg_class": "wkgs:Cafe", "tags": {"amenity": "cafe"}, "distance_m": 500.0},
        {"name": "Bank X", "wkg_class": "wkgs:Bank", "tags": {"amenity": "bank"}, "distance_m": 2000.0},
        {"name": None, "wkg_class": "wkgs:Restaurant", "tags": {"amenity": "restaurant"}},
    ]


# ── Fail-soft ────────────────────────────────────────────────────────────

class TestFailSoft:
    def test_no_results_returns_none(self, monkeypatch):
        def boom(*a, **k):
            raise AssertionError("LLM must not be called with no results")

        _patch_llm(monkeypatch, boom)
        assert QueryEnrichmentService.enrich("q", "T (#1)", [], [], "BZ") is None

    def test_dict_results_returns_none(self, monkeypatch):
        _patch_llm(monkeypatch, _StubLLM())
        assert QueryEnrichmentService.enrich(
            "q", "T (#11)", [], {"error": "no data"}, "BZ",
        ) is None

    def test_llm_unavailable_returns_none(self, monkeypatch):
        _patch_llm(monkeypatch, _StubLLM(available=False))
        assert QueryEnrichmentService.enrich(
            "q", "T (#1)", [], _results(), "BZ",
        ) is None

    def test_synthesis_failure_returns_none(self, monkeypatch):
        _patch_llm(monkeypatch, _StubLLM(
            selection={"tools": [{"tool": "nameSearch",
                                  "args": {"naturalQuery": "Cafe A"}}]},
            synthesized="   ",
        ))
        assert QueryEnrichmentService.enrich(
            "q", "T (#1)", [], _results(), "BZ",
        ) is None

    def test_no_derivable_default_returns_none(self, monkeypatch):
        # No country_code AND no derivable default action → keep primary.
        _patch_llm(monkeypatch, _StubLLM(selection={}))
        assert QueryEnrichmentService.enrich(
            "q", "T (#1)", [], _results(), None,
        ) is None


# ── Tool decision validation ─────────────────────────────────────────────

class TestToolDecision:
    def test_valid_selection(self):
        out = QueryEnrichmentService._validate_tool_decision({
            "tools": [
                {"tool": "nameSearch", "args": {"naturalQuery": "Cafe A", "topK": 5}},
                {"tool": "structuredSearch", "args": {"queryTags": {"amenity": "cafe"}}},
            ],
        })
        assert out == [
            ("nameSearch", {"naturalQuery": "Cafe A", "topK": 5}),
            ("structuredSearch", {"queryTags": {"amenity": "cafe"}}),
        ]

    def test_invalid_tools_filtered(self):
        out = QueryEnrichmentService._validate_tool_decision({
            "tools": [
                {"tool": "delete-everything", "args": {}},
                {"tool": "structuredSearch", "args": {"queryTags": {"amenity": "cafe"}}},
            ],
        })
        assert out == [("structuredSearch", {"queryTags": {"amenity": "cafe"}})]

    def test_missing_required_args_rejected(self):
        assert QueryEnrichmentService._validate_tool_decision({
            "tools": [
                {"tool": "nameSearch", "args": {}},               # no naturalQuery
                {"tool": "structuredSearch", "args": {}},         # no queryTags
            ],
        }) == []

    def test_capped_at_two(self):
        out = QueryEnrichmentService._validate_tool_decision({
            "tools": [
                {"tool": "nameSearch", "args": {"naturalQuery": "a"}},
                {"tool": "structuredSearch", "args": {"queryTags": {"a": "b"}}},
                {"tool": "nameSearch", "args": {"naturalQuery": "c"}},
            ],
        })
        assert len(out) == 2

    def test_non_dict_returns_empty(self):
        assert QueryEnrichmentService._validate_tool_decision([]) == []
        assert QueryEnrichmentService._validate_tool_decision(
            {"tools": "nameSearch"},
        ) == []


# ── Forced default research action ───────────────────────────────────────

class TestDefaultAction:
    def test_dominant_amenity_yields_structured_search(self):
        out = QueryEnrichmentService._default_research_action(_results(), "BZ")
        assert out == ("structuredSearch", {
            "countryCode": "BZ",
            "queryTags": {"amenity": "cafe"},
            "topK": 5,
        })

    def test_top_named_entity_yields_name_search(self):
        # No amenity-asserting keys → falls back to the top named entity.
        results = [{"tags": {"building": "yes"}}, {"name": "Patty's Pastries"}]
        out = QueryEnrichmentService._default_research_action(results, "BZ")
        assert out == ("nameSearch", {
            "countryCode": "BZ",
            "naturalQuery": "Patty's Pastries",
            "topK": 5,
        })

    def test_no_country_returns_none(self):
        assert QueryEnrichmentService._default_research_action(_results(), None) is None

    def test_no_amenity_no_name_returns_none(self):
        assert QueryEnrichmentService._default_research_action(
            [{"tags": {"building": "yes"}}], "BZ",
        ) is None


# ── enrich() contract ────────────────────────────────────────────────────

class TestEnrich:
    """Contract tests mock the search execution — hermetic, no DB."""

    @staticmethod
    def _canned_search(monkeypatch, output):
        monkeypatch.setattr(
            QueryEnrichmentService, "_call_search_tool",
            classmethod(lambda cls, *a, **k: output),
        )

    def test_llm_selected_research_used(self, monkeypatch):
        self._canned_search(monkeypatch, [
            {"name": "Cafe A", "wkg_class": "wkgs:Cafe", "lat": 16.85, "lon": -88.28},
        ])
        stub = _StubLLM(
            selection={"tools": [
                {"tool": "nameSearch", "args": {"naturalQuery": "Cafe A", "topK": 3}},
            ]},
            synthesized="18 entities within 50km — mostly cafes, with Cafe A closest.",
        )
        _patch_llm(monkeypatch, stub)
        out = QueryEnrichmentService.enrich(
            "Which cafes are within 50km of Belize City?",
            "FILTER-AGGREGATE-MEASURE (#1)",
            [{"type": "AMOUNT", "text": "50km"}],
            _results(), "BZ",
        )
        assert out is not None
        assert out["primary_answer"] == "Found 4 entities within 50km."
        assert out["actions"] == ["nameSearch"]
        assert out["action_outputs"]["nameSearch"][0]["name"] == "Cafe A"
        assert "cafes" in out["enriched_answer"]

    def test_forces_default_when_llm_selects_nothing(self, monkeypatch):
        """No valid LLM selection → structuredSearch on the dominant amenity
        must still run (research is forced, never skipped)."""
        self._canned_search(monkeypatch, [{"name": "Cafe A"}])
        stub = _StubLLM(selection={}, synthesized="enriched answer")
        _patch_llm(monkeypatch, stub)
        out = QueryEnrichmentService.enrich(
            "Which cafes are within 50km of Belize City?",
            "FILTER-AGGREGATE-MEASURE (#1)",
            [{"type": "AMOUNT", "text": "50km"}],
            _results(), "BZ",
        )
        assert out is not None
        assert out["actions"] == ["structuredSearch"]
        assert out["action_outputs"]["structuredSearch"] == [{"name": "Cafe A"}]

    def test_synthesis_receives_primary_and_research(self, monkeypatch):
        self._canned_search(monkeypatch, [{"name": "Cafe A"}])
        stub = _StubLLM(
            selection={"tools": [
                {"tool": "structuredSearch", "args": {"queryTags": {"amenity": "cafe"}}},
            ]},
            synthesized="ok",
        )
        _patch_llm(monkeypatch, stub)
        QueryEnrichmentService.enrich(
            "q", "T (#1)", [], _results(), "BZ",
        )
        user_msg = stub.chat_calls[0][-1]["content"]
        assert "Found 4 entities" in user_msg
        assert "structuredSearch" in user_msg
        assert "Cafe A" in user_msg  # research output fed into synthesis


# ── Search tool payload construction (fake client — no DB) ───────────────

class TestCallSearchTool:
    class _FakeClient:
        def __init__(self):
            self.calls = []

        def post(self, path, data=None, content_type=None, **kwargs):
            self.calls.append((path, data, content_type))
            return FakeSearchResponse({"results": [
                {"osm_id": 1, "osm_type": "node", "tags": {"name": "Cafe A"},
                 "wkg_class": "wkgs:Cafe",
                 "geom": {"lat": 16.85, "lon": -88.28},
                 "scores": {"final_score": 4.5}},
            ]})

    def test_name_search_payload(self, monkeypatch):
        fake = self._FakeClient()
        monkeypatch.setattr("django.test.Client", lambda: fake)
        out = QueryEnrichmentService._call_search_tool(
            "nameSearch",
            {"countryCode": "BZ", "naturalQuery": "Cafe A", "topK": 3},
            _results(), "BZ", None,
        )
        path, data, _ = fake.calls[0]
        payload = json.loads(data)
        assert path == "/api/nca/semantic-triplet-search/"
        assert payload["natural_query"] == "Cafe A"
        assert payload["top_k"] == 3
        assert payload["country_code"] == "BZ"
        assert out[0]["name"] == "Cafe A"
        assert out[0]["lat"] == 16.85

    def test_structured_search_payload(self, monkeypatch):
        fake = self._FakeClient()
        monkeypatch.setattr("django.test.Client", lambda: fake)
        out = QueryEnrichmentService._call_search_tool(
            "structuredSearch",
            {"countryCode": "BZ", "queryTags": {"amenity": "cafe"}, "topK": 5},
            _results(), "BZ", "2025_12_31",
        )
        _, data, _ = fake.calls[0]
        payload = json.loads(data)
        assert payload["query_tags"] == {"amenity": "cafe"}
        assert payload["snapshot_date"] == "2025_12_31"
        assert out[0]["wkg_class"] == "wkgs:Cafe"

    def test_no_country_returns_none(self, monkeypatch):
        def boom(*a, **k):
            raise AssertionError("must not call search without a country")

        monkeypatch.setattr("django.test.Client", boom)
        assert QueryEnrichmentService._call_search_tool(
            "nameSearch", {"naturalQuery": "x"}, _results(), None, None,
        ) is None

    def test_unknown_tool_returns_none(self):
        assert QueryEnrichmentService._call_search_tool(
            "delete-everything", {}, _results(), "BZ", None,
        ) is None
