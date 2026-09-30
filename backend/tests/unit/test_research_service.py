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

from django.conf import settings

from core.services.llm_service import LLMService
from semantic_search.services.research_service import (
    MAX_QUESTIONS,
    ResearchOrchestratorService,
    _DECOMPOSE_SYSTEM_PROMPT,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Every test starts with unset LLM_* and RESEARCH_LLM_* settings."""
    LLMService.reset_instance()
    for var, unset in (
        ("LLM_BASE_URL", ""), ("LLM_MODEL", ""), ("LLM_ENABLED", "1"),
        ("LLM_TIMEOUT", ""), ("LLM_API_STYLE", ""),
        ("LLM_AVAILABILITY_CACHE_SECONDS", ""), ("LLM_THINK", ""),
        ("RESEARCH_LLM_BASE_URL", ""), ("RESEARCH_LLM_MODEL", ""),
        ("RESEARCH_LLM_TIMEOUT", ""), ("RESEARCH_LLM_API_STYLE", ""),
        ("RESEARCH_LLM_THINK", ""), ("RESEARCH_REPLAN_LLM", "1"),
    ):
        monkeypatch.setattr(settings, var, unset)
    yield
    LLMService.reset_instance()


class FakeLLM:
    """Minimal stand-in for LLMService used by plan() tests."""

    def __init__(self, available=True, chat_json_result=None,
                 tools_result=None,
                 stream_parts=("summary part 1 ", "summary part 2")):
        self._available = available
        self._chat_json_result = chat_json_result
        self._tools_result = tools_result
        self._stream_parts = stream_parts
        self.chat_json_calls = 0
        self.chat_calls = 0
        self.chat_tools_calls = 0

    def is_available(self):
        return self._available

    def chat_json(self, *args, **kwargs):
        self.chat_json_calls += 1
        return self._chat_json_result

    def chat_tools(self, *args, **kwargs):
        self.chat_tools_calls += 1
        return None, self._tools_result

    def chat_stream(self, *args, **kwargs):
        for part in self._stream_parts:
            yield part

    def chat(self, *args, **kwargs):
        self.chat_calls += 1
        return "".join(self._stream_parts)


class _SequenceLLM(FakeLLM):
    """FakeLLM whose chat_json returns canned results in order, then None."""

    def __init__(self, results):
        super().__init__()
        self._results = list(results)

    def chat_json(self, *args, **kwargs):
        self.chat_json_calls += 1
        if self._results:
            return self._results.pop(0)
        return None


# ── LLMService: constructor overrides ────────────────────────────────────

class TestConstructorOverrides:
    def test_env_defaults_when_overrides_none(self):
        svc = LLMService()
        assert svc.style == "native"
        assert svc.base_url == "http://localhost:11434/v1"
        assert svc.model == "qwen3:8b"
        assert svc.timeout == 60.0

    def test_explicit_overrides_beat_env(self, monkeypatch):
        monkeypatch.setattr(settings, "LLM_BASE_URL", "http://ollama:11434/v1")
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
        monkeypatch.setattr(settings, "LLM_MODEL", "qwen3:14b")
        svc = LLMService(model=None, timeout="9.5")
        assert svc.model == "qwen3:14b"
        assert svc.timeout == 9.5


# ── LLMService: get_research_instance ────────────────────────────────────

class TestResearchInstance:
    def test_unset_research_env_falls_back_to_platform(self):
        inst = LLMService.get_research_instance()
        assert inst is LLMService.get_instance()

    def test_research_env_creates_second_instance(self, monkeypatch):
        monkeypatch.setattr(settings, "RESEARCH_LLM_BASE_URL", "http://research:11435/v1")
        monkeypatch.setattr(settings, "RESEARCH_LLM_MODEL", "qwen3:4b")
        monkeypatch.setattr(settings, "RESEARCH_LLM_TIMEOUT", "120")
        inst = LLMService.get_research_instance()
        platform = LLMService.get_instance()
        assert inst is not platform
        assert inst.base_url == "http://research:11435/v1"
        assert inst.model == "qwen3:4b"
        assert inst.timeout == 120.0
        assert inst.style == "native"
        # The platform instance is untouched.
        assert platform.base_url == "http://localhost:11434/v1"

    def test_research_instance_think_env(self, monkeypatch):
        monkeypatch.setattr(settings, "RESEARCH_LLM_BASE_URL", "http://research:11435/v1")
        monkeypatch.setattr(settings, "RESEARCH_LLM_THINK", "1")
        inst = LLMService.get_research_instance()
        assert inst.think is True
        # Platform default stays off.
        assert LLMService.get_instance().think is False


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

    def test_slot_passthrough(self):
        # The slot id is the plan's criteria (the summary is organized by
        # it); older planner output without a slot defaults to "general".
        decision = {"questions": [
            {"question": "Which hotels are within 2km of Belize City?",
             "why": "lodging", "slot": "stay"},
            {"question": "Which cafes are within 1km of the top hotel?",
             "why": "meals"},
        ]}
        questions = ResearchOrchestratorService._validate_questions(decision)
        assert questions[0]["slot"] == "stay"
        assert questions[1]["slot"] == "general"


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

    def test_top_entity_prefers_class(self):
        results = [
            {"name": "Annali's Restaurant", "wkg_class": "wkgs:Restaurant"},
            {"name": "Hilton Belize", "wkg_class": "wkgs:Hotel"},
        ]
        # Class-aware: "the top hotel" must not resolve to a restaurant
        # (observed 2026-09-30 on the Jamaica run).
        assert ResearchOrchestratorService._top_entity_name(
            results, class_hint="hotel",
        ) == "Hilton Belize"
        # No hint → plain first named (legacy behavior).
        assert ResearchOrchestratorService._top_entity_name(
            results,
        ) == "Annali's Restaurant"

    def test_degenerate_distances_flag(self):
        svc = ResearchOrchestratorService
        # Eight restaurants at 0m (observed 2026-09-30) → unreliable.
        assert svc._degenerate_distances([
            {"name": "A", "distance_m": 0},
            {"name": "B", "distance_m": 0},
        ]) is True
        assert svc._degenerate_distances([
            {"name": "A", "distance_m": 0},
            {"name": "B", "distance_m": 120},
        ]) is False
        assert svc._degenerate_distances([
            {"name": "A", "distance_m": 0},
        ]) is False
        assert svc._degenerate_distances([]) is False

    def test_class_mismatch_flag(self):
        svc = ResearchOrchestratorService
        # A "hotels" question whose only result is a shop → mismatch
        # (reads the OSM tags, not the coarse class).
        results = [{"name": "Shoe Store", "wkg_class": "wkgs:Shop",
                    "tags": {"shop": "shoes"}}]
        assert svc._results_mismatch(
            results, "Which hotels are within 2km of Kingston?",
        ) is True
        # A tourism=hotel result IS a hotel — no mismatch despite
        # wkgs:Tourism (Grand Lido Negril, 2026-09-30).
        results = [{"name": "Grand Lido Negril", "wkg_class": "wkgs:Tourism",
                    "tags": {"tourism": "hotel"}}]
        assert svc._results_mismatch(
            results, "Which hotels are within 2km of Negril?",
        ) is False
        # Open phrasing has no asked class → not a mismatch.
        assert svc._results_mismatch(
            results, "What amenities are near Negril?",
        ) is False
        # The ledger reader reflects the stored flag.
        assert svc._class_mismatch({"class_mismatch": True}) is True
        assert svc._class_mismatch({"class_mismatch": False}) is False

    def test_class_mismatch_slot_is_unmet(self):
        svc = ResearchOrchestratorService
        # Results exist but none match the asked class → the coverage
        # status is class_mismatch (UNMET), not filled — "jerk restaurants"
        # returning a grocery store, or 108 amenities for "historic sites".
        records = [{
            "slot": "eat",
            "question": "Which jerk restaurants are within 5km of Montego Bay?",
            "result_count": 1, "repair": None, "error": None,
            "class_mismatch": True,
        }]
        ledger = svc._coverage_ledger(records)
        assert ledger[0]["status"] == "class_mismatch"
        records[0]["class_mismatch"] = False
        assert svc._coverage_ledger(records)[0]["status"] == "filled"

    def test_entity_matches_tags_before_class(self):
        svc = ResearchOrchestratorService
        # tourism=hotel wins over the coarse wkgs:Building class.
        assert svc._entity_matches(
            {"wkg_class": "wkgs:Building", "tags": {"tourism": "hotel"}},
            "hotel",
        ) is True
        # Class fallback when no tags are present.
        assert svc._entity_matches({"wkg_class": "wkgs:Hotel"}, "hotel") is True
        assert svc._entity_matches(
            {"wkg_class": "wkgs:Shop", "tags": {"shop": "shoes"}}, "hotel",
        ) is False
        # Multi-value cuisine tag ("regional;chicken") matches a restaurant
        # intent by substring.
        assert svc._entity_matches(
            {"wkg_class": "wkgs:Amenity",
             "tags": {"amenity": "restaurant", "cuisine": "regional;chicken"}},
            "restaurant",
        ) is True

    def test_jerk_cuisine_matching(self):
        svc = ResearchOrchestratorService
        # A grocery store with no cuisine tag must NOT pass as a jerk
        # restaurant (Hi-Lo Food Store, 2026-09-30).
        assert svc._entity_matches(
            {"name": "Hi-Lo Food Store",
             "wkg_class": "wkgs:Shop", "tags": {"shop": "supermarket"}},
            "jerk",
        ) is False
        # A restaurant with Caribbean/regional cuisine IS the jerk match.
        assert svc._entity_matches(
            {"name": "Pepper's",
             "wkg_class": "wkgs:Amenity",
             "tags": {"amenity": "restaurant", "cuisine": "caribbean;international"}},
            "jerk",
        ) is True
        # "jerk restaurant" normalizes to the jerk intent.
        assert svc._class_hint_from("jerk restaurant") == "jerk"

    def test_class_swap_pluralizes_tag_target(self):
        svc = ResearchOrchestratorService
        # "hikes" → the probe found natural=peak → re-ask for peaks; the
        # executor round-trips "peaks" to ("natural", "peak").
        q = svc._class_swap_question(
            "Which hikes are within 2km of Negril?", "Negril",
            [{"name": "Blue Peak", "tags": {"natural": "peak"}}],
        )
        assert q == "Which peaks are within 2 km of Negril?"
        # Multi-word token pluralizes the last word.
        q = svc._class_swap_question(
            "Which hotels are within 2km of Negril?", "Negril",
            [{"name": "B&B", "tags": {"tourism": "guest_house"}}],
        )
        assert q == "Which guest houses are within 2 km of Negril?"

    def test_resolve_tag_target(self):
        from semantic_search.services.query_executor_service.template_executors import (
            TemplateExecutorsMixin,
        )
        resolve = TemplateExecutorsMixin._resolve_tag_target
        assert resolve("hotels") == ("tourism", "hotel")
        assert resolve("peaks") == ("natural", "peak")
        assert resolve("waterfalls") == ("waterway", "waterfall")
        assert resolve("guest houses") == ("tourism", "guest_house")
        assert resolve("cafe") == ("amenity", "cafe")
        assert resolve("shoes") is None

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

        def exec_full(*args, **kwargs):
            q = kwargs.get("question", "")
            if "cafes" in q:
                return {
                    "template": "FILTER-AGGREGATE-MEASURE (#1)",
                    "results": [{"name": "Cafe X", "osm_id": 9,
                                 "wkg_class": "wkgs:Cafe",
                                 "tags": {"amenity": "cafe"},
                                 "distance_m": 100}],
                    "answer": "Found 1 entities within 1km.",
                    "trace": [], "latency_ms": 10,
                }
            return {
                "template": "FILTER-AGGREGATE-MEASURE (#1)",
                "results": [{"name": "Hilton Belize", "osm_id": 1,
                             "wkg_class": "wkgs:Hotel",
                             "tags": {"tourism": "hotel"},
                             "distance_m": 500}],
                "answer": "Found 1 entities within 2km.",
                "trace": [], "latency_ms": 10,
            }

        with self._patch_loop(fake_llm) as executor:
            executor.execute.side_effect = exec_full
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

    def test_decompose_validation_retry_once(self):
        # DeepSeek-style smart retry: an invalid first response gets one
        # retry with the schema failure fed back, then the loop proceeds.
        llm = _SequenceLLM([
            {"bad": "shape"},
            {"questions": [
                {"question": "Which hotels are within 2km of Dublin?",
                 "why": "lodging", "slot": "stay"},
            ]},
        ])
        with self._patch_loop(llm) as _executor:
            result = ResearchOrchestratorService.plan("Plan a trip", "IE", None)
        assert llm.chat_json_calls == 2
        assert len(result["questions"]) == 1
        assert result["questions"][0]["slot"] == "stay"
        assert result["coverage"][0]["status"] == "filled"

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

        assert len(result["questions"]) == 3
        assert result["questions"][0].get("error") is None
        assert result["questions"][1]["error"] == "boom"
        assert len(result["errors"]) == 1
        # Summary still produced from the surviving answer (and the empty
        # hotels slot got a round-2 widened re-ask, not a silent gap).
        assert result["summary"] == (
            "summary part 1 summary part 2\n\nsummary part 1 summary part 2"
        )
        assert result["rounds"] == 2

    def test_plan_hub_reanchor_on_unresolvable_anchor(self):
        # "Jerk Town" (an interest-derived place) geocodes null — the loop
        # re-anchors once at the country hub (JM → Kingston) instead of
        # leaving the slot unanswerable (observed 2026-09-30).
        fake_llm = FakeLLM(chat_json_result={"questions": [
            {"question": "Which hotels are within 2km of Jerk Town?", "why": ""},
        ]})
        calls = []

        def hub_execute(*args, **kwargs):
            q = kwargs.get("question", "")
            calls.append(q)
            if "Jerk Town" in q:
                return {
                    "template": "FILTER-AGGREGATE-MEASURE (#1)",
                    "results": [], "answer": "No results found.",
                    "trace": [
                        {"step": "geocode", "input": "Jerk Town",
                         "output": None},
                        {"step": "spatial_filter_skipped",
                         "warning": "anchor has no coordinates"},
                    ],
                    "latency_ms": 5,
                }
            return {
                "template": "FILTER-AGGREGATE-MEASURE (#1)",
                "results": [{"name": "Grand Lido Negril", "osm_id": 2,
                             "wkg_class": "wkgs:Tourism",
                             "tags": {"tourism": "hotel"}}],
                "answer": "Found 1 entities within 2km.",
                "trace": [], "latency_ms": 5,
            }

        with self._patch_loop(fake_llm) as executor:
            executor.execute.side_effect = hub_execute
            result = ResearchOrchestratorService.plan(
                "Plan a trip to Jamaica", "JM", None,
            )

        assert calls == [
            "Which hotels are within 2km of Jerk Town?",
            "Which hotels are within 2km of Kingston?",
        ]
        record = result["questions"][0]
        assert record["hub_reanchored"] is True
        assert record["result_count"] == 1
        assert result["errors"] == []

    def test_continuation_questions_widen_radius(self):
        svc = ResearchOrchestratorService
        records = [{
            "question": "Which restaurants are within 2km of Jerk Town?",
            "slot": "eat", "result_count": 0, "template": "FILTER-AGGREGATE-MEASURE (#1)",
        }]
        unmet = [{
            "slot": "eat", "status": "missing",
            "question": "Which restaurants are within 2km of Jerk Town?",
        }]
        follow_ups = svc._continuation_questions(records, unmet, "JM", None)
        assert follow_ups == [{
            "question": "Which restaurants are within 6 km of Jerk Town?",
            "slot": "eat",
        }]

    def test_continuation_questions_adventure_family(self):
        svc = ResearchOrchestratorService
        records = [{
            "question": "Which hikes are within 5km of Negril?",
            "slot": "see", "result_count": 0, "template": "FILTER-AGGREGATE-MEASURE (#1)",
        }]
        unmet = [{
            "slot": "see", "status": "missing",
            "question": "Which hikes are within 5km of Negril?",
        }]
        follow_ups = svc._continuation_questions(records, unmet, "JM", None)
        # Outdoor intent → the concrete adventure target the snapshot maps.
        assert follow_ups == [{
            "question": "Which peaks are within 30 km of Negril?",
            "slot": "see",
        }]

    def test_evidence_worthy_filters_transport_noise(self):
        svc = ResearchOrchestratorService
        # Bus stops / transit must not pollute the nearest-evidence digest
        # ("5, 8, 12 Bus Stop" in a hikes slot, 2026-09-30).
        assert svc._evidence_worthy(
            {"name": "5, 8, 12 Bus Stop", "tags": {"highway": "bus_stop"}},
        ) is False
        assert svc._evidence_worthy(
            {"name": "Station", "tags": {"railway": "station"}},
        ) is False
        assert svc._evidence_worthy(
            {"name": "Platform 1", "tags": {"public_transport": "platform"}},
        ) is False
        assert svc._evidence_worthy(
            {"name": "National Gallery of Jamaica",
             "tags": {"tourism": "museum"}},
        ) is True

    def test_pluralize_category(self):
        svc = ResearchOrchestratorService
        assert svc._pluralize_category("beach") == "beaches"
        assert svc._pluralize_category("peak") == "peaks"
        assert svc._pluralize_category("guest house") == "guest houses"
        assert svc._pluralize_category("church") == "churches"
        assert svc._pluralize_category("waterfall") == "waterfalls"

    def test_plan_continuation_round(self):
        # An unmet slot drives a round-2 follow-up and an addendum: the
        # plan keeps going instead of ending on the gap.
        fake_llm = FakeLLM(chat_json_result={"questions": [
            {"question": "Which restaurants are within 2km of Jerk Town?",
             "why": "", "slot": "eat"},
        ]})
        calls = []

        def exec_rounds(*args, **kwargs):
            q = kwargs.get("question", "")
            calls.append(q)
            if "2km" in q or "amenities" in q:
                return {
                    "template": "FILTER-AGGREGATE-MEASURE (#1)",
                    "results": [], "answer": "No results found.",
                    "trace": [], "latency_ms": 5,
                }
            # The widened round-2 re-ask finds restaurants (in Kingston).
            return {
                "template": "FILTER-AGGREGATE-MEASURE (#1)",
                "results": [{"name": "Usain Bolt's Tracks and Records",
                             "osm_id": 8, "wkg_class": "wkgs:Amenity",
                             "tags": {"amenity": "restaurant"},
                             "distance_m": 6000}],
                "answer": "Found 1 entities within 6km.",
                "trace": [], "latency_ms": 5,
            }

        events = []
        with self._patch_loop(fake_llm) as executor:
            executor.execute.side_effect = exec_rounds
            result = ResearchOrchestratorService.plan(
                "Plan a trip to Jamaica", "JM", None,
                event_callback=events.append,
            )

        # Round 1: original question empty → probe → evidence (missing).
        # Round 2: widened re-ask finds the restaurants + addendum.
        assert "Which restaurants are within 6 km of Jerk Town?" in calls
        assert result["rounds"] == 2
        assert result["rounds_meta"][0]["round"] == 2
        assert result["rounds_meta"][0]["questions"] == [
            "Which restaurants are within 6 km of Jerk Town?",
        ]
        round_events = [e for e in events if e["event"] == "round"]
        assert len(round_events) == 1
        assert round_events[0]["unmet"] == ["eat"]
        # The round-2 question row exists in the trace (index continues).
        q2 = [e for e in events if e["event"] == "question" and e.get("round") == 2]
        assert len(q2) == 1
        assert q2[0]["index"] == 1
        assert q2[0]["result_count"] == 1
        # The addendum extends the summary.
        assert result["summary"] == (
            "summary part 1 summary part 2\n\nsummary part 1 summary part 2"
        )

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
                             "wkg_class": "wkgs:Tourism",
                             "tags": {"tourism": "apartment"},
                             "distance_m": 851}],
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

    def test_plan_radius_at_floor_repairs_with_evidence(self):
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

        events = []
        with self._patch_loop(fake_llm) as executor:
            executor.execute.side_effect = empty_execute
            result = ResearchOrchestratorService.plan(
                "Plan a trip", "IE", None, event_callback=events.append,
            )

        # Radius already at the 2km floor → no escalation; the coverage-led
        # repair re-asks the anchor as an open-ended #8 (zero LLM calls) and
        # records the digest as nearest evidence instead of leaving the slot
        # silently empty. The post-summary continuation then re-asks the
        # category at a wider radius (round 2).
        assert calls == [
            "Which hotels are within 2km of Dublin?",
            "What amenities are near Dublin?",
            "Which hotels are within 6 km of Dublin?",
        ]
        record = result["questions"][0]
        assert record.get("radius_escalated") is None
        assert record["repair"] == "fallback_evidence"
        assert record["evidence_question"] == "What amenities are near Dublin?"
        assert record["evidence_result_count"] == 0
        assert result["coverage"][0]["status"] == "missing"
        assert result["rounds"] == 2
        # The probe is a visible follow-up row even when it finds nothing.
        follow_ups = [e for e in events if e["event"] == "follow_up"]
        assert len(follow_ups) == 1
        assert follow_ups[0]["kind"] == "probe"
        assert follow_ups[0]["question"] == "What amenities are near Dublin?"
        assert follow_ups[0]["result_count"] == 0

    def test_plan_repair_class_swap_fills_slot(self):
        # "Which museums..." finds nothing, but the #8 probe reveals the
        # snapshot HAS tourism=museum entities — the repair class-swaps to
        # the present category (tag-driven) and the slot is filled.
        fake_llm = FakeLLM(chat_json_result={"questions": [
            {"question": "Which museums are within 2km of Belize City?",
             "why": "culture", "slot": "see"},
        ]})
        calls = []

        def exec_repair(*args, **kwargs):
            q = kwargs.get("question", "")
            calls.append(q)
            if "2km" in q:  # the original question — no results
                return {
                    "template": "FILTER-AGGREGATE-MEASURE (#1)",
                    "results": [], "answer": "No results found.",
                    "trace": [], "latency_ms": 5,
                }
            # The probe + the class-swapped re-ask find museums (tags carry
            # the real semantics — wkgs:Historic is coarse).
            return {
                "template": "PLACE-ATTRIBUTE-QUERY (#8)",
                "results": [
                    {"name": "Belize Museum", "osm_id": 7,
                     "wkg_class": "wkgs:Historic",
                     "tags": {"tourism": "museum"}, "distance_m": 900},
                ],
                "answer": "Found 1 entities.",
                "trace": [], "latency_ms": 5,
            }

        events = []
        with self._patch_loop(fake_llm) as executor:
            executor.execute.side_effect = exec_repair
            result = ResearchOrchestratorService.plan(
                "Plan a trip", "BZ", None, event_callback=events.append,
            )

        assert calls == [
            "Which museums are within 2km of Belize City?",
            "What amenities are near Belize City?",
            "Which museums are within 2 km of Belize City?",
        ]
        record = result["questions"][0]
        assert record["repair"] == "class_swap"
        assert record["original_question"] == \
            "Which museums are within 2km of Belize City?"
        assert record["result_count"] == 1
        assert "Belize Museum" in record["digest"]
        # The slot is the plan's criteria id; the status says the slot was
        # filled by the closest present category, not the asked one.
        assert result["coverage"][0]["slot"] == "see"
        assert result["coverage"][0]["status"] == "filled_by_repair"
        assert result["coverage"][0]["question"] == \
            "Which museums are within 2km of Belize City?"
        replan_events = [e for e in events if e["event"] == "replan"]
        assert len(replan_events) == 1
        assert replan_events[0]["repairs"][0]["status"] == "filled_by_repair"
        assert replan_events[0]["repairs"][0]["repair"] == "class_swap"
        # The follow-ups are visible: probe + the class-swap re-ask.
        follow_ups = [e for e in events if e["event"] == "follow_up"]
        assert [f["kind"] for f in follow_ups] == ["probe", "class_swap"]
        assert follow_ups[1]["question"] == \
            "Which museums are within 2 km of Belize City?"
        assert follow_ups[1]["result_count"] == 1

    def test_plan_placeholder_resolves_to_hotel_class(self):
        # End-to-end: the hotels question returns a restaurant ranked first,
        # but the cafe question's "the top hotel" must resolve to the hotel.
        fake_llm = FakeLLM(chat_json_result={"questions": [
            {"question": "Which hotels are within 2km of Belize City?", "why": ""},
            {"question": "Which cafes are within 1km of the top hotel?", "why": ""},
        ]})
        calls = []

        def exec_hotels(*args, **kwargs):
            q = kwargs.get("question", "")
            calls.append(q)
            if "hotels" in q:
                return {
                    "template": "FILTER-AGGREGATE-MEASURE (#1)",
                    "results": [
                        {"name": "Annali's Restaurant", "osm_id": 1,
                         "wkg_class": "wkgs:Restaurant", "distance_m": 300},
                        {"name": "Hilton Belize", "osm_id": 2,
                         "wkg_class": "wkgs:Hotel", "distance_m": 500},
                    ],
                    "answer": "Found 2 entities within 2km.",
                    "trace": [], "latency_ms": 5,
                }
            return {
                "template": "FILTER-AGGREGATE-MEASURE (#1)",
                "results": [{"name": "Cafe X", "osm_id": 3,
                             "wkg_class": "wkgs:Cafe", "distance_m": 100}],
                "answer": "Found 1 entities within 1km.",
                "trace": [], "latency_ms": 5,
            }

        with self._patch_loop(fake_llm) as executor:
            executor.execute.side_effect = exec_hotels
            result = ResearchOrchestratorService.plan("Plan a trip", "BZ", None)

        # The restaurant must NOT become the anchor for the cafes question.
        assert calls[1] == "Which cafes are within 1km of Hilton Belize?"

    def test_plan_result_carries_recipe(self):
        fake_llm = FakeLLM(chat_json_result={"questions": [
            {"question": "Which hotels are within 2km of Belize City?",
             "why": ""},
        ]})
        with self._patch_loop(fake_llm) as _executor:
            result = ResearchOrchestratorService.plan(
                "Plan a 2-day trip to Belize City", "BZ", None,
            )
        # The router picks the recipe; the run reports it (rides the done
        # payload so the frontend knows the plan-type schema).
        assert result["recipe"] == "trip_plan"

    def test_plan_llm_replan_rewrites_slot(self):
        # Deterministic repair exhausted (no classes near the anchor) →
        # one bounded LLM rewrite re-asks with a different class.
        llm = _SequenceLLM([
            {"questions": [
                {"question": "Which hikes are near Montego Bay?",
                 "why": "", "slot": "see"},
            ]},
            {"question": "Which peaks are within 10km of Montego Bay?"},
        ])
        calls = []

        def exec_replan(*args, **kwargs):
            q = kwargs.get("question", "")
            calls.append(q)
            if "hikes" in q or "amenities" in q:
                return {
                    "template": "FILTER-AGGREGATE-MEASURE (#1)",
                    "results": [], "answer": "No results found.",
                    "trace": [], "latency_ms": 5,
                }
            return {
                "template": "FILTER-AGGREGATE-MEASURE (#1)",
                "results": [
                    {"name": "Blue Mountain Peak", "osm_id": 3,
                     "wkg_class": "wkgs:Peak", "distance_m": 9000},
                ],
                "answer": "Found 1 entities within 10km.",
                "trace": [], "latency_ms": 5,
            }

        events = []
        with self._patch_loop(llm) as executor:
            executor.execute.side_effect = exec_replan
            result = ResearchOrchestratorService.plan(
                "Plan a trip", "JM", None, event_callback=events.append,
            )

        # decompose + exactly one replan rewrite (bounded).
        assert llm.chat_json_calls == 2
        assert "Which peaks are within 10km of Montego Bay?" in calls
        record = result["questions"][0]
        assert record["repair"] == "llm_replan"
        assert record["original_question"] == "Which hikes are near Montego Bay?"
        assert record["result_count"] == 1
        assert result["coverage"][0]["status"] == "filled_by_repair"
        # The rewrite is a visible follow-up row (probe + llm_replan).
        follow_ups = [e for e in events if e["event"] == "follow_up"]
        assert [f["kind"] for f in follow_ups] == ["probe", "llm_replan"]
        assert follow_ups[1]["question"] == \
            "Which peaks are within 10km of Montego Bay?"
        assert follow_ups[1]["result_count"] == 1

    def test_plan_open_ended_question_not_repaired(self):
        # An already-open #8-style question must not be re-run by the
        # repair pass (fallback == question → skipped).
        fake_llm = FakeLLM(chat_json_result={"questions": [
            {"question": "What amenities are near Belize City?", "why": ""},
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
            result = ResearchOrchestratorService.plan("Plan a trip", "BZ", None)

        assert calls == ["What amenities are near Belize City?"]
        record = result["questions"][0]
        assert record.get("repair") is None
        assert result["coverage"][0]["status"] == "missing"

    def test_plan_followup_tools(self):
        fake_llm = FakeLLM(
            chat_json_result={
                "questions": [
                    {"question": "Which hotels are within 2km of Belize City?", "why": ""},
                ],
            },
            tools_result=[
                {"name": "structuredSearch",
                 "arguments": {"queryTags": {"amenity": "restaurant"}}},
            ],
        )
        events = []
        with mock.patch(
            "semantic_search.services.research_service._FOLLOWUP_ENABLED", True,
        ), self._patch_loop(fake_llm) as _executor:
            result = ResearchOrchestratorService.plan(
                "Plan a trip", "BZ", None, event_callback=events.append,
            )

        # Decompose stays chat_json; the follow-up pick is native tools now.
        assert fake_llm.chat_json_calls == 1
        assert fake_llm.chat_tools_calls == 1
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
