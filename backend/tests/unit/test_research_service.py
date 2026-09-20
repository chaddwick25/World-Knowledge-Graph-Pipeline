"""Unit tests for the research orchestrator (RESEARCH_ORCHESTRATOR_MVP_PLAN.md).

Covers:
  - LLMService constructor overrides + get_research_instance() (RESEARCH_LLM_*)
  - ResearchOrchestratorService: decompose JSON validation, placeholder
    substitution, top-entity selection, the plan() loop (mocked LLM + mocked
    executor): event emission, skip_enrichment=True, per-question failure
    survival, LLM-unavailable fail-soft

All LLM HTTP calls are mocked — no Ollama/network required. No DB required
(executor and parser are patched).
"""

import pytest
from contextlib import contextmanager
from unittest import mock

from core.services.llm_service import LLMService
from semantic_search.services.research_service import (
    MAX_QUESTIONS,
    ResearchOrchestratorService,
    _DECOMPOSE_SYSTEM_PROMPT,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Every test starts with clean LLM_* and RESEARCH_LLM_* env."""
    LLMService.reset_instance()
    for var in (
        "LLM_BASE_URL", "LLM_MODEL", "LLM_ENABLED", "LLM_TIMEOUT",
        "LLM_API_STYLE", "LLM_AVAILABILITY_CACHE_SECONDS",
        "RESEARCH_LLM_BASE_URL", "RESEARCH_LLM_MODEL",
        "RESEARCH_LLM_TIMEOUT", "RESEARCH_LLM_API_STYLE",
    ):
        monkeypatch.delenv(var, raising=False)
    yield
    LLMService.reset_instance()


class FakeLLM:
    """Minimal stand-in for LLMService used by plan() tests."""

    def __init__(self, available=True, chat_json_result=None,
                 stream_parts=("summary part 1 ", "summary part 2")):
        self._available = available
        self._chat_json_result = chat_json_result
        self._stream_parts = stream_parts
        self.chat_json_calls = 0
        self.chat_calls = 0

    def is_available(self):
        return self._available

    def chat_json(self, *args, **kwargs):
        self.chat_json_calls += 1
        return self._chat_json_result

    def chat_stream(self, *args, **kwargs):
        for part in self._stream_parts:
            yield part

    def chat(self, *args, **kwargs):
        self.chat_calls += 1
        return "".join(self._stream_parts)


# ── LLMService: constructor overrides ────────────────────────────────────

class TestConstructorOverrides:
    def test_env_defaults_when_overrides_none(self):
        svc = LLMService()
        assert svc.style == "native"
        assert svc.base_url == "http://localhost:11434/v1"
        assert svc.model == "qwen3:8b"
        assert svc.timeout == 60.0

    def test_explicit_overrides_beat_env(self, monkeypatch):
        monkeypatch.setenv("LLM_BASE_URL", "http://ollama:11434/v1")
        svc = LLMService(
            base_url="http://other:8080/v1",
            model="qwen3:4b",
            style="openai",
            timeout=7.5,
        )
        assert svc.style == "openai"
        assert svc.base_url == "http://other:8080/v1"
        assert svc.root_url == "http://other:8080"
        assert svc.model == "qwen3:4b"
        assert svc.timeout == 7.5

    def test_partial_overrides_keep_env_defaults(self, monkeypatch):
        monkeypatch.setenv("LLM_MODEL", "qwen3:14b")
        svc = LLMService(model=None, timeout="9.5")
        assert svc.model == "qwen3:14b"
        assert svc.timeout == 9.5


# ── LLMService: get_research_instance ────────────────────────────────────

class TestResearchInstance:
    def test_unset_research_env_falls_back_to_platform(self):
        inst = LLMService.get_research_instance()
        assert inst is LLMService.get_instance()

    def test_research_env_creates_second_instance(self, monkeypatch):
        monkeypatch.setenv("RESEARCH_LLM_BASE_URL", "http://research:11435/v1")
        monkeypatch.setenv("RESEARCH_LLM_MODEL", "qwen3:4b")
        monkeypatch.setenv("RESEARCH_LLM_TIMEOUT", "120")
        inst = LLMService.get_research_instance()
        platform = LLMService.get_instance()
        assert inst is not platform
        assert inst.base_url == "http://research:11435/v1"
        assert inst.model == "qwen3:4b"
        assert inst.timeout == 120.0
        assert inst.style == "native"
        # The platform instance is untouched.
        assert platform.base_url == "http://localhost:11434/v1"


# ── Decompose JSON validation ────────────────────────────────────────────

class TestValidateQuestions:
    def test_valid_list(self):
        decision = {"questions": [
            {"question": "Which hotels are within 2km of the centre of Belize City?", "why": "lodging"},
            {"question": "Which cafes are within 1km of the top hotel?", "why": "meals"},
        ]}
        questions = ResearchOrchestratorService._validate_questions(decision)
        assert len(questions) == 2
        assert questions[0]["question"].startswith("Which hotels")
        assert questions[0]["why"] == "lodging"

    def test_non_dict_decision(self):
        assert ResearchOrchestratorService._validate_questions(None) == []
        assert ResearchOrchestratorService._validate_questions({"tools": []}) == []
        assert ResearchOrchestratorService._validate_questions([1, 2]) == []

    def test_short_and_empty_questions_dropped(self):
        decision = {"questions": [
            {"question": ""},
            {"question": "short"},
            {"question": "Which hotels are within 2km of Belize City?", "why": ""},
        ]}
        questions = ResearchOrchestratorService._validate_questions(decision)
        assert len(questions) == 1

    def test_capped_at_max_questions(self):
        decision = {"questions": [
            {"question": f"Which hotel {i} is near Belize City?"}
            for i in range(MAX_QUESTIONS + 5)
        ]}
        questions = ResearchOrchestratorService._validate_questions(decision)
        assert len(questions) == MAX_QUESTIONS


# ── Placeholder substitution + top entity ────────────────────────────────

class TestDecomposePrompt:
    def test_radius_guidance_present(self):
        # City-scale anchors must never get a 1km radius ("1 km is too
        # small to search Dublin" — observed 2026-09-19).
        assert "Never use 1 km for a city" in _DECOMPOSE_SYSTEM_PROMPT
        assert "at least 2 km" in _DECOMPOSE_SYSTEM_PROMPT


class TestPlaceholders:
    def test_top_entity_substituted(self):
        out = ResearchOrchestratorService._substitute_placeholders(
            "Which cafes are within 1km of the top hotel?", "Hilton Belize",
        )
        assert out == "Which cafes are within 1km of Hilton Belize?"

    def test_no_previous_entity_unchanged(self):
        out = ResearchOrchestratorService._substitute_placeholders(
            "Which cafes are within 1km of the top hotel?", None,
        )
        assert out == "Which cafes are within 1km of the top hotel?"

    def test_top_entity_selection(self):
        results = [
            {"name": "", "tags": {"name": "Fallback Cafe"}},
            {"name": "First Hotel", "tags": {"name": "Other"}},
        ]
        assert ResearchOrchestratorService._top_entity_name(results) == "Fallback Cafe"
        assert ResearchOrchestratorService._top_entity_name([]) is None

    def test_top_entity_excludes_anchor(self):
        results = [
            {"name": "belize city", "tags": {}},
            {"name": "Jovilee Apartments", "tags": {}},
        ]
        # The geocoded anchor ("belize city" node) must never become
        # 'the top hotel' — case-insensitive exclusion.
        assert ResearchOrchestratorService._top_entity_name(
            results, exclude="Belize City",
        ) == "Jovilee Apartments"

    def test_parse_radius_m(self):
        svc = ResearchOrchestratorService
        assert svc._parse_radius_m("within 1km of Dublin") == 1000
        assert svc._parse_radius_m("within 500m of Temple Bar") == 500
        assert svc._parse_radius_m("within 2 km of Dublin") == 2000
        assert svc._parse_radius_m("within 1.5km of Rome") == 1500
        assert svc._parse_radius_m("near Dublin") is None
        assert svc._parse_radius_m(None) is None

    def test_widen_radius(self):
        svc = ResearchOrchestratorService
        assert svc._widen_radius("Which hotels are within 1km of Dublin?", 2000) == \
            "Which hotels are within 2 km of Dublin?"
        assert svc._widen_radius("Which cafes are within 500m of X?", 2000) == \
            "Which cafes are within 2 km of X?"

    def test_anchor_geocode_failed_detection(self):
        assert ResearchOrchestratorService._anchor_geocode_failed({
            "trace": [{"step": "geocode", "output": None},
                      {"step": "spatial_filter_skipped"}],
        }) is True
        assert ResearchOrchestratorService._anchor_geocode_failed({
            "trace": [{"step": "geocode", "output": {"lat": 1}}],
        }) is False


# ── The plan() loop ──────────────────────────────────────────────────────

class TestPlan:
    @contextmanager
    def _patch_loop(self, fake_llm, execute_result=None):
        """Patch LLM + parser + executor for the duration of a plan() call.
        Yields the executor mock."""
        parser = mock.Mock()
        parser.get_instance.return_value.parse.return_value = {
            "template": "FILTER-AGGREGATE-MEASURE (#1)",
            "confidence": 0.9,
            "concepts": [],
        }
        executor = mock.Mock()
        if execute_result is None:
            execute_result = {
                "template": "FILTER-AGGREGATE-MEASURE (#1)",
                "results": [
                    {"name": "Hilton Belize", "osm_id": 1,
                     "wkg_class": "wkgs:Hotel", "distance_m": 500},
                ],
                "answer": "Found 1 entities within 2km.",
                "trace": [],
                "latency_ms": 10,
            }
        executor.execute.return_value = execute_result
        with mock.patch(
            "core.services.llm_service.LLMService.get_research_instance",
            return_value=fake_llm,
        ), mock.patch(
            "semantic_search.services.query_parser_service.QueryParserService",
            parser,
        ), mock.patch(
            "semantic_search.services.query_executor_service.QueryExecutorService",
            executor,
        ):
            yield executor

    def test_plan_full_loop(self):
        fake_llm = FakeLLM(chat_json_result={"questions": [
            {"question": "Which hotels are within 2km of the centre of Belize City?", "why": "lodging"},
            {"question": "Which cafes are within 1km of the top hotel?", "why": "meals"},
        ]})
        events = []
        with self._patch_loop(fake_llm) as executor:
            result = ResearchOrchestratorService.plan(
                "Plan a 2-day trip to Belize City", "BZ", None,
                event_callback=events.append,
            )

        assert len(result["questions"]) == 2
        assert result["questions"][1]["question"] == \
            "Which cafes are within 1km of Hilton Belize?"
        assert result["summary"] == "summary part 1 summary part 2"
        assert result["errors"] == []
        # Deterministic loop: no enrichment on sub-questions.
        assert executor.execute.call_args.kwargs["skip_enrichment"] is True

        event_names = [e["event"] for e in events]
        assert event_names[0] == "plan"
        assert event_names[1] == "question"
        assert event_names[2] == "question"
        assert "summary_delta" in event_names

    def test_plan_llm_unavailable_fail_soft(self):
        fake_llm = FakeLLM(available=False)
        with mock.patch(
            "core.services.llm_service.LLMService.get_research_instance",
            return_value=fake_llm,
        ):
            result = ResearchOrchestratorService.plan("Plan a trip", "BZ", None)
        assert result["questions"] == []
        assert result["summary"] is None
        assert result["errors"] == [{"error": "research LLM unavailable"}]

    def test_plan_no_questions_decomposed(self):
        fake_llm = FakeLLM(chat_json_result={"questions": []})
        with mock.patch(
            "core.services.llm_service.LLMService.get_research_instance",
            return_value=fake_llm,
        ):
            result = ResearchOrchestratorService.plan("Plan a trip", "BZ", None)
        assert result["errors"] == [
            {"error": "no parser-ready questions decomposed"},
        ]

    def test_plan_question_failure_keeps_loop_alive(self):
        fake_llm = FakeLLM(chat_json_result={"questions": [
            {"question": "Which hotels are within 2km of Belize City?", "why": ""},
            {"question": "Which cafes are within 1km of Belize City?", "why": ""},
        ]})
        execute_result = {
            "template": "FILTER-AGGREGATE-MEASURE (#1)",
            "results": [],
            "answer": "Found 0 entities within 2km.",
            "trace": [],
            "latency_ms": 5,
        }

        def flaky_execute(*args, **kwargs):
            question = kwargs.get("question", "")
            if "cafes" in question:
                raise RuntimeError("boom")
            return execute_result

        with self._patch_loop(fake_llm) as executor:
            executor.execute.side_effect = flaky_execute
            result = ResearchOrchestratorService.plan(
                "Plan a trip", "BZ", None,
            )

        assert len(result["questions"]) == 2
        assert result["questions"][0].get("error") is None
        assert result["questions"][1]["error"] == "boom"
        assert len(result["errors"]) == 1
        # Summary still produced from the surviving answer.
        assert result["summary"] == "summary part 1 summary part 2"

    def test_plan_anchor_qualifier_retry(self):
        fake_llm = FakeLLM(chat_json_result={"questions": [
            {"question": "Which hotels are within 2km of the centre of Belize City?", "why": ""},
        ]})
        calls = []

        def geo_execute(*args, **kwargs):
            q = kwargs.get("question", "")
            calls.append(q)
            if "centre of" in q:
                return {
                    "template": "FILTER-AGGREGATE-MEASURE (#1)",
                    "results": [], "answer": "No results found.",
                    "trace": [
                        {"step": "geocode", "input": "the centre of Belize City", "output": None},
                        {"step": "spatial_filter_skipped",
                         "warning": "anchor has no coordinates"},
                    ],
                    "latency_ms": 5,
                }
            return {
                "template": "FILTER-AGGREGATE-MEASURE (#1)",
                "results": [{"name": "Jovilee Apartments", "osm_id": 2,
                             "wkg_class": "wkgs:Tourism", "distance_m": 851}],
                "answer": "Found 1 entities within 2km.",
                "trace": [], "latency_ms": 5,
            }

        with self._patch_loop(fake_llm) as executor:
            executor.execute.side_effect = geo_execute
            result = ResearchOrchestratorService.plan("Plan a trip", "BZ", None)

        # First call geocodes null; the loop strips the qualifier and retries.
        assert calls == [
            "Which hotels are within 2km of the centre of Belize City?",
            "Which hotels are within 2km of Belize City?",
        ]
        assert result["questions"][0]["result_count"] == 1
        assert result["questions"][0]["question"] == \
            "Which hotels are within 2km of Belize City?"
        assert result["errors"] == []

    def test_plan_empty_result_does_not_wipe_placeholder(self):
        fake_llm = FakeLLM(chat_json_result={"questions": [
            {"question": "Which hotels are within 1km of Belize City?", "why": ""},
            {"question": "Which beaches are within 5km of Belize City?", "why": ""},
            {"question": "What amenities are near the top hotel?", "why": ""},
        ]})
        calls = []

        def exec_with_gaps(*args, **kwargs):
            q = kwargs.get("question", "")
            calls.append(q)
            if "hotels" in q:
                return {
                    "template": "FILTER-AGGREGATE-MEASURE (#1)",
                    "results": [{"name": "Jovilee Apartments", "osm_id": 2,
                                 "wkg_class": "wkgs:Tourism"}],
                    "answer": "Found 1 entities within 1km.",
                    "trace": [], "latency_ms": 1,
                }
            return {
                "template": "FILTER-AGGREGATE-MEASURE (#1)",
                "results": [], "answer": "No results found.",
                "trace": [], "latency_ms": 1,
            }

        with self._patch_loop(fake_llm) as executor:
            executor.execute.side_effect = exec_with_gaps
            result = ResearchOrchestratorService.plan("Plan a trip", "BZ", None)

        # The empty beaches question must not wipe 'Jovilee Apartments' —
        # the amenities question still resolves its placeholder.
        assert calls[2] == "What amenities are near Jovilee Apartments?"
        assert result["questions"][2]["question"] == \
            "What amenities are near Jovilee Apartments?"

    def test_plan_small_radius_empty_escalates(self):
        fake_llm = FakeLLM(chat_json_result={"questions": [
            {"question": "Which hotels are within 1km of Dublin?", "why": ""},
        ]})
        calls = []

        def radius_execute(*args, **kwargs):
            q = kwargs.get("question", "")
            calls.append(q)
            if "1km" in q:
                return {
                    "template": "FILTER-AGGREGATE-MEASURE (#1)",
                    "results": [], "answer": "No results found.",
                    "trace": [], "latency_ms": 5,
                }
            return {
                "template": "FILTER-AGGREGATE-MEASURE (#1)",
                "results": [{"name": "The Shelbourne", "osm_id": 9,
                             "wkg_class": "wkgs:Hotel", "distance_m": 1200}],
                "answer": "Found 1 entities within 2km.",
                "trace": [], "latency_ms": 5,
            }

        with self._patch_loop(fake_llm) as executor:
            executor.execute.side_effect = radius_execute
            result = ResearchOrchestratorService.plan("Plan a trip", "IE", None)

        # 1km of Dublin returns nothing → the loop widens to 2km and re-runs.
        assert calls == [
            "Which hotels are within 1km of Dublin?",
            "Which hotels are within 2 km of Dublin?",
        ]
        record = result["questions"][0]
        assert record["radius_escalated"] is True
        assert record["result_count"] == 1
        assert record["question"] == "Which hotels are within 2 km of Dublin?"

    def test_plan_small_radius_with_results_no_escalation(self):
        fake_llm = FakeLLM(chat_json_result={"questions": [
            {"question": "Which cafes are within 500m of Temple Bar?", "why": ""},
        ]})
        calls = []

        def ok_execute(*args, **kwargs):
            calls.append(kwargs.get("question", ""))
            return {
                "template": "FILTER-AGGREGATE-MEASURE (#1)",
                "results": [{"name": "Cafe X", "osm_id": 1,
                             "wkg_class": "wkgs:Cafe", "distance_m": 300}],
                "answer": "Found 1 entities within 500m.",
                "trace": [], "latency_ms": 5,
            }

        with self._patch_loop(fake_llm) as executor:
            executor.execute.side_effect = ok_execute
            result = ResearchOrchestratorService.plan("Plan a trip", "IE", None)

        assert len(calls) == 1  # results exist → no escalation
        assert result["questions"][0].get("radius_escalated") is None

    def test_plan_radius_at_floor_no_escalation(self):
        fake_llm = FakeLLM(chat_json_result={"questions": [
            {"question": "Which hotels are within 2km of Dublin?", "why": ""},
        ]})
        calls = []

        def empty_execute(*args, **kwargs):
            calls.append(kwargs.get("question", ""))
            return {
                "template": "FILTER-AGGREGATE-MEASURE (#1)",
                "results": [], "answer": "No results found.",
                "trace": [], "latency_ms": 5,
            }

        with self._patch_loop(fake_llm) as executor:
            executor.execute.side_effect = empty_execute
            result = ResearchOrchestratorService.plan("Plan a trip", "IE", None)

        # Radius already at the 2km floor → the empty result stands.
        assert len(calls) == 1
        assert result["questions"][0].get("radius_escalated") is None

    def test_plan_followup_tools(self):
        fake_llm = FakeLLM(chat_json_result={
            "questions": [
                {"question": "Which hotels are within 2km of Belize City?", "why": ""},
            ],
            "tools": [
                {"tool": "structuredSearch", "args": {"queryTags": {"amenity": "restaurant"}}},
            ],
        })
        events = []
        with mock.patch(
            "semantic_search.services.research_service._FOLLOWUP_ENABLED", True,
        ), self._patch_loop(fake_llm) as _executor:
            result = ResearchOrchestratorService.plan(
                "Plan a trip", "BZ", None, event_callback=events.append,
            )

        assert fake_llm.chat_json_calls == 2  # decompose + follow-up pick
        assert len(result["tool_calls"]) == 1
        assert result["tool_calls"][0]["tool"] == "structuredSearch"
        event_names = [e["event"] for e in events]
        assert "tool" in event_names
        assert "tool_out" in event_names


# ── Brief renderer ───────────────────────────────────────────────────────

class TestRenderBrief:
    def test_full_fields(self):
        brief = ResearchOrchestratorService._render_brief({
            "destination": "Belize City", "duration": "2-day",
            "party_size": "2 adults", "budget": "$5000",
            "interests": "exploring", "constraints": "no car",
        })
        assert brief == (
            "Plan a 2-day trip to Belize City for 2 adults with a $5000 "
            "budget, focused on exploring. Constraints: no car"
        )

    def test_minimal_fields(self):
        assert ResearchOrchestratorService._render_brief({
            "destination": "Belize City",
        }) == "Plan a trip to Belize City"

    def test_missing_destination_none(self):
        assert ResearchOrchestratorService._render_brief({
            "budget": "$5000",
        }) is None
        assert ResearchOrchestratorService._render_brief(None) is None

    def test_long_fields_capped(self):
        brief = ResearchOrchestratorService._render_brief({
            "destination": "X" * 500,
            "interests": "y" * 300,
        })
        assert len(brief) <= 400
        assert brief.startswith("Plan a trip to " + "X" * 100)
        assert "y" * 100 in brief


# ── Brief finalize ───────────────────────────────────────────────────────

class TestFinalizeBrief:
    def test_structured_extraction(self):
        fake = FakeLLM(chat_json_result={
            "destination": "Belize City", "duration": "2-day",
            "party_size": "2 adults", "budget": "$5000",
            "interests": "exploring",
        })
        with mock.patch.object(LLMService, "get_instance", return_value=fake):
            result = ResearchOrchestratorService.finalize_brief([
                {"role": "user", "content": "plan a 2-day trip to Belize City"},
                {"role": "assistant", "content": "How many people?"},
                {"role": "user", "content": "2 adults, $5000, exploring"},
            ], country_code="BZ")
        assert result["source"] == "structured"
        assert result["brief"].startswith("Plan a 2-day trip to Belize City")
        assert "for 2 adults" in result["brief"]
        assert "$5000" in result["brief"]

    def test_fallback_on_llm_down(self):
        fake = FakeLLM(available=False)
        with mock.patch.object(LLMService, "get_instance", return_value=fake):
            result = ResearchOrchestratorService.finalize_brief([
                {"role": "user", "content": "plan a trip to Belize City"},
                {"role": "assistant", "content": "When?"},
                {"role": "user", "content": "June"},
            ])
        assert result["source"] == "fallback"
        assert result["brief"] == "plan a trip to Belize City"

    def test_empty_extraction_falls_back(self):
        fake = FakeLLM(chat_json_result={"destination": ""})
        with mock.patch.object(LLMService, "get_instance", return_value=fake):
            result = ResearchOrchestratorService.finalize_brief([
                {"role": "user", "content": "plan a trip to Belize City"},
            ])
        assert result["source"] == "fallback"
        assert result["brief"] == "plan a trip to Belize City"

    def test_no_user_messages_none(self):
        fake = FakeLLM(available=False)
        with mock.patch.object(LLMService, "get_instance", return_value=fake):
            result = ResearchOrchestratorService.finalize_brief([
                {"role": "assistant", "content": "hello"},
            ])
        assert result is None
