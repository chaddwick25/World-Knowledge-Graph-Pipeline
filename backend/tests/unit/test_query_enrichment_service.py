"""Unit tests for LLM-driven answer enrichment.

The LLM is stubbed throughout — no Ollama/network required. Verifies:
  - tool decision validation against the research-tool set
  - deterministic tool execution (_call_search_tool, shared with the
    research orchestrator's follow-up tools)
  - synthesize() — direct-path, single-LLM-call synthesis from
    pre-fetched entity context (no tool selection)
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
        self.chat_json_calls = 0

    def is_available(self):
        return self._available

    def chat_json(self, *args, **kwargs):
        self.chat_json_calls += 1
        return self._selection

    def chat(self, messages, *args, **kwargs):
        self.chat_calls.append(messages)
        return self._synthesized


def _patch_llm(monkeypatch, stub):
    monkeypatch.setattr(
        "core.services.llm_service.LLMService.get_instance", lambda: stub,
    )


@pytest.fixture(autouse=True)
def _clear_enrichment_cache():
    """The module-level TTL cache must not leak between tests."""
    from semantic_search.services.query_enrichment_service import _CACHE
    _CACHE.clear()
    yield
    _CACHE.clear()


def _results():
    return [
        {"name": "Cafe A", "wkg_class": "wkgs:Cafe", "tags": {"amenity": "cafe"}, "distance_m": 100.0},
        {"name": "Cafe B", "wkg_class": "wkgs:Cafe", "tags": {"amenity": "cafe"}, "distance_m": 500.0},
        {"name": "Bank X", "wkg_class": "wkgs:Bank", "tags": {"amenity": "bank"}, "distance_m": 2000.0},
        {"name": None, "wkg_class": "wkgs:Restaurant", "tags": {"amenity": "restaurant"}},
    ]


# ── Tool call validation (native tool_calls + JSON-schema args) ──────────

class TestToolCallValidation:
    def test_valid_selection(self):
        out = QueryEnrichmentService._validate_tool_calls([
            {"name": "nameSearch",
             "arguments": {"naturalQuery": "Cafe A", "topK": 5}},
            {"name": "structuredSearch",
             "arguments": {"queryTags": {"amenity": "cafe"}}},
        ])
        assert out == [
            ("nameSearch", {"naturalQuery": "Cafe A", "topK": 5}),
            ("structuredSearch", {"queryTags": {"amenity": "cafe"}}),
        ]

    def test_invalid_tools_filtered(self):
        out = QueryEnrichmentService._validate_tool_calls([
            {"name": "delete-everything", "arguments": {}},
            {"name": "structuredSearch",
             "arguments": {"queryTags": {"amenity": "cafe"}}},
        ])
        assert out == [("structuredSearch", {"queryTags": {"amenity": "cafe"}})]

    def test_missing_required_args_rejected(self):
        assert QueryEnrichmentService._validate_tool_calls([
            {"name": "nameSearch", "arguments": {}},        # no naturalQuery
            {"name": "structuredSearch", "arguments": {}},  # no queryTags
        ]) == []

    def test_wrong_type_rejected(self):
        assert QueryEnrichmentService._validate_tool_calls([
            {"name": "nameSearch", "arguments": {"naturalQuery": 42}},
        ]) == []

    def test_unknown_props_dropped(self):
        out = QueryEnrichmentService._validate_tool_calls([
            {"name": "nameSearch",
             "arguments": {"naturalQuery": "Cafe A", "dropMe": True}},
        ])
        assert out == [("nameSearch", {"naturalQuery": "Cafe A"})]

    def test_capped_at_two(self):
        out = QueryEnrichmentService._validate_tool_calls([
            {"name": "nameSearch", "arguments": {"naturalQuery": "a"}},
            {"name": "structuredSearch", "arguments": {"queryTags": {"a": "b"}}},
            {"name": "nameSearch", "arguments": {"naturalQuery": "c"}},
        ])
        assert len(out) == 2

    def test_non_list_returns_empty(self):
        assert QueryEnrichmentService._validate_tool_calls(None) == []
        assert QueryEnrichmentService._validate_tool_calls("nameSearch") == []

    def test_tools_schema_shape(self):
        schema = QueryEnrichmentService._research_tools_schema()
        assert [t["function"]["name"] for t in schema] == [
            "nameSearch", "structuredSearch",
        ]
        for entry in schema:
            fn = entry["function"]
            assert entry["type"] == "function"
            assert fn["description"]
            assert fn["parameters"]["type"] == "object"
            assert fn["parameters"]["required"]


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


# ── Primary digest ───────────────────────────────────────────────────────

class TestPrimaryDigest:
    def test_class_counts_and_top_names(self):
        digest = QueryEnrichmentService._primary_digest(_results())
        assert "wkgs:Cafe (2)" in digest
        assert "wkgs:Bank (1)" in digest
        # Nearest named first; the unnamed restaurant is skipped.
        assert digest.index("Cafe A (100m)") < digest.index("Cafe B (500m)")
        assert "Cafe A" in digest and "Bank X" in digest

    def test_unnamed_results_only_show_classes(self):
        digest = QueryEnrichmentService._primary_digest(
            [{"wkg_class": "wkgs:Shop", "tags": {"shop": "bakery"}}],
        )
        assert "wkgs:Shop (1)" in digest
        assert "-" not in digest.replace("classes: ", "")

# ── synthesize() — direct-path context-based synthesis ───────────────────

class TestSynthesize:
    """synthesize() takes pre-fetched context — one LLM call, no selection."""

    @staticmethod
    def _context():
        return {
            "uslp_links": [{"head_osm_id": 1, "relation": "within_50m_of",
                            "tail_osm_id": 9, "normalized_score": 0.9}],
            "communities": {1: {"community": 3, "degree": 12}},
            "class_distribution": [{"wkg_class": "wkgs:Cafe", "count": 2}],
        }

    def test_synthesize_returns_enrichment_dict(self, monkeypatch):
        stub = _StubLLM(available=True, synthesized="18 cafes cluster into 3 communities.")
        _patch_llm(monkeypatch, stub)
        out = QueryEnrichmentService.synthesize(
            "Which cafes are within 50km of Belize City?",
            "FILTER-AGGREGATE-MEASURE (#1)",
            [{"type": "AMOUNT", "text": "50km"}],
            _results(), self._context(), "BZ",
        )
        assert out is not None
        assert out["enriched_answer"] == "18 cafes cluster into 3 communities."
        assert out["primary_answer"] == "Found 4 entities within 50km."
        assert set(out["actions"]) == {"uslp_links", "communities", "class_distribution"}
        assert out["action_outputs"]["communities"] == {1: {"community": 3, "degree": 12}}

    def test_prompt_consumes_context(self, monkeypatch):
        stub = _StubLLM(synthesized="ok")
        _patch_llm(monkeypatch, stub)
        QueryEnrichmentService.synthesize(
            "q", "FILTER-AGGREGATE-MEASURE (#1)", [], _results(),
            self._context(), "BZ",
        )
        user_msg = stub.chat_calls[0][-1]["content"]
        assert "Entity context" in user_msg
        assert "within_50m_of" in user_msg
        assert "wkgs:Cafe" in user_msg
        # No research-tool section on the direct path.
        assert "Research tool outputs" not in user_msg

    def test_no_results_returns_none(self, monkeypatch):
        def boom(*a, **k):
            raise AssertionError("LLM must not be called with no results")

        _patch_llm(monkeypatch, boom)
        assert QueryEnrichmentService.synthesize(
            "q", "T (#1)", [], [], self._context(), "BZ",
        ) is None

    def test_dict_results_returns_none(self, monkeypatch):
        _patch_llm(monkeypatch, _StubLLM())
        assert QueryEnrichmentService.synthesize(
            "q", "T (#11)", [], {"error": "no data"}, self._context(), "BZ",
        ) is None

    def test_llm_unavailable_returns_none(self, monkeypatch):
        _patch_llm(monkeypatch, _StubLLM(available=False))
        assert QueryEnrichmentService.synthesize(
            "q", "T (#1)", [], _results(), self._context(), "BZ",
        ) is None

    def test_empty_synthesis_returns_none(self, monkeypatch):
        _patch_llm(monkeypatch, _StubLLM(synthesized="   "))
        assert QueryEnrichmentService.synthesize(
            "q", "T (#1)", [], _results(), self._context(), "BZ",
        ) is None

    def test_empty_context_is_omitted_from_actions(self, monkeypatch):
        stub = _StubLLM(synthesized="answer")
        _patch_llm(monkeypatch, stub)
        out = QueryEnrichmentService.synthesize(
            "q", "T (#1)", [], _results(),
            {"uslp_links": [], "communities": {}, "class_distribution": []}, "BZ",
        )
        assert out is not None
        assert out["actions"] == []
        assert out["action_outputs"] == {}

    def test_streams_answer_deltas(self, monkeypatch):
        class _StreamingStub(_StubLLM):
            def chat_stream(self, messages, *args, **kwargs):
                for tok in ["18", " cafes", " found"]:
                    yield tok

        events = []
        _patch_llm(monkeypatch, _StreamingStub())
        out = QueryEnrichmentService.synthesize(
            "q", "T (#1)", [], _results(), self._context(), "BZ",
            None, events.append,
        )
        deltas = [e["delta"] for e in events if e["event"] == "answer_delta"]
        assert "".join(deltas) == "18 cafes found"
        assert out["enriched_answer"] == "18 cafes found"

    def test_second_call_hits_cache(self, monkeypatch):
        stub = _StubLLM(synthesized="cached synthesis")
        _patch_llm(monkeypatch, stub)
        first = QueryEnrichmentService.synthesize(
            "q", "T (#1)", [], _results(), self._context(), "BZ",
        )
        assert first is not None
        assert len(stub.chat_calls) == 1

        second = QueryEnrichmentService.synthesize(
            "q", "T (#1)", [], _results(), self._context(), "BZ",
        )
        assert second == first
        assert len(stub.chat_calls) == 1  # cache hit → no new LLM call

    def test_cache_replays_answer_deltas(self, monkeypatch):
        stub = _StubLLM(synthesized="cached replay answer")
        _patch_llm(monkeypatch, stub)
        QueryEnrichmentService.synthesize(
            "q", "T (#1)", [], _results(), self._context(), "BZ",
        )
        events = []
        QueryEnrichmentService.synthesize(
            "q", "T (#1)", [], _results(), self._context(), "BZ",
            None, events.append,
        )
        assert [e["event"] for e in events] == ["answer_delta"]
        assert "".join(e["delta"] for e in events) == "cached replay answer"
        assert len(stub.chat_calls) == 1

    def test_cache_key_versioned_and_namespaced(self):
        import hashlib
        from semantic_search.services.query_enrichment_service import _cache_key

        # v2 version bump: the key is md5("v2|" + old raw string).
        assert _cache_key("q", "T (#1)", "BZ", "2025_12_31") == hashlib.md5(
            "v2|q|T (#1)|BZ|2025_12_31".encode("utf-8"),
        ).hexdigest()
        # enrich() and synthesize() never share an entry for the same inputs.
        assert _cache_key("q", "T (#1)", "BZ", "2025_12_31",
                          namespace="enrich") != _cache_key(
            "q", "T (#1)", "BZ", "2025_12_31", namespace="synthesize",
        )


# ── Context prompt formatting (prompt-size regression) ───────────────────

class TestContextFormatting:
    """The synthesis prompt must consume a compact context digest.

    Regression: the raw context dict (50 USLP links + per-entity community
    rows ≈ 1.5K tokens) inflated the LLM synthesis to 50-115s on fresh
    queries. The prompt gets relation/community histograms instead.
    """

    @staticmethod
    def _big_context():
        return {
            "uslp_links": [
                {"head_osm_id": 1, "relation": "within_50m_of",
                 "tail_osm_id": 9, "normalized_score": 0.9},
            ] * 32,  # 32 links — the pre-fix cap
            "communities": {
                1: {"community": 3, "fiedler": 0.008, "degree": 53,
                    "component_size": 35541, "subgraph_slug": None},
                2: {"community": 5, "fiedler": 0.009, "degree": 12,
                    "component_size": 35541, "subgraph_slug": None},
                3: {"community": 3, "fiedler": 0.007, "degree": 20,
                    "component_size": 35541, "subgraph_slug": None},
            },
            "class_distribution": [{"wkg_class": "wkgs:Cafe", "count": 2}],
        }

    def test_compact_histogram_format(self):
        out = QueryEnrichmentService._format_context_for_prompt(
            self._big_context(),
        )
        # Relation histogram, not 32 raw JSON rows.
        assert "USLP links: 32 total" in out
        assert "within_50m_of × 32" in out
        assert out.count("--within_50m_of->") == 8  # capped examples
        # Community histogram, not per-entity rows.
        assert "Communities: 3 entities" in out
        assert "community 3 × 2" in out
        assert "community 5 × 1" in out
        # Class mix.
        assert "Classes: wkgs:Cafe × 2" in out
        # Compact: a fraction of the raw ~4KB payload.
        assert len(out) < 800

    def test_empty_context(self):
        out = QueryEnrichmentService._format_context_for_prompt(
            {"uslp_links": [], "communities": {}, "class_distribution": []},
        )
        assert "USLP links: none" in out
        assert "Communities: none" in out
        assert "Classes: none" in out

    def test_unfetched_sources_render_not_applicable(self):
        """Template #5 (bearing) fetches only classes — the prompt must
        not claim USLP/community data is absent, only not applicable."""
        out = QueryEnrichmentService._format_context_for_prompt(
            {"uslp_links": [], "communities": {},
             "class_distribution": [{"wkg_class": "wkgs:Bar", "count": 1}]},
            sources={"classes"},
        )
        assert "USLP links: (not applicable to this template)" in out
        assert "Communities: (not applicable to this template)" in out
        assert "Classes: wkgs:Bar × 1" in out
        assert "USLP links: none" not in out

    def test_template5_prompt_marks_unfetched_sources(self, monkeypatch):
        stub = _StubLLM(synthesized="ok")
        _patch_llm(monkeypatch, stub)
        QueryEnrichmentService.synthesize(
            "What is west of Tullygally Tavern?",
            "LOCATION-BEARING-CLASSIFY (#5)", [],
            [{"osm_id": 1, "name": "St Vincent de Paul"}],
            {"uslp_links": [], "communities": {},
             "class_distribution": [{"wkg_class": "wkgs:Amenity", "count": 1}]},
            "IE",
        )
        user_msg = stub.chat_calls[0][-1]["content"]
        assert "not applicable to this template" in user_msg
        assert "USLP links: none" not in user_msg

    def test_prompt_uses_compact_format(self, monkeypatch):
        stub = _StubLLM(synthesized="ok")
        _patch_llm(monkeypatch, stub)
        QueryEnrichmentService.synthesize(
            "q", "FILTER-AGGREGATE-MEASURE (#1)", [], _results(),
            self._big_context(), "BZ",
        )
        user_msg = stub.chat_calls[0][-1]["content"]
        assert "USLP links: 32 total" in user_msg
        # No raw JSON dump of the links array in the prompt.
        assert '"head_osm_id": 1, "relation"' not in user_msg


# ── Executor-level guard: no entity context → no enrichment ──────────────


class TestExecuteEnrichmentGuard:
    """QueryExecutorService.execute must skip the direct-path enrichment
    when the results carry no osm_id — there is no deterministic entity
    context to ground the LLM on, and empty-context synthesis produced
    answers contradicting the deterministic one ("distance not provided"
    vs "Distance: 0.44 km")."""

    def _execute(self, monkeypatch, template, results, executor_name):
        from semantic_search.services.query_executor_service import (
            QueryExecutorService,
        )

        # Patch the executor dispatch table directly — `_executors` is
        # class-cached, so patching the method attribute alone is ignored
        # once `_get_executors()` has been called by an earlier test.
        # Plain callable (not classmethod): the dispatch invokes it as
        # executor(concepts, cc, snap, trace, question=...).
        monkeypatch.setattr(
            QueryExecutorService, "_executors",
            {template: (lambda *a, _r=results, **k: _r)},
        )

        def should_not_be_called(*a, **k):
            raise AssertionError("enrichment must not run without entity context")

        monkeypatch.setattr(
            "semantic_search.services.entity_context_service."
            "EntityContextService.get_context",
            should_not_be_called,
        )
        monkeypatch.setattr(
            "semantic_search.services.query_enrichment_service."
            "QueryEnrichmentService.synthesize",
            should_not_be_called,
        )

        parsed = {"template": template, "concepts": []}
        return QueryExecutorService.execute(
            parsed, "IE", None, question="How far is A from B?",
        )

    def test_distance_result_no_osm_id_skips_enrichment(self, monkeypatch):
        out = self._execute(
            monkeypatch, "OBJECT-FIELD-MEASURE (#2)",
            [{"distance_km": 0.44, "distance_m": 440.4,
              "from": "A", "to": "B"}],
            "_execute_object_field_measure",
        )
        assert out["enrichment"] is None
        assert out["results"][0]["distance_m"] == 440.4

    def test_bearing_result_no_osm_id_skips_enrichment(self, monkeypatch):
        out = self._execute(
            monkeypatch, "LOCATION-BEARING-CLASSIFY (#5)",
            [{"bearing_degrees": 90.0, "direction": "E",
              "from": "A", "to": "B"}],
            "_execute_location_bearing_classify",
        )
        assert out["enrichment"] is None

    def test_entity_result_with_osm_id_runs_enrichment(self, monkeypatch):
        from semantic_search.services.query_executor_service import (
            QueryExecutorService,
        )
        from unittest.mock import MagicMock

        results = [{"osm_id": 1, "name": "A", "lat": 1.0, "lon": 2.0}]
        monkeypatch.setattr(
            QueryExecutorService, "_executors",
            {"PLACE-ATTRIBUTE-QUERY (#8)":
             (lambda *a, _r=results, **k: _r)},
        )
        # Stubbed context + a stubbed synthesis — hermetic, no DB.
        monkeypatch.setattr(
            "semantic_search.services.entity_context_service."
            "EntityContextService.get_context",
            classmethod(
                lambda cls, *a, **k: {
                    "uslp_links": [], "communities": {},
                    "class_distribution": [],
                }
            ),
        )
        monkeypatch.setattr(
            "semantic_search.services.query_enrichment_service."
            "QueryEnrichmentService.synthesize",
            classmethod(
                lambda cls, *a, **k: {
                    "enriched_answer": "A is near the anchor.",
                    "primary_answer": "Found 1 place.",
                }
            ),
        )
        parsed = {"template": "PLACE-ATTRIBUTE-QUERY (#8)", "concepts": []}
        out = QueryExecutorService.execute(
            parsed, "IE", None, question="What is near A?",
        )
        assert out["enrichment"] is not None
        assert out["enrichment"]["enriched_answer"] == "A is near the anchor."
