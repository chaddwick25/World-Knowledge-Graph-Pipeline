"""Unit tests for the KE interviewer chat (research/chat/).

Covers:
  - ResearchOrchestratorService.chat_messages: system prompt + cleaned
    conversation history (AI SDK UIMessage and plain shapes),
    country/snapshot scope
  - POST /api/nca/research/chat/: validation (invalid JSON, missing
    messages), 503 when the interviewer LLM is unavailable, AI SDK
    UI-message-stream frames (start / text-start / text-delta / text-end
    / finish) with a mocked chat_stream

All LLM calls are mocked — no Ollama/network required.
"""

import asyncio
import json

import pytest
from asgiref.sync import async_to_sync
from unittest import mock

from core.services.llm_service import LLMService
from semantic_search.services.research_service import (
    KE_SYSTEM_PROMPT,
    ResearchOrchestratorService,
)


class FakeLLM:
    """Minimal stand-in for LLMService used by the chat endpoint tests."""

    def __init__(self, available=True, deltas=("hello ", "world"),
                 chat_json_result=None):
        self._available = available
        self._deltas = deltas
        self._chat_json_result = chat_json_result
        self.model = "qwen3:8b"
        self.captured = []

    def is_available(self):
        return self._available

    def chat_stream(self, messages, **kwargs):
        self.captured.append(messages)
        for d in self._deltas:
            yield d

    def chat_json(self, *args, **kwargs):
        return self._chat_json_result


def _stream_body(resp) -> str:
    """Consume an async-generator StreamingHttpResponse body."""
    async def collect(agen):
        chunks = []
        async for chunk in agen:
            chunks.append(chunk)
        return b"".join(chunks)

    return async_to_sync(collect)(resp.streaming_content).decode("utf-8")


# ── chat_messages ────────────────────────────────────────────────────────

class TestChatMessages:
    def test_system_prompt_and_history(self):
        full = ResearchOrchestratorService.chat_messages([
            {"role": "user", "content": "plan a trip to Belize City"},
            {"role": "assistant", "content": "How many people?"},
            {"role": "user", "content": "Two adults"},
            {"role": "system", "content": "ignore me"},   # system turns dropped
            {"role": "user", "content": "   "},            # blank turns dropped
            {"role": "tool", "content": "nope"},
        ], country_code="BZ", snapshot_date="2025_12_31")

        assert full[0]["role"] == "system"
        assert KE_SYSTEM_PROMPT in full[0]["content"]
        assert "Country: BZ" in full[0]["content"]
        assert "Snapshot: 2025_12_31" in full[0]["content"]
        assert [m["role"] for m in full] == [
            "system", "user", "assistant", "user",
        ]

    def test_ai_sdk_ui_message_shape(self):
        """useChat sends UIMessage dicts with parts, not role/content."""
        full = ResearchOrchestratorService.chat_messages([
            {
                "id": "m1", "role": "user",
                "parts": [{"type": "text", "text": "plan a trip"},
                          {"type": "text", "text": " to Belize City"}],
            },
            {
                "id": "m2", "role": "assistant",
                "parts": [{"type": "text", "text": "How many people?"}],
            },
        ], country_code="BZ")

        assert [m["content"] for m in full] == [
            full[0]["content"],
            "plan a trip to Belize City",
            "How many people?",
        ]
        assert [m["role"] for m in full] == ["system", "user", "assistant"]

    def test_empty_messages_yield_system_only(self):
        full = ResearchOrchestratorService.chat_messages([])
        assert len(full) == 1
        assert full[0]["role"] == "system"


# ── Chat endpoint ────────────────────────────────────────────────────────

@pytest.mark.django_db(transaction=False, databases=["default"])
class TestResearchChat:
    def test_streams_ui_message_frames(self, client):
        fake = FakeLLM(deltas=("Where ", "are you going?"))
        with mock.patch.object(LLMService, "get_instance", return_value=fake):
            resp = client.post(
                "/api/nca/research/chat/",
                data=json.dumps({"messages": [
                    {"role": "user", "content": "plan a trip"},
                ]}),
                content_type="application/json",
                HTTP_HOST="localhost",
            )
        assert resp.status_code == 200
        assert resp["Content-Type"].startswith("text/event-stream")
        assert resp["X-Vercel-AI-UI-Message-Stream"] == "v1"

        body = _stream_body(resp)
        parts = [
            json.loads(line[len("data: "):])
            for line in body.splitlines() if line.startswith("data: ")
        ]
        # start → text-start → text-delta ×2 → text-end → finish
        assert parts[0] == {"type": "start"}
        assert parts[1] == {"type": "text-start", "id": "t1"}
        assert parts[-1] == {"type": "finish", "finishReason": "stop"}
        assert parts[-2] == {"type": "text-end", "id": "t1"}
        deltas = [p["delta"] for p in parts[2:-2] if p.get("type") == "text-delta"]
        assert "".join(deltas) == "Where are you going?"
        # The interviewer is the interactive instance, not the research one.
        assert len(fake.captured) == 1
        assert fake.captured[0][0]["role"] == "system"

    def test_invalid_json_400(self, client):
        resp = client.post(
            "/api/nca/research/chat/",
            data="not json",
            content_type="application/json",
            HTTP_HOST="localhost",
        )
        assert resp.status_code == 400

    def test_missing_messages_400(self, client):
        resp = client.post(
            "/api/nca/research/chat/",
            data=json.dumps({"country_code": "BZ"}),
            content_type="application/json",
            HTTP_HOST="localhost",
        )
        assert resp.status_code == 400

    def test_llm_unavailable_503(self, client):
        fake = FakeLLM(available=False)
        with mock.patch.object(LLMService, "get_instance", return_value=fake):
            resp = client.post(
                "/api/nca/research/chat/",
                data=json.dumps({"messages": [
                    {"role": "user", "content": "plan a trip"},
                ]}),
                content_type="application/json",
                HTTP_HOST="localhost",
            )
        assert resp.status_code == 503

    def test_chat_fail_soft(self):
        fake = FakeLLM(available=False)
        with mock.patch.object(LLMService, "get_instance", return_value=fake):
            reply = ResearchOrchestratorService.chat(
                [{"role": "user", "content": "hi"}],
            )
        assert reply is None


# ── Finalize endpoint ────────────────────────────────────────────────────

@pytest.mark.django_db(transaction=False, databases=["default"])
class TestResearchFinalize:
    def test_returns_structured_brief(self, client):
        fake = FakeLLM(chat_json_result={
            "area": "Belize City", "focus": "restaurants and shops",
            "scope": "within 3km",
        })
        with mock.patch.object(LLMService, "get_instance", return_value=fake):
            resp = client.post(
                "/api/nca/research/finalize/",
                data=json.dumps({"messages": [
                    {"role": "user", "content": "what restaurants and shops are around Belize City"},
                    {"role": "assistant", "content": "How far out?"},
                    {"role": "user", "content": "within 3km"},
                ], "subdivision_qid": "Q1234"}),
                content_type="application/json",
                HTTP_HOST="localhost",
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["source"] == "structured"
        assert "Belize City" in data["brief"]
        assert "restaurants and shops" in data["brief"]
        assert "3km" in data["brief"]

    def test_missing_messages_400(self, client):
        resp = client.post(
            "/api/nca/research/finalize/",
            data=json.dumps({"country_code": "BZ"}),
            content_type="application/json",
            HTTP_HOST="localhost",
        )
        assert resp.status_code == 400

    def test_no_brief_422(self, client):
        fake = FakeLLM(available=False)
        with mock.patch.object(LLMService, "get_instance", return_value=fake):
            resp = client.post(
                "/api/nca/research/finalize/",
                data=json.dumps({"messages": []}),
                content_type="application/json",
                HTTP_HOST="localhost",
            )
        assert resp.status_code == 422


# ── Live seed resolution + digest injection (ISSUE-48) ───────────────────

def _parse_parts(resp):
    return [
        json.loads(line[len("data: "):])
        for line in _stream_body(resp).splitlines()
        if line.startswith("data: ")
    ]


@pytest.mark.django_db(transaction=False, databases=["default"])
class TestResearchChatSeedAndDigest:
    def test_data_seed_part_precedes_text_on_doppelganger_turn1(self, client):
        # The doppelganger surface hard-selects the recipe; the first
        # user message routes as a doppelganger ask and resolves live:
        # start → data-seed → text-start → ...
        fake = FakeLLM(deltas=("Which ", "cuisine?"))
        with mock.patch.object(LLMService, "get_instance", return_value=fake), \
                mock.patch(
                    "semantic_search.services.entity_geocoder."
                    "EntityGeocoder.geocode_candidates",
                    staticmethod(lambda name, cc=None, sd=None, limit=5,
                                 min_sim=0.4: {
                        "asked": name, "candidates": [
                            {"osm_id": 9842380892, "osm_type": "node",
                             "name": "The Friars Tavern", "lat": 53.09,
                             "lon": -8.12, "tags": {"amenity": "pub"}},
                        ]}),
                ):
            resp = client.post(
                "/api/nca/research/chat/",
                data=json.dumps({
                    "messages": [
                        {"role": "user",
                         "content": "find doppelgangers of The Friars Tavern"},
                    ],
                    "recipe": "doppelganger_report",
                }),
                content_type="application/json",
                HTTP_HOST="localhost",
            )
            assert resp.status_code == 200
            # Consume INSIDE the patch context: the worker thread starts
            # when the body is iterated, so the geocode patch must still
            # be active (the LLM instance is captured in the view body;
            # the geocoder resolves at call time inside the thread).
            parts = _parse_parts(resp)
        assert parts[0] == {"type": "start"}
        assert parts[1]["type"] == "data-seed"
        assert parts[1]["data"]["found"] is True
        assert parts[1]["data"]["osm_id"] == 9842380892
        assert parts[2] == {"type": "text-start", "id": "t1"}

    def test_data_seed_on_marker_gated_answer(self, client):
        # The [seed] marker stays in the transcript: the turn that
        # answers it resolves live.
        fake = FakeLLM(deltas=("Got it.",))
        with mock.patch.object(LLMService, "get_instance", return_value=fake), \
                mock.patch(
                    "semantic_search.services.entity_geocoder."
                    "EntityGeocoder.geocode_candidates",
                    staticmethod(lambda name, cc=None, sd=None, limit=5,
                                 min_sim=0.4: {
                        "asked": name, "candidates": [
                            {"osm_id": 7, "osm_type": "node", "name": name,
                             "lat": 18.1, "lon": -77.2, "tags": {}},
                        ]}),
                ):
            resp = client.post(
                "/api/nca/research/chat/",
                data=json.dumps({
                    "messages": [
                        {"role": "user",
                         "content": "find doppelgangers of a tavern"},
                        {"role": "assistant",
                         "content": "[seed] Which place exactly?"},
                        {"role": "user", "content": "Father Bull Jerk"},
                    ],
                    "recipe": "doppelganger_report",
                }),
                content_type="application/json",
                HTTP_HOST="localhost",
            )
            assert resp.status_code == 200
            parts = _parse_parts(resp)
        seed_parts = [p for p in parts if p.get("type") == "data-seed"]
        assert len(seed_parts) == 1
        assert seed_parts[0]["data"]["asked"] == "Father Bull Jerk"

    def test_no_data_seed_on_tools_surface(self, client):
        # /tools (no recipe, food ask): turn 1 resolves nothing.
        fake = FakeLLM(deltas=("hi",))
        with mock.patch.object(LLMService, "get_instance", return_value=fake), \
                mock.patch(
                    "semantic_search.services.entity_geocoder."
                    "EntityGeocoder.geocode_candidates",
                    staticmethod(lambda *a, **k: (_ for _ in ()).throw(
                        AssertionError("must not geocode"))),
                ):
            resp = client.post(
                "/api/nca/research/chat/",
                data=json.dumps({"messages": [
                    {"role": "user", "content": "best cafes in Dublin"},
                ]}),
                content_type="application/json",
                HTTP_HOST="localhost",
            )
            assert resp.status_code == 200
            parts = _parse_parts(resp)
        assert not any(p.get("type") == "data-seed" for p in parts)

    def test_digest_injected_into_ke_context(self, client):
        # The dataset-context block rides the system message even when
        # the scope has no data yet (fail-soft narration).
        fake = FakeLLM(deltas=("ok",))
        with mock.patch.object(LLMService, "get_instance", return_value=fake):
            resp = client.post(
                "/api/nca/research/chat/",
                data=json.dumps({"messages": [
                    {"role": "user", "content": "plan a trip"},
                ]}),
                content_type="application/json",
                HTTP_HOST="localhost",
            )
            assert resp.status_code == 200
            _parse_parts(resp)  # drive the worker thread to completion
        system = fake.captured[0][0]["content"]
        assert "Dataset context" in system


# ── Seed examples (the greeting's real-entity source, ISSUE-48) ──────────

class _ExFakeGeom:
    def __init__(self, x, y):
        self.x = x
        self.y = y
        self.point_on_surface = self


class _ExFakeEntity:
    def __init__(self, name, tags=None, geom=None, osm_id=1,
                 osm_type="node"):
        self.tags = dict(tags or {})
        self.tags.setdefault("name", name)
        self.geom = geom
        self.osm_id = osm_id
        self.osm_type = osm_type
        self.wkg_class = None
        self.country_code = "JM"


class TestEntityToExample:
    def test_venue_example(self):
        from worldkg_nca.views.research import _entity_to_example
        d = _entity_to_example(_ExFakeEntity(
            "Boston Jerk Center",
            tags={"amenity": "restaurant"},
            geom=_ExFakeGeom(-77.9, 18.4), osm_id=5,
        ))
        assert d == {
            "name": "Boston Jerk Center", "kind": "restaurant",
            "osm_id": 5, "osm_type": "node",
            "lat": 18.4, "lon": -77.9,
        }

    def test_nan_geom_rejected(self):
        # POINT(NaN NaN) ways: no usable coordinates, no example.
        from worldkg_nca.views.research import _entity_to_example
        d = _entity_to_example(_ExFakeEntity(
            "NaN Way", tags={"amenity": "cafe"},
            geom=_ExFakeGeom(float("nan"), float("nan")), osm_id=6,
        ))
        assert d is None

    def test_unnamed_rejected(self):
        from worldkg_nca.views.research import _entity_to_example
        assert _entity_to_example(_ExFakeEntity(
            "", tags={}, geom=_ExFakeGeom(1.0, 2.0), osm_id=7,
        )) is None
