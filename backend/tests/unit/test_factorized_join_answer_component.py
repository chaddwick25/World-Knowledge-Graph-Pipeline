"""Unit tests for the deterministic factor-join answer emission.

Hermetic — no DB, no geocoding, no Ollama. Verifies that
``QueryExecutorService.execute()`` emits the raw (non-AI) factor-join
answer as an ``answer`` SSE event before the enrichment phase, so the
client renders it immediately and the LLM enrichment replaces it when
ready:
  - LLM unavailable → ``answer`` event carries the deterministic text,
    ``enrichment`` is None, and the final ``result.answer`` equals the
    emitted text (no flicker at ``done``).
  - LLM available → the early ``answer`` event still carries the
    deterministic text, and the enriched answer wins in the final result.
"""

import pytest

from semantic_search.services.query_executor_service import (
    QueryExecutorService,
)

TEMPLATE = "FILTER-AGGREGATE-MEASURE (#1)"

PARSED = {
    "template": TEMPLATE,
    "concepts": [
        {"type": "OBJECT", "text": "cafe", "confidence": 1.0,
         "resolved_value": None},
    ],
}

# No osm_id/osm_type on purpose: keeps _enrich_results and
# EntityContextService.get_context on their DB-free early paths.
FAKE_RESULTS = [
    {"name": "Alpha", "tags": {"name": "Alpha", "amenity": "cafe"},
     "wkg_class": "wkgs:Cafe", "lat": 1.0, "lon": 2.0},
    {"name": "Beta", "tags": {"name": "Beta", "amenity": "bar"},
     "wkg_class": "wkgs:Bar", "lat": 1.1, "lon": 2.1},
]

EXPECTED_ANSWER = "Found 2 entities within the specified radius."


class _StubLLM:
    def __init__(self, available):
        self._available = available

    def is_available(self):
        return self._available


def _patch_hermetic(monkeypatch):
    """Neutralize every DB/LLM touchpoint in execute()."""
    monkeypatch.setattr(
        QueryExecutorService, "_get_executors",
        classmethod(
            lambda cls, _t=TEMPLATE, _r=FAKE_RESULTS: {_t: (
                lambda concepts, country_code, snapshot_date, trace: list(_r)
            )},
        ),
    )
    monkeypatch.setattr(
        QueryExecutorService, "_get_snapshot_id",
        staticmethod(lambda *a, **k: "2025_12_31"),
    )


def _execute(monkeypatch, events, patch_llm=True, available=False):
    _patch_hermetic(monkeypatch)
    if patch_llm:
        monkeypatch.setattr(
            "core.services.llm_service.LLMService.get_instance",
            lambda: _StubLLM(available),
        )
    return QueryExecutorService.execute(
        PARSED, country_code="XX", snapshot_date="2025_12_31",
        question="Which cafes are nearby?", event_callback=events.append,
    )


def test_answer_event_emitted_before_enrichment_llm_down(monkeypatch):
    """LLM down: the raw answer event fires first and matches the result."""
    events = []
    result = _execute(monkeypatch, events, available=False)

    names = [e["event"] for e in events]
    assert "executed" in names
    assert "answer" in names
    assert "context" in names
    # Deterministic answer must precede the enrichment context phase.
    assert names.index("answer") < names.index("context")

    answer_event = next(e for e in events if e["event"] == "answer")
    assert answer_event["answer"] == EXPECTED_ANSWER

    assert result["enrichment"] is None
    assert result["answer"] == EXPECTED_ANSWER


def test_answer_event_carries_deterministic_text_llm_up(monkeypatch):
    """LLM up: the early event is still the raw join answer; the enriched
    answer wins in the final result."""
    events = []
    monkeypatch.setattr(
        "semantic_search.services.query_enrichment_service."
        "QueryEnrichmentService.synthesize",
        classmethod(
            lambda cls, *a, **k: {"enriched_answer": "AI enriched answer"},
        ),
    )
    result = _execute(monkeypatch, events, patch_llm=False)

    answer_event = next(e for e in events if e["event"] == "answer")
    assert answer_event["answer"] == EXPECTED_ANSWER

    assert result["enrichment"]["enriched_answer"] == "AI enriched answer"
    assert result["answer"] == "AI enriched answer"


def test_no_question_does_not_emit_answer_event(monkeypatch):
    """Without a question the raw answer is not emitted as an event (the
    early-emit is gated on the stream contract); the answer still lands
    in the result."""
    _patch_hermetic(monkeypatch)
    monkeypatch.setattr(
        "core.services.llm_service.LLMService.get_instance",
        lambda: _StubLLM(False),
    )
    events = []
    result = QueryExecutorService.execute(
        PARSED, country_code="XX", snapshot_date="2025_12_31",
        event_callback=events.append,
    )
    assert not [e for e in events if e["event"] == "answer"]
    assert isinstance(result["answer"], str) and result["answer"]
