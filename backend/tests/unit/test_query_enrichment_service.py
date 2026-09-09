"""Unit tests for LLM-driven answer enrichment.

The LLM is stubbed throughout — no Ollama/network required. Verifies:
  - fail-soft behavior (LLM down / no results / synthesis fail)
  - tool decision validation against the research-tool set (enrich())
  - the FORCED default research action when the LLM selects nothing
  - deterministic tool execution + the enrich() contract (agent path)
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

    def test_synthesis_receives_primary_digest(self, monkeypatch):
        from semantic_search.services.query_enrichment_service import (
            QueryEnrichmentService,
        )
        monkeypatch.setattr(
            QueryEnrichmentService, "_call_search_tool",
            classmethod(lambda cls, *a, **k: []),  # research returns NOTHING
        )
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
        # The primary digest grounds the answer even with empty research.
        assert "Primary entities" in user_msg
        assert "wkgs:Cafe (2)" in user_msg
        assert "Cafe A" in user_msg


# ── Streaming events ─────────────────────────────────────────────────────

class TestEvents:
    def test_events_emitted_in_order(self, monkeypatch):
        from semantic_search.services.query_enrichment_service import (
            QueryEnrichmentService,
        )
        monkeypatch.setattr(
            QueryEnrichmentService, "_call_search_tool",
            classmethod(lambda cls, *a, **k: [{"name": "Cafe A"}]),
        )

        class _StreamingStub(_StubLLM):
            def chat_stream(self, messages, *args, **kwargs):
                yield "enriched answer"

        events = []
        _patch_llm(monkeypatch, _StreamingStub(
            selection={"tools": [
                {"tool": "nameSearch", "args": {"naturalQuery": "Cafe A"}},
            ]},
            synthesized="ignored — streaming used",
        ))
        QueryEnrichmentService.enrich(
            "q", "T (#1)", [], _results(), "BZ", None, events.append,
        )
        names = [e["event"] for e in events]
        assert names == ["research", "research_out", "answer_delta"]
        assert events[0]["tool"] == "nameSearch"
        assert events[1]["output"] == [{"name": "Cafe A"}]
        assert events[2]["delta"] == "enriched answer"

    def test_stream_synthesis_uses_chat_stream(self, monkeypatch):
        """With a callback, synthesis streams token deltas via chat_stream."""
        from semantic_search.services.query_enrichment_service import (
            QueryEnrichmentService,
        )
        monkeypatch.setattr(
            QueryEnrichmentService, "_call_search_tool",
            classmethod(lambda cls, *a, **k: [{"name": "Cafe A"}]),
        )

        class _StreamingStub(_StubLLM):
            def chat_stream(self, messages, *args, **kwargs):
                for tok in ["18", " entities", " found"]:
                    yield tok

        events = []
        _patch_llm(monkeypatch, _StreamingStub(
            selection={"tools": [
                {"tool": "nameSearch", "args": {"naturalQuery": "Cafe A"}},
            ]},
            synthesized="ignored — streaming used",
        ))
        out = QueryEnrichmentService.enrich(
            "q", "T (#1)", [], _results(), "BZ", None, events.append,
        )
        deltas = [e["delta"] for e in events if e["event"] == "answer_delta"]
        assert "".join(deltas) == "18 entities found"
        assert out["enriched_answer"] == "18 entities found"


# ── Answer cache ─────────────────────────────────────────────────────────

class TestCache:
    def test_second_call_hits_cache(self, monkeypatch):
        from semantic_search.services.query_enrichment_service import (
            QueryEnrichmentService,
        )
        monkeypatch.setattr(
            QueryEnrichmentService, "_call_search_tool",
            classmethod(lambda cls, *a, **k: [{"name": "Cafe A"}]),
        )
        stub = _StubLLM(
            selection={"tools": [
                {"tool": "nameSearch", "args": {"naturalQuery": "Cafe A"}},
            ]},
            synthesized="cached answer",
        )
        _patch_llm(monkeypatch, stub)

        first = QueryEnrichmentService.enrich("q", "T (#1)", [], _results(), "BZ")
        assert first is not None
        assert stub.chat_json_calls == 1

        # Same question + country → cache hit, no LLM calls.
        second = QueryEnrichmentService.enrich("q", "T (#1)", [], _results(), "BZ")
        assert second == first
        assert stub.chat_json_calls == 1
        assert len(stub.chat_calls) == 1

    def test_cache_replays_events(self, monkeypatch):
        from semantic_search.services.query_enrichment_service import (
            QueryEnrichmentService,
        )
        monkeypatch.setattr(
            QueryEnrichmentService, "_call_search_tool",
            classmethod(lambda cls, *a, **k: [{"name": "Cafe A"}]),
        )
        stub = _StubLLM(
            selection={"tools": [
                {"tool": "nameSearch", "args": {"naturalQuery": "Cafe A"}},
            ]},
            synthesized="cached answer",
        )
        _patch_llm(monkeypatch, stub)

        QueryEnrichmentService.enrich("q", "T (#1)", [], _results(), "BZ")
        events = []
        QueryEnrichmentService.enrich("q", "T (#1)", [], _results(), "BZ", None, events.append)
        names = [e["event"] for e in events]
        assert "research" in names
        assert "answer_delta" in names
        # Cache hit → no new LLM calls.
        assert stub.chat_json_calls == 1

    def test_cache_keyed_by_country(self, monkeypatch):
        from semantic_search.services.query_enrichment_service import (
            QueryEnrichmentService,
        )
        monkeypatch.setattr(
            QueryEnrichmentService, "_call_search_tool",
            classmethod(lambda cls, *a, **k: [{"name": "Cafe A"}]),
        )
        stub = _StubLLM(
            selection={"tools": [
                {"tool": "nameSearch", "args": {"naturalQuery": "Cafe A"}},
            ]},
            synthesized="answer",
        )
        _patch_llm(monkeypatch, stub)
        QueryEnrichmentService.enrich("q", "T (#1)", [], _results(), "BZ")
        QueryEnrichmentService.enrich("q", "T (#1)", [], _results(), "US")  # different key
        assert stub.chat_json_calls == 2


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
