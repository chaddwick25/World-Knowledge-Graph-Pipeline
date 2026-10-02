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
from semantic_search.services.research_recipes import (
    get_recipe,
    route_to_recipe,
)
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


# The PLACE_REPORT recipe's required slots. Tests that exercise specific
# loop mechanics give the planner ALL required slots so the slot-coverage
# fill (2026-10-01) is a no-op and their exact question/call assertions
# hold. The dummy questions are class-neutral on purpose: no class words
# (no class_mismatch against whatever the mock returns), and no overlap
# with the mock executors' special branches ("cafes", "2km", "amenities",
# "Dublin", ...).
_REQUIRED_SLOT_DUMMIES = [
    {"question": "What is within 3km of the anchor?",
     "why": "", "slot": "eat"},
    {"question": "What is within 3km of the anchor?",
     "why": "", "slot": "shop"},
    {"question": "What is near the anchor?",
     "why": "", "slot": "services"},
    {"question": "What is near the anchor?",
     "why": "", "slot": "move"},
    {"question": "What is within 3km of the anchor?",
     "why": "", "slot": "infra"},
]


def _full_slots(questions):
    """Give the planner a question per required PLACE_REPORT slot within
    the MAX_QUESTIONS cap, so the slot-coverage fill (2026-10-01) is a
    no-op and exact question/call assertions hold. Questions without a
    slot get one assigned (round-robin); the rest are appended as
    class-neutral dummies (no class words — no class_mismatch against
    whatever the exec mock returns, no collision with its branches)."""
    required = [d["slot"] for d in _REQUIRED_SLOT_DUMMIES]
    out = []
    used = set()
    for q in questions:
        slot = q.get("slot") or required[len(out) % len(required)]
        out.append(dict(q, slot=slot))
        used.add(slot)
    for dummy in _REQUIRED_SLOT_DUMMIES:
        if len(out) >= MAX_QUESTIONS:
            break
        if dummy["slot"] not in used:
            out.append(dict(dummy))
            used.add(dummy["slot"])
    return out


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

    def test_resolvable_class_whitelist_present(self):
        # The planner must stay inside the resolver's vocabulary
        # (2026-10-01) — vague intents are rephrased, not asked.
        assert "resolvable class vocabulary" in _DECOMPOSE_SYSTEM_PROMPT
        assert "marketplace" in _DECOMPOSE_SYSTEM_PROMPT
        assert "taco vendors" in _DECOMPOSE_SYSTEM_PROMPT

    def test_slot_coverage_and_radius_m_present(self):
        # Every required slot gets >= 1 question; radii are agent-decided
        # and structured (2026-10-01).
        assert (
            "Every required slot must have at least one question"
            in _DECOMPOSE_SYSTEM_PROMPT
        )
        assert "radius_m" in _DECOMPOSE_SYSTEM_PROMPT

    def test_slot_ids_and_placeholders_constrained(self):
        # No invented slot ids ("culture"), and placeholders only in #1
        # radius questions (2026-10-01 — the Mexico City run produced a
        # #2 distance question with ungeocodable placeholders).
        assert "Use ONLY these slot ids" in _DECOMPOSE_SYSTEM_PROMPT
        assert "Never invent a new slot id" in _DECOMPOSE_SYSTEM_PROMPT
        assert (
            "placeholders ONLY in radius (#1) questions"
            in _DECOMPOSE_SYSTEM_PROMPT
        )


# ── Agent-decided radius + slot coverage (2026-10-01) ────────────────────

class TestAgentRadiusAndSlotCoverage:
    def test_clamp_radius_m(self):
        svc = ResearchOrchestratorService
        assert svc._clamp_radius_m(2000) == 2000
        assert svc._clamp_radius_m("3000") == 3000
        assert svc._clamp_radius_m(50) is None      # below the 100m floor
        assert svc._clamp_radius_m(60000) == 50000  # capped at 50 km
        assert svc._clamp_radius_m(None) is None
        assert svc._clamp_radius_m("abc") is None

    def test_inject_radius_replaces_amount(self):
        svc = ResearchOrchestratorService
        parsed = {"template": "FILTER-AGGREGATE-MEASURE (#1)",
                  "concepts": [{"type": "OBJECT", "text": "restaurants"},
                               {"type": "AMOUNT", "text": "2km"}]}
        out = svc._inject_radius(parsed, 5000)
        amount = [c for c in out["concepts"] if c["type"] == "AMOUNT"]
        assert amount[0]["text"] == "5000m"

    def test_inject_radius_appends_when_missing(self):
        svc = ResearchOrchestratorService
        parsed = {"template": "FILTER-AGGREGATE-MEASURE (#1)",
                  "concepts": [{"type": "OBJECT", "text": "restaurants"}]}
        out = svc._inject_radius(parsed, 3000)
        assert any(c["type"] == "AMOUNT" and c["text"] == "3000m"
                   for c in out["concepts"])

    def test_inject_radius_ignores_other_templates(self):
        svc = ResearchOrchestratorService
        parsed = {"template": "PLACE-ATTRIBUTE-QUERY (#8)", "concepts": []}
        assert svc._inject_radius(parsed, 3000) is parsed
        assert svc._inject_radius(None, 3000) is None
        assert svc._inject_radius(parsed, None) is parsed

    def test_missing_required_slots(self):
        svc = ResearchOrchestratorService
        recipe = get_recipe("place_report")
        assert svc._missing_required_slots([], recipe) == [
            "eat", "shop", "services", "move", "infra",
        ]
        assert svc._missing_required_slots(
            [{"slot": "eat"}, {"slot": "move"}], recipe,
        ) == ["shop", "services", "infra"]
        assert svc._missing_required_slots([], None) == []

    def test_fill_missing_slots_uses_anchor(self):
        svc = ResearchOrchestratorService
        recipe = get_recipe("place_report")
        questions = [{
            "question": "Which restaurants are within 2km of Belmopan?",
            "why": "", "slot": "eat", "radius_m": 2000,
        }]
        filled = svc._fill_missing_slots(questions, recipe)
        slots = {q["slot"] for q in filled}
        assert {"eat", "shop", "services", "move", "infra"} <= slots
        # The fill questions carry the recipe defaults + a parsed radius.
        shop = next(q for q in filled if q["slot"] == "shop")
        assert shop["question"] == "Which shops are within 3km of Belmopan?"
        assert shop["radius_m"] == 3000
        # The original question is untouched and first.
        assert filled[0]["question"] == questions[0]["question"]

    def test_fill_missing_slots_noop_when_covered(self):
        svc = ResearchOrchestratorService
        recipe = get_recipe("place_report")
        questions = _full_slots([{
            "question": "Which restaurants are within 2km of Belmopan?",
            "why": "", "slot": "eat", "radius_m": 2000,
        }])
        assert svc._fill_missing_slots(questions, recipe) == questions

    def test_taco_cuisine_matching(self):
        svc = ResearchOrchestratorService
        # A taco spot carries cuisine=tacos (multi-value "tacos;tortas") —
        # the TAG_RULES cuisine family matches it (2026-10-01).
        assert svc._entity_matches(
            {"name": "Taquería El Califa",
             "wkg_class": "wkgs:Amenity",
             "tags": {"amenity": "restaurant", "cuisine": "tacos;tortas"}},
            "tacos",
        ) is True
        # A convenience store without cuisine must NOT pass as tacos.
        assert svc._entity_matches(
            {"name": "Oxxo", "wkg_class": "wkgs:Shop",
             "tags": {"shop": "convenience"}},
            "tacos",
        ) is False


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
        # Commercial generalization (2026-10-01): markets, bakeries, and
        # the taco/mexican cuisine family resolve instead of falling to
        # the fuzzy tier ("markets" → marketplace, "taco vendors" →
        # restaurant after the trailing-word strip).
        assert resolve("markets") == ("amenity", "marketplace")
        assert resolve("market") == ("amenity", "marketplace")
        assert resolve("bakery") == ("shop", "bakery")
        assert resolve("tacos") == ("amenity", "restaurant")
        assert resolve("taqueria") == ("amenity", "restaurant")
        assert resolve("taco vendors") == ("amenity", "restaurant")
        assert resolve("food vendors") == ("amenity", "restaurant")

    def test_parse_radius_m(self):
        svc = ResearchOrchestratorService
        assert svc._parse_radius_m("within 1km of Dublin") == 1000
        assert svc._parse_radius_m("within 500m of Temple Bar") == 500
        assert svc._parse_radius_m("within 2 km of Dublin") == 2000
        assert svc._parse_radius_m("within 1.5km of Rome") == 1500
        assert svc._parse_radius_m("near Dublin") is None
        assert svc._parse_radius_m(None) is None

    def test_anchor_geocode_failed_detection(self):
        assert ResearchOrchestratorService._anchor_geocode_failed({
            "trace": [{"step": "geocode", "output": None},
                      {"step": "spatial_filter_skipped"}],
        }) is True
        assert ResearchOrchestratorService._anchor_geocode_failed({
            "trace": [{"step": "geocode", "output": {"lat": 1}}],
        }) is False


# ── Recipe routing (research_recipes.py) ─────────────────────────────────

class TestRecipeRouting:
    def test_default_recipe_is_place_report(self):
        # The general researcher's default: a domain-neutral report over
        # the classes OSM maps most completely (commercial entities,
        # services, transit, infrastructure) — 2026-10-01.
        assert get_recipe()["key"] == "place_report"
        assert get_recipe("place_report")["slots"][0]["id"] == "eat"
        assert route_to_recipe(None)["key"] == "place_report"
        assert route_to_recipe("")["key"] == "place_report"

    def test_place_report_prompts_stay_on_default(self):
        # Commercial / transit / infrastructure research questions all
        # land on the default recipe — there is no trip recipe to route
        # to since TRIP_PLAN was removed 2026-10-01.
        for prompt in (
            "Overview the restaurants and shops in Belmopan",
            "How well is Belize City served by public transit?",
            "Where are the fuel stations on the road from Belmopan to Belize City?",
            "Plan a study of restaurants in Belmopan",
            "Where should I visit in Belize City?",
        ):
            assert route_to_recipe(prompt)["key"] == "place_report", prompt

    def test_trip_prompts_now_land_on_place_report(self):
        # TRIP_PLAN was removed 2026-10-01 (tourism strained the data);
        # even explicit travel phrasing stays on the default recipe.
        for prompt in (
            "Plan a 2-day trip to Belize City",
            "Plan a trip to Dublin",
            "Build an itinerary for Rome",
            "I want a vacation in Havana",
            "5-day tour of Ireland",
        ):
            assert route_to_recipe(prompt)["key"] == "place_report", prompt


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
        fake_llm = FakeLLM(chat_json_result={"questions": _full_slots([
            {"question": "Which hotels are within 2km of the centre of Belize City?", "why": "lodging"},
            {"question": "Which cafes are within 1km of the top hotel?", "why": "meals"},
        ])})
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

        assert len(result["questions"]) == 5  # 2 planned + 3 slot fills
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
            {"questions": _full_slots([
                {"question": "Which hotels are within 2km of Dublin?",
                 "why": "lodging", "slot": "eat"},
            ])},
        ])
        with self._patch_loop(llm) as _executor:
            result = ResearchOrchestratorService.plan("Plan a trip", "IE", None)
        assert llm.chat_json_calls == 2
        assert len(result["questions"]) == 5  # 1 planned + 4 slot fills
        assert result["questions"][0]["slot"] == "eat"
        assert result["coverage"][0]["status"] == "filled"

    def test_plan_question_failure_keeps_loop_alive(self):
        fake_llm = FakeLLM(chat_json_result={"questions": _full_slots([
            {"question": "Which hotels are within 2km of Belize City?", "why": ""},
            {"question": "Which cafes are within 1km of Belize City?", "why": ""},
        ])})
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
            if "hotels" in question:
                return execute_result  # the empty hotels slot
            return {
                "template": "FILTER-AGGREGATE-MEASURE (#1)",
                "results": [{"name": "Cafe X", "osm_id": 9,
                             "wkg_class": "wkgs:Cafe", "distance_m": 100}],
                "answer": "Found 1 entities.",
                "trace": [], "latency_ms": 5,
            }

        with self._patch_loop(fake_llm) as executor:
            executor.execute.side_effect = flaky_execute
            result = ResearchOrchestratorService.plan(
                "Plan a trip", "BZ", None,
            )

        # 2 planned + 3 slot fills, plus the round-2 wider re-ask for the
        # empty hotels slot.
        assert len(result["questions"]) == 6
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
        fake_llm = FakeLLM(chat_json_result={"questions": _full_slots([
            {"question": "Which hotels are within 2km of Jerk Town?", "why": ""},
        ])})
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
            "What is within 3km of the anchor?",   # shop fill
            "What is near the anchor?",            # services fill
            "What is near the anchor?",            # move fill
            "What is within 3km of the anchor?",   # infra fill
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

    def test_evidence_worthy_allow_transport_for_transit_slots(self):
        svc = ResearchOrchestratorService
        # A transit-target slot (place-report "getting around") keeps the
        # bus stops that a tourism slot treats as noise — the abundance
        # IS the signal there (2026-10-01).
        assert svc._evidence_worthy(
            {"name": "5, 8, 12 Bus Stop", "tags": {"highway": "bus_stop"}},
            allow_transport=True,
        ) is True
        assert svc._evidence_worthy(
            {"name": "Platform 1", "tags": {"public_transport": "platform"}},
            allow_transport=True,
        ) is True
        # Default (tourism) behavior unchanged.
        assert svc._evidence_worthy(
            {"name": "5, 8, 12 Bus Stop", "tags": {"highway": "bus_stop"}},
        ) is False

    def test_slot_allows_transport_reads_recipe(self):
        svc = ResearchOrchestratorService
        place_report = get_recipe("place_report")
        assert svc._slot_allows_transport(place_report, "move") is True
        assert svc._slot_allows_transport(place_report, "eat") is False
        assert svc._slot_allows_transport(None, "move") is False
        assert svc._slot_allows_transport(place_report, "nope") is False

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
        fake_llm = FakeLLM(chat_json_result={"questions": _full_slots([
            {"question": "Which restaurants are within 2km of Jerk Town?",
             "why": "", "slot": "eat"},
        ])})
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
        assert q2[0]["index"] == 5  # round 1 = eat + 4 slot fills
        assert q2[0]["result_count"] == 1
        # The addendum extends the summary.
        assert result["summary"] == (
            "summary part 1 summary part 2\n\nsummary part 1 summary part 2"
        )

    def test_plan_anchor_qualifier_retry(self):
        fake_llm = FakeLLM(chat_json_result={"questions": _full_slots([
            {"question": "Which hotels are within 2km of the centre of Belize City?", "why": ""},
        ])})
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
            "What is within 3km of the anchor?",   # shop fill
            "What is near the anchor?",            # services fill
            "What is near the anchor?",            # move fill
            "What is within 3km of the anchor?",   # infra fill
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

    def test_plan_small_radius_empty_goes_to_repair(self):
        # The small-radius escalation was removed 2026-10-01: the agent
        # decides radii (radius_m); an empty 1km question flows to the
        # repair pipeline (probe → class-swap → evidence) instead of being
        # silently widened behind the agent's back.
        fake_llm = FakeLLM(chat_json_result={"questions": _full_slots([
            {"question": "Which hotels are within 1km of Dublin?",
             "why": "", "slot": "eat"},
        ])})
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
                "answer": "Found 1 entities.",
                "trace": [], "latency_ms": 5,
            }

        with self._patch_loop(fake_llm) as executor:
            executor.execute.side_effect = radius_execute
            result = ResearchOrchestratorService.plan("Plan a trip", "IE", None)

        record = result["questions"][0]
        assert record.get("radius_escalated") is None
        assert record["repair"] in ("class_swap", "fallback_evidence")

    def test_plan_small_radius_with_results_no_repair(self):
        fake_llm = FakeLLM(chat_json_result={"questions": _full_slots([
            {"question": "Which cafes are within 500m of Temple Bar?", "why": ""},
        ])})
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

        assert len(calls) == 5  # 1 planned + 4 slot fills, all with results
        assert result["questions"][0].get("radius_escalated") is None

    def test_plan_radius_at_floor_repairs_with_evidence(self):
        fake_llm = FakeLLM(chat_json_result={"questions": _full_slots([
            {"question": "Which hotels are within 2km of Dublin?", "why": ""},
        ])})
        calls = []

        def empty_execute(*args, **kwargs):
            q = kwargs.get("question", "")
            calls.append(q)
            if "Dublin" in q or "amenities" in q:
                return {
                    "template": "FILTER-AGGREGATE-MEASURE (#1)",
                    "results": [], "answer": "No results found.",
                    "trace": [], "latency_ms": 5,
                }
            return {
                "template": "FILTER-AGGREGATE-MEASURE (#1)",
                "results": [{"name": "The Shelbourne", "osm_id": 9,
                             "wkg_class": "wkgs:Hotel", "distance_m": 1200}],
                "answer": "Found 1 entities.",
                "trace": [], "latency_ms": 5,
            }

        events = []
        with self._patch_loop(fake_llm) as executor:
            executor.execute.side_effect = empty_execute
            result = ResearchOrchestratorService.plan(
                "Plan a trip", "IE", None, event_callback=events.append,
            )

        # No escalation: the coverage-led repair re-asks the anchor as an
        # open-ended #8 and records the digest as nearest evidence instead
        # of leaving the slot silently empty. The post-summary continuation
        # then re-asks the category at a wider radius (round 2).
        assert calls == [
            "Which hotels are within 2km of Dublin?",
            "What is within 3km of the anchor?",   # shop fill
            "What is near the anchor?",            # services fill
            "What is near the anchor?",            # move fill
            "What is within 3km of the anchor?",   # infra fill
            "What amenities are near Dublin?",     # repair probe
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
        fake_llm = FakeLLM(chat_json_result={"questions": _full_slots([
            {"question": "Which museums are within 2km of Belize City?",
             "why": "culture", "slot": "see"},
        ])})
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
            "What is within 3km of the anchor?",   # eat fill
            "What is within 3km of the anchor?",   # shop fill
            "What is near the anchor?",            # services fill
            "What is near the anchor?",            # move fill
            "What is within 3km of the anchor?",   # infra fill
            "What amenities are near Belize City?",  # repair probe
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
                # Trip phrasing no longer routes anywhere else — TRIP_PLAN
                # removed 2026-10-01; the default recipe is the result.
                "Plan a 2-day trip to Belize City", "BZ", None,
            )
        # The run reports the recipe (rides the done payload so the
        # frontend knows the plan-type schema).
        assert result["recipe"] == "place_report"

    def test_plan_scopes_execution_to_subdivision(self):
        # A subdivision-scoped run threads the QID into EVERY executor
        # call (round 1 + repair probes + continuation) so the results are
        # bounded to the district polygon (2026-10-01).
        fake_llm = FakeLLM(chat_json_result={"questions": _full_slots([
            {"question": "Which restaurants are within 2km of Belmopan?",
             "why": "", "slot": "eat"},
        ])})
        with self._patch_loop(fake_llm) as executor:
            ResearchOrchestratorService.plan(
                "Overview restaurants in Belmopan", "BZ", None,
                subdivision_qid="Q1234",
            )
        calls = executor.execute.call_args_list
        assert calls
        assert all(
            c.kwargs.get("subdivision_qid") == "Q1234" for c in calls
        )

    def test_plan_without_subdivision_passes_none(self):
        fake_llm = FakeLLM(chat_json_result={"questions": _full_slots([
            {"question": "Which restaurants are within 2km of Belmopan?",
             "why": "", "slot": "eat"},
        ])})
        with self._patch_loop(fake_llm) as executor:
            ResearchOrchestratorService.plan(
                "Overview restaurants in Belmopan", "BZ", None,
            )
        assert all(
            c.kwargs.get("subdivision_qid") is None
            for c in executor.execute.call_args_list
        )

    def test_chat_messages_include_subdivision_scope(self):
        svc = ResearchOrchestratorService
        msgs = svc.chat_messages(
            [{"role": "user", "content": "hi"}], "BZ", "2025_12_31",
            subdivision_qid="Q1234",
        )
        assert "Scope: subdivision Q1234" in msgs[0]["content"]
        msgs = svc.chat_messages(
            [{"role": "user", "content": "hi"}], "BZ", "2025_12_31",
        )
        assert "Scope:" not in msgs[0]["content"]

    def test_scope_to_subdivision_filters_results(self):
        from unittest import mock as _mock

        from django.contrib.gis.geos import Polygon

        from semantic_search.services.query_executor_service.service import (
            QueryExecutorService,
        )

        poly = Polygon(((0, 0), (0, 2), (2, 2), (2, 0), (0, 0)))
        with _mock.patch(
            "semantic_search.utils.subdivision_resolver.resolve_subdivision_polygon",
            return_value=poly,
        ):
            scoped = QueryExecutorService._scope_to_subdivision(
                [
                    {"name": "Inside", "lat": 1.0, "lon": 1.0},
                    {"name": "Outside", "lat": 5.0, "lon": 5.0},
                    {"name": "No coords", "lat": None, "lon": None},
                ],
                "Q1234", [],
            )
        assert [r["name"] for r in scoped] == ["Inside"]

    def test_dedupe_results_collapses_co_located_and_caps_chains(self):
        from semantic_search.services.query_executor_service.service import (
            QueryExecutorService,
        )

        svc = QueryExecutorService
        results = [
            # A real chain: 4 spread-out same-name entries → capped at 3.
            {"name": "Tacos El Güero", "lat": 19.40, "lon": -99.13},
            {"name": "Tacos El Güero", "lat": 19.41, "lon": -99.13},
            {"name": "Tacos El Güero", "lat": 19.42, "lon": -99.13},
            {"name": "Tacos El Güero", "lat": 19.43, "lon": -99.13},
            # A true duplicate: same name, co-located (~11 m apart).
            {"name": "Farmacia París", "lat": 19.40, "lon": -99.14},
            {"name": "Farmacia París", "lat": 19.4001, "lon": -99.14},
            # Distinct names survive; nameless pass through.
            {"name": "Mercado de San Juan", "lat": 19.41, "lon": -99.15},
            {"lat": None, "lon": None},
        ]
        trace = []
        deduped = svc._dedupe_results(results, trace)
        names = [r.get("name") for r in deduped]
        assert names.count("Tacos El Güero") == 3
        assert names.count("Farmacia París") == 1
        assert "Mercado de San Juan" in names
        assert any(r.get("name") is None for r in deduped)
        assert len(deduped) == 6  # 3 + 1 + 1 + 1 nameless
        assert trace[-1]["step"] == "dedupe_results"
        assert trace[-1]["dropped"] == 2  # 1 chain cap + 1 co-located

    def test_plan_place_report_prompt_carries_recipe(self):
        # The general researcher default: a place report (commercial /
        # services / transit / infrastructure) routes to place_report and
        # the run reports it (2026-10-01).
        fake_llm = FakeLLM(chat_json_result={"questions": [
            {"question": "Which restaurants are within 2km of Belmopan?",
             "why": "", "slot": "eat"},
        ]})
        with self._patch_loop(fake_llm) as _executor:
            result = ResearchOrchestratorService.plan(
                "Overview the restaurants, shops, and transit in Belmopan",
                "BZ", None,
            )
        assert result["recipe"] == "place_report"
        assert result["questions"][0]["slot"] == "eat"

    def test_plan_llm_replan_rewrites_slot(self):
        # Deterministic repair exhausted (no classes near the anchor) →
        # one bounded LLM rewrite re-asks with a different class.
        llm = _SequenceLLM([
            {"questions": _full_slots([
                {"question": "Which hikes are near Montego Bay?",
                 "why": "", "slot": "see"},
            ])},
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

    def test_plan_replan_outside_template_vocabulary_discarded(self):
        # An LLM replan that rewrites into a non-parser question ("What
        # is the cultural significance of tacos?") must be discarded — the
        # template gate in _rerun rejects parses outside the 5 trained
        # shapes (fabricated "CULTURAL-CONTEXT-EXPLORATION" output was
        # observed on the Mexico City run, 2026-10-01).
        llm = _SequenceLLM([
            {"questions": _full_slots([
                {"question": "Which hikes are near Montego Bay?",
                 "why": "", "slot": "see"},
            ])},
            {"question": "What is the cultural significance of tacos?"},
        ])
        calls = []

        def exec_replan(*args, **kwargs):
            q = kwargs.get("question", "")
            calls.append(q)
            return {
                "template": "FILTER-AGGREGATE-MEASURE (#1)",
                "results": [], "answer": "No results found.",
                "trace": [], "latency_ms": 5,
            }

        parser = mock.Mock()

        def parse_side(q):
            if "cultural significance" in q:
                return {"template": "CULTURAL-CONTEXT-EXPLORATION (#2)",
                        "confidence": 0.5, "concepts": []}
            return {"template": "FILTER-AGGREGATE-MEASURE (#1)",
                    "confidence": 0.9, "concepts": []}

        parser.get_instance.return_value.parse.side_effect = parse_side

        executor = mock.Mock()
        executor.execute.side_effect = exec_replan
        with mock.patch(
            "core.services.llm_service.LLMService.get_research_instance",
            return_value=llm,
        ), mock.patch(
            "semantic_search.services.query_parser_service.QueryParserService",
            parser,
        ), mock.patch(
            "semantic_search.services.query_executor_service.QueryExecutorService",
            executor,
        ):
            result = ResearchOrchestratorService.plan("Plan a trip", "JM", None)

        # The rewrite never reached the executor (discarded by the gate).
        assert not any("cultural significance" in q for q in calls)
        record = result["questions"][0]
        assert record["repair"] == "fallback_evidence"

    def test_plan_open_ended_question_not_repaired(self):
        # An already-open #8-style question must not be re-run by the
        # repair pass (fallback == question → skipped).
        fake_llm = FakeLLM(chat_json_result={"questions": _full_slots([
            {"question": "What amenities are near Belize City?", "why": ""},
        ])})
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

        # The open-ended question is not re-run (probe == question); the
        # slot fills are separate questions that do get probed.
        assert calls[0] == "What amenities are near Belize City?"
        assert len(calls) > 1
        record = result["questions"][0]
        assert record.get("repair") is None
        assert result["coverage"][0]["status"] == "missing"

    def test_plan_followup_tools(self):
        fake_llm = FakeLLM(
            chat_json_result={
                "questions": _full_slots([
                    {"question": "Which hotels are within 2km of Belize City?", "why": ""},
                ]),
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
            "area": "Belize City", "scope": "within 3km",
            "focus": "restaurants and shops", "constraints": "no car",
        })
        assert brief == (
            "Research Belize City (within 3km) focusing on restaurants "
            "and shops. Constraints: no car"
        )

    def test_minimal_fields(self):
        assert ResearchOrchestratorService._render_brief({
            "area": "Belize City",
        }) == "Research Belize City"

    def test_missing_area_none(self):
        assert ResearchOrchestratorService._render_brief({
            "focus": "restaurants",
        }) is None
        assert ResearchOrchestratorService._render_brief(None) is None

    def test_long_fields_capped(self):
        brief = ResearchOrchestratorService._render_brief({
            "area": "X" * 500,
            "focus": "y" * 300,
        })
        assert len(brief) <= 400
        assert brief.startswith("Research " + "X" * 100)
        assert "y" * 100 in brief


# ── Brief finalize ───────────────────────────────────────────────────────

class TestFinalizeBrief:
    def test_structured_extraction(self):
        fake = FakeLLM(chat_json_result={
            "area": "Belize City", "focus": "restaurants and shops",
            "scope": "within 3km",
        })
        with mock.patch.object(LLMService, "get_instance", return_value=fake):
            result = ResearchOrchestratorService.finalize_brief([
                {"role": "user", "content": "what restaurants and shops are around Belize City"},
                {"role": "assistant", "content": "How far out?"},
                {"role": "user", "content": "within 3km"},
            ], country_code="BZ")
        assert result["source"] == "structured"
        assert result["brief"].startswith("Research Belize City")
        assert "restaurants and shops" in result["brief"]
        assert "3km" in result["brief"]

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
