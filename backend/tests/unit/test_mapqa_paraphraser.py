"""Unit tests for the MapQA paraphraser.

Tests paraphrase invariants ([MAPQA_TEMPLATE_COVERAGE_EXPANSION_PLAN.md] §4, §8):
  - Slot invariance: every paraphrase (both tiers) contains all slot values
    verbatim; LLM outputs failing this are rejected.
  - With the LLM tier disabled, only rule-based output is produced (and no
    LLM calls are made).

No DB needed.

Run inside Docker:
  docker compose exec backend python -m pytest tests/unit/test_mapqa_paraphraser.py -v
"""

import os
import pytest


class StubLLM:
    """Fail-soft LLM stub: returns queued chat_json responses."""

    def __init__(self, available=True, responses=None):
        self.available = available
        self.responses = list(responses or [])
        self.calls = 0

    def is_available(self):
        return self.available

    def chat_json(self, messages, **kwargs):
        self.calls += 1
        if not self.responses:
            return None
        return self.responses.pop(0)


@pytest.fixture(scope="module")
def paraphraser():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "backend.settings")
    import django
    django.setup()
    from semantic_search.services.mapqa_paraphraser import MapQAParaphraser
    return MapQAParaphraser


class TestRuleBasedSlotInvariance:
    """Every rule-based paraphrase must keep all slot values verbatim."""

    @pytest.mark.parametrize("question,slots", [
        ("Which bars are within 50m of Hollywood Blvd?",
         ["bar", "Hollywood Blvd", "50m"]),
        ("How far is Union Station from downtown LA?",
         ["Union Station", "downtown LA"]),
        ("What is the distance between Union Station and Hollywood Blvd?",
         ["Union Station", "Hollywood Blvd"]),
        ("What amenity is available at Union Station?",
         ["Union Station"]),
        ("What is the nearest restaurant to Union Station?",
         ["restaurant", "Union Station"]),
        ("Which is closer to Union Station, Starbucks or Central Park?",
         ["Union Station", "Starbucks", "Central Park"]),
        ("Which direction is Union Station from downtown LA?",
         ["Union Station", "downtown LA"]),
        ("What is the nearest cafe north of Union Station?",
         ["cafe", "north", "Union Station"]),
        ("What restaurant is right by Hayama?", ["restaurant", "Hayama"]),
    ])
    def test_slots_preserved(self, paraphraser, question, slots):
        p = paraphraser(llm_enabled=False)
        paraphrases = p.paraphrase(question, slots)
        assert paraphrases, f"no rule-based paraphrases for: {question}"
        for para in paraphrases:
            lowered = para.lower()
            for slot in slots:
                assert slot.lower() in lowered, (
                    f"slot {slot!r} lost in paraphrase {para!r} "
                    f"(from {question!r})"
                )

    def test_paraphrases_differ_from_seed(self, paraphraser):
        p = paraphraser(llm_enabled=False)
        question = "Which bars are within 50m of Hollywood Blvd?"
        paraphrases = p.paraphrase(question, ["bar", "Hollywood Blvd", "50m"])
        for para in paraphrases:
            assert para != question
            assert para.lower().strip().rstrip("?") != question.lower().strip().rstrip("?")

    def test_no_duplicates(self, paraphraser):
        p = paraphraser(llm_enabled=False)
        question = "What is the nearest restaurant to Union Station?"
        paraphrases = p.paraphrase(question, ["restaurant", "Union Station"])
        assert len(paraphrases) == len(set(paraphrases))


class TestLLMTier:
    def test_llm_output_validation(self, paraphraser):
        """LLM paraphrases that drop slot values are rejected; valid ones kept."""
        stub = StubLLM(responses=[{
            "paraphrases": [
                "What is the closest restaurant near Union Station?",
                "What is the nearest cafe to Downtown?",  # drops both slots
            ],
        }])
        p = paraphraser(llm_enabled=True, llm_service=stub)
        out = p.paraphrase(
            "What is the nearest restaurant to Union Station?",
            ["restaurant", "Union Station"],
        )
        assert stub.calls == 1
        assert "What is the closest restaurant near Union Station?" in out
        assert "What is the nearest cafe to Downtown?" not in out
        # Rule-based tier still present
        assert "What is the closest restaurant to Union Station?" in out

    def test_llm_unavailable_falls_back_to_rule_based(self, paraphraser):
        stub = StubLLM(available=False)
        p = paraphraser(llm_enabled=True, llm_service=stub)
        out = p.paraphrase(
            "What is the nearest restaurant to Union Station?",
            ["restaurant", "Union Station"],
        )
        assert stub.calls == 0
        assert out, "rule-based tier must survive an unavailable LLM"

    def test_llm_garbage_output_ignored(self, paraphraser):
        stub = StubLLM(responses=[{"not_paraphrases": []}])
        p = paraphraser(llm_enabled=True, llm_service=stub)
        out = p.paraphrase(
            "What is the nearest restaurant to Union Station?",
            ["restaurant", "Union Station"],
        )
        assert out, "rule-based tier must survive garbage LLM output"

    def test_llm_disabled_fallback(self, paraphraser):
        """With the LLM tier off, only rule-based output is produced."""
        stub = StubLLM()
        p = paraphraser(llm_enabled=False, llm_service=stub)
        out = p.paraphrase(
            "What is the nearest restaurant to Union Station?",
            ["restaurant", "Union Station"],
        )
        assert stub.calls == 0, "no LLM calls when the tier is disabled"
        assert out
        # Every output is a rule-based transform (nearest ↔ closest etc.)
        assert all("restaurant" in o and "Union Station" in o for o in out)

    def test_llm_enabled_default_reads_settings(self, paraphraser):
        """Default llm_enabled comes from MAPQA_LLM_AUGMENTATION_ENABLED."""
        from django.conf import settings
        p = paraphraser(llm_service=StubLLM())
        assert p.llm_enabled == bool(
            getattr(settings, "MAPQA_LLM_AUGMENTATION_ENABLED", False)
        )
