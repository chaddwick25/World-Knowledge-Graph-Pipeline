"""
Tests for the continuation-round guards (radius-only re-ask, anchor
qualifier strip).

The 2026-10-11 Ireland run produced the garbled round-2 question
"Which cuisines are within 6 km of Belfast in Dublin?": the widened-radius
generator was applied to a #4 compare question that has no radius to
widen, and the anchor extraction swallowed the original question's
trailing "in Dublin" qualifier after a placeholder substitution left
"Belfast" glued to it. Two guards: the re-ask fires only for radius
questions, and the anchor strips a single capitalized trailing "in <X>"
qualifier. No DB / network required.
"""

from semantic_search.services.research_service import ResearchOrchestratorService


def test_anchor_strips_trailing_capitalized_qualifier():
    assert ResearchOrchestratorService._anchor_from_question(
        "Which cuisines are near Belfast in Dublin?"
    ) == "Belfast"


def test_anchor_regression_plain_radius_question():
    assert ResearchOrchestratorService._anchor_from_question(
        "Which hotels are within 2km of Belize City?"
    ) == "Belize City"


def test_anchor_keeps_lowercase_multiword_tail():
    assert ResearchOrchestratorService._anchor_from_question(
        "Which cafes are near the cafe in the wood?"
    ) == "the cafe in the wood"


def _continuation_for(qtext, status="class_mismatch"):
    records = [{"question": qtext}]
    unmet = [{"slot": "cuisine", "status": status, "question": qtext}]
    return ResearchOrchestratorService._continuation_questions(
        records, unmet, "IE", None,
    )


def test_continuation_skips_non_radius_questions():
    assert _continuation_for(
        "Which cuisines are near the top restaurant in Dublin?"
    ) == []


def test_continuation_widens_radius_questions():
    follow_ups = _continuation_for(
        "Which restaurants are within 2km of Jerk Town?"
    )
    assert len(follow_ups) == 1
    # 10 km minimum (2026-10-12): max(2km * 3, 10 km).
    assert follow_ups[0]["question"] == (
        "Which restaurants are within 10 km of Jerk Town?"
    )


def test_continuation_insight_questions_from_profile():
    # The owner's design (2026-10-12): a fully-met profile earns
    # insight questions built from its own top entities, in the
    # 5-template grammar by construction.
    records = [{
        "question": "Which restaurants are within 10km of Kingston?",
        "slot": "eat", "result_count": 10,
        "brand_counts": {"Island Grill": 5, "Annali's Restaurant": 2},
    }]
    follow_ups = ResearchOrchestratorService._continuation_questions(
        records, [], "JM", None,
    )
    assert [f["question"] for f in follow_ups] == [
        "What amenities are near Island Grill?",
        "How far is Island Grill from Annali's Restaurant?",
    ]


def test_continuation_insights_dedupe_and_cap():
    # An already-asked insight is skipped; the round caps at 3.
    records = [
        {
            "question": "What amenities are near Island Grill?",
            "slot": "eat", "result_count": 20,
            "brand_counts": {"Island Grill": 5, "Moby Dick": 2},
        },
        {
            "question": "Which cafes are within 10km of Kingston?",
            "slot": "eat", "result_count": 0,
            "brand_counts": {},
        },
    ]
    unmet = [{"slot": "eat", "status": "missing",
              "question": "Which cafes are within 10km of Kingston?"}]
    follow_ups = ResearchOrchestratorService._continuation_questions(
        records, unmet, "JM", None,
    )
    assert len(follow_ups) <= 3
    lowered = [f["question"].lower() for f in follow_ups]
    assert "what amenities are near island grill?" not in lowered


def test_addendum_is_deterministic_render():
    # The addendum renders the round-2 Q&As verbatim; the LLM is never
    # called (the invention channel is closed).
    class ExplodingLLM:
        @staticmethod
        def chat_stream(*a, **kw):
            raise AssertionError("addendum must not call the LLM")

        @staticmethod
        def chat(*a, **kw):
            raise AssertionError("addendum must not call the LLM")

    records = [
        {"question": "What amenities are near Island Grill?",
         "answer": "Found 20 places.", "digest": "Island Grill (0m); ..."},
        {"question": "How far is Island Grill from Moby Dick?",
         "answer": "Distance: 0.61 km.", "digest": ""},
    ]
    out = ResearchOrchestratorService._assemble_addendum(
        ExplodingLLM(), "prompt", records, None,
    )
    assert out.startswith("Continuing the plan:")
    assert "Q: What amenities are near Island Grill?" in out
    assert "A: Found 20 places." in out
    assert "A: Distance: 0.61 km." in out


def test_addendum_empty_round_returns_none():
    assert ResearchOrchestratorService._assemble_addendum(
        None, "prompt", [], None,
    ) is None
