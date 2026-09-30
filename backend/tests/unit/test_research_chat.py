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
                # localhost bypasses PublicAuthGuardMiddleware (testserver is
                # a "public" host in the container's TRUSTED_LOCAL_HOSTS).
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
            "destination": "Belize City", "duration": "2-day",
            "party_size": "2 adults", "budget": "$5000",
            "interests": "exploring",
        })
        with mock.patch.object(LLMService, "get_instance", return_value=fake):
            resp = client.post(
                "/api/nca/research/finalize/",
                data=json.dumps({"messages": [
                    {"role": "user", "content": "plan a 2-day trip to Belize City"},
                    {"role": "assistant", "content": "Who's coming?"},
                    {"role": "user", "content": "2 adults, $5000, exploring"},
                ]}),
                content_type="application/json",
                HTTP_HOST="localhost",
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["source"] == "structured"
        assert "Belize City" in data["brief"]
        assert "for 2 adults" in data["brief"]
        assert "$5000" in data["brief"]

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
