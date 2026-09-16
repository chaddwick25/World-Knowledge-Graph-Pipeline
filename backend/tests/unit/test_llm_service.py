"""Unit tests for the platform LLM integration.

Covers:
  - core.services.llm_service: config resolution, native (Ollama) vs openai
    request shapes, fail-soft chat/embed, JSON extraction, availability
  - QueryExecutorService._synthesize_answer: LLM-grounded answers with
    deterministic fallback
  - QueryParserService._llm_refine: low-confidence re-classification with
    validation and heuristic fallback

All LLM HTTP calls are mocked — no Ollama/network required.
"""

import json

import pytest
import requests

from core.services.llm_service import LLMService


# ── Helpers ──────────────────────────────────────────────────────────────

class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload is not None else text)

    def json(self):
        return self._payload


class FakeStreamResponse:
    def __init__(self, status_code=200, lines=()):
        self.status_code = status_code
        self._lines = lines

    def iter_lines(self, decode_unicode=False):
        for line in self._lines:
            yield line


@pytest.fixture(autouse=True)
def _reset_llm(monkeypatch):
    """Every test starts with a fresh singleton and clean env."""
    LLMService.reset_instance()
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("LLM_ENABLED", raising=False)
    monkeypatch.delenv("LLM_TIMEOUT", raising=False)
    monkeypatch.delenv("LLM_API_STYLE", raising=False)
    monkeypatch.delenv("LLM_AVAILABILITY_CACHE_SECONDS", raising=False)
    yield
    LLMService.reset_instance()


class _StubLLM:
    """Minimal stand-in for LLMService used by executor/parser tests."""

    def __init__(self, available=True, answer="LLM answer", chat_json=None):
        self._available = available
        self._answer = answer
        self._chat_json = chat_json
        self.enabled = True

    def is_available(self):
        return self._available

    def chat(self, *args, **kwargs):
        return self._answer

    def chat_json(self, *args, **kwargs):
        return self._chat_json


# ── LLMService: config ───────────────────────────────────────────────────

class TestConfig:
    def test_defaults_native(self):
        svc = LLMService()
        assert svc.enabled is True
        assert svc.style == "native"
        assert svc.base_url == "http://localhost:11434/v1"
        assert svc.root_url == "http://localhost:11434"
        assert svc.model == "qwen3:8b"
        assert svc.timeout == 60.0  # read timeout — cold model reloads take 5-15s

    def test_env_overrides(self, monkeypatch):
        monkeypatch.setenv("LLM_BASE_URL", "http://ollama:11434/v1")
        monkeypatch.setenv("LLM_MODEL", "qwen3:8b")
        monkeypatch.setenv("LLM_ENABLED", "0")
        monkeypatch.setenv("LLM_TIMEOUT", "7.5")
        monkeypatch.setenv("LLM_API_STYLE", "openai")
        svc = LLMService()
        assert svc.enabled is False
        assert svc.style == "openai"
        assert svc.base_url == "http://ollama:11434/v1"
        assert svc.model == "qwen3:8b"
        assert svc.timeout == 7.5

    def test_root_url_without_v1(self, monkeypatch):
        monkeypatch.setenv("LLM_BASE_URL", "http://ollama:11434")
        svc = LLMService()
        assert svc.root_url == "http://ollama:11434"

    def test_singleton(self):
        first = LLMService.get_instance()
        assert first is LLMService.get_instance()  # same instance on repeat call
        LLMService.reset_instance()
        second = LLMService.get_instance()
        assert second is not first  # reset forces a fresh instance


# ── LLMService: chat ─────────────────────────────────────────────────────

class TestChat:
    def test_native_chat_builds_request(self, monkeypatch):
        calls = {}

        def fake_post(url, json=None, timeout=None):
            calls["url"] = url
            calls["json"] = json
            return FakeResponse(200, {"message": {"content": "hello world"}})

        monkeypatch.setattr(requests, "post", fake_post)
        svc = LLMService()
        out = svc.chat([{"role": "user", "content": "hi"}])
        assert out == "hello world"
        assert calls["url"] == "http://localhost:11434/api/chat"
        assert calls["json"]["model"] == "qwen3:8b"
        assert calls["json"]["think"] is False       # thinking disabled
        assert calls["json"]["stream"] is False
        assert calls["json"]["options"]["num_predict"] == 512

    def test_openai_chat_builds_request(self, monkeypatch):
        calls = {}

        def fake_post(url, json=None, timeout=None):
            calls["url"] = url
            calls["json"] = json
            return FakeResponse(200, {
                "choices": [{"message": {"content": "hi there"}}],
            })

        monkeypatch.setattr(requests, "post", fake_post)
        monkeypatch.setenv("LLM_API_STYLE", "openai")
        out = LLMService().chat([{"role": "user", "content": "hi"}])
        assert out == "hi there"
        assert calls["url"] == "http://localhost:11434/v1/chat/completions"
        assert "think" not in calls["json"]

    def test_chat_connection_error_returns_none(self, monkeypatch):
        def boom(*args, **kwargs):
            raise requests.exceptions.ConnectionError("refused")

        monkeypatch.setattr(requests, "post", boom)
        assert LLMService().chat([{"role": "user", "content": "x"}]) is None

    def test_chat_non_200_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            requests, "post",
            lambda *a, **k: FakeResponse(503, None, "server overloaded"),
        )
        assert LLMService().chat([{"role": "user", "content": "x"}]) is None

    def test_chat_empty_content_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            requests, "post",
            lambda *a, **k: FakeResponse(200, {"message": {"content": ""}}),
        )
        assert LLMService().chat([{"role": "user", "content": "x"}]) is None

    def test_chat_disabled_returns_none(self, monkeypatch):
        def should_not_be_called(*args, **kwargs):
            raise AssertionError("chat must not fire when disabled")

        monkeypatch.setattr(requests, "post", should_not_be_called)
        monkeypatch.setenv("LLM_ENABLED", "0")
        assert LLMService().chat([{"role": "user", "content": "x"}]) is None


# ── LLMService: chat_stream ──────────────────────────────────────────────

class TestChatStream:
    def test_native_stream_yields_deltas(self, monkeypatch):
        lines = [
            json.dumps({"message": {"content": "Hello"}}),
            json.dumps({"message": {"content": " world"}}),
            json.dumps({"message": {"content": ""}}),
            json.dumps({"message": {"content": "!"}}),
        ]
        calls = {}

        def fake_post(url, json=None, timeout=None, stream=False):
            calls["stream"] = stream
            calls["body"] = json
            return FakeStreamResponse(200, lines)

        monkeypatch.setattr(requests, "post", fake_post)
        out = "".join(LLMService().chat_stream([{"role": "user", "content": "hi"}]))
        assert out == "Hello world!"
        assert calls["stream"] is True
        assert calls["body"]["think"] is False

    def test_openai_stream_yields_deltas(self, monkeypatch):
        lines = [
            'data: {"choices": [{"delta": {"content": "A"}}]}',
            'data: {"choices": [{"delta": {"content": "B"}}]}',
            "data: [DONE]",
        ]
        monkeypatch.setattr(
            requests, "post",
            lambda *a, **k: FakeStreamResponse(200, lines),
        )
        monkeypatch.setenv("LLM_API_STYLE", "openai")
        out = "".join(LLMService().chat_stream([{"role": "user", "content": "hi"}]))
        assert out == "AB"

    def test_stream_error_yields_nothing(self, monkeypatch):
        def boom(*args, **kwargs):
            raise requests.exceptions.ConnectionError("refused")

        monkeypatch.setattr(requests, "post", boom)
        assert list(LLMService().chat_stream([{"role": "user", "content": "x"}])) == []

    def test_stream_non_200_yields_nothing(self, monkeypatch):
        monkeypatch.setattr(
            requests, "post",
            lambda *a, **k: FakeStreamResponse(503, ["oops"]),
        )
        assert list(LLMService().chat_stream([{"role": "user", "content": "x"}])) == []

    def test_stream_disabled_yields_nothing(self, monkeypatch):
        def should_not_be_called(*args, **kwargs):
            raise AssertionError("must not fire when disabled")

        monkeypatch.setattr(requests, "post", should_not_be_called)
        monkeypatch.setenv("LLM_ENABLED", "0")
        assert list(LLMService().chat_stream([{"role": "user", "content": "x"}])) == []


# ── LLMService: JSON extraction ──────────────────────────────────────────

class TestChatJson:
    def test_native_json_mode(self, monkeypatch):
        calls = {}
        payload = {"template": "FILTER-AGGREGATE-MEASURE (#1)"}
        content = json.dumps(payload)

        def fake_post(url, json=None, timeout=None):
            calls["json"] = json
            return FakeResponse(200, {"message": {"content": content}})

        monkeypatch.setattr(requests, "post", fake_post)
        svc = LLMService()
        assert svc.chat_json([]) == payload
        assert calls["json"]["format"] == "json"      # native JSON mode
        assert calls["json"]["think"] is False

    def test_fenced_json(self):
        assert LLMService._extract_json(
            '```json\n{"a": 1}\n```'
        ) == {"a": 1}

    def test_json_with_prose(self):
        assert LLMService._extract_json(
            'Sure! Here is the result: {"a": 1, "b": [2, 3]} hope that helps'
        ) == {"a": 1, "b": [2, 3]}

    def test_garbage_returns_none(self):
        assert LLMService._extract_json("no json here") is None
        assert LLMService._extract_json("") is None
        assert LLMService._extract_json(None) is None


# ── LLMService: embeddings + availability ────────────────────────────────

class TestEmbedAndAvailability:
    def test_native_embed_request(self, monkeypatch):
        calls = {}

        def fake_post(url, json=None, timeout=None):
            calls["url"] = url
            calls["body"] = json
            return FakeResponse(200, {"embeddings": [[0.1, 0.2], [0.3, 0.4]]})

        monkeypatch.setattr(requests, "post", fake_post)
        out = LLMService().embed(["cafe", "restaurant"])
        assert out == [[0.1, 0.2], [0.3, 0.4]]
        assert calls["url"] == "http://localhost:11434/api/embed"
        assert calls["body"]["input"] == ["cafe", "restaurant"]

    def test_embed_fails_soft(self, monkeypatch):
        monkeypatch.setattr(
            requests, "post",
            lambda *a, **k: FakeResponse(500, None, "boom"),
        )
        assert LLMService().embed(["x"]) is None

    def test_availability_native_cached(self, monkeypatch):
        calls = {"n": 0}

        def fake_get(url, timeout=None):
            calls["n"] += 1
            assert url == "http://localhost:11434/api/tags"
            return FakeResponse(200, {"models": []})

        monkeypatch.setattr(requests, "get", fake_get)
        svc = LLMService()
        assert svc.is_available() is True
        assert svc.is_available() is True  # cached — no second request
        assert calls["n"] == 1

    def test_availability_down_not_cached_long(self, monkeypatch):
        def fake_get(url, timeout=None):
            raise requests.exceptions.ConnectionError("refused")

        monkeypatch.setattr(requests, "get", fake_get)
        monkeypatch.setenv("LLM_AVAILABILITY_CACHE_SECONDS", "0")
        svc = LLMService()
        assert svc.is_available() is False
        assert svc.is_available() is False  # re-probed (cache 0)


# ── Executor: LLM-grounded answer synthesis ──────────────────────────────

class TestSynthesizeAnswer:
    def _call(self, template, results, trace=None, concepts=None):
        from semantic_search.services.query_executor_service import (
            QueryExecutorService,
        )
        return QueryExecutorService._synthesize_answer(
            template, concepts or [], results, trace or [],
        )

    def test_llm_answer_used_when_available(self, monkeypatch):
        monkeypatch.setattr(
            "core.services.llm_service.LLMService.get_instance",
            lambda: _StubLLM(available=True, answer="Three cafes within 50m."),
        )
        results = [{"name": "A", "distance_m": 40.0}]
        assert self._call("FILTER-AGGREGATE-MEASURE (#1)", results) == \
            "Three cafes within 50m."

    def test_falls_back_when_unavailable(self, monkeypatch):
        monkeypatch.setattr(
            "core.services.llm_service.LLMService.get_instance",
            lambda: _StubLLM(available=False),
        )
        results = [{"name": "A"}, {"name": "B"}]
        answer = self._call("FILTER-AGGREGATE-MEASURE (#1)", results)
        assert answer == "Found 2 entities within the specified radius."

    def test_llm_empty_answer_falls_back(self, monkeypatch):
        monkeypatch.setattr(
            "core.services.llm_service.LLMService.get_instance",
            lambda: _StubLLM(available=True, answer="   "),
        )
        results = [{"name": "A"}]
        answer = self._call("OBJECT-FIELD-MEASURE (#2)", results)
        assert answer.startswith("Found 1 results.")  # deterministic path

    def test_no_llm_when_no_results(self, monkeypatch):
        def should_not_be_called(*args, **kwargs):
            raise AssertionError("LLM must not be called with empty results")

        monkeypatch.setattr(
            "core.services.llm_service.LLMService.get_instance", should_not_be_called,
        )
        assert self._call("FILTER-AGGREGATE-MEASURE (#1)", []) == \
            "No results found."

    def test_no_llm_when_error_result(self, monkeypatch):
        def should_not_be_called(*args, **kwargs):
            raise AssertionError("LLM must not be called with error results")

        monkeypatch.setattr(
            "core.services.llm_service.LLMService.get_instance", should_not_be_called,
        )
        answer = self._call("SPECTRAL-ANALYSIS (#11)", {"error": "no data"})
        assert answer == "no data"

    def test_skip_llm_uses_deterministic_path(self, monkeypatch):
        def should_not_be_called(*args, **kwargs):
            raise AssertionError("LLM must not be called when skip_llm=True")

        monkeypatch.setattr(
            "core.services.llm_service.LLMService.get_instance", should_not_be_called,
        )
        results = [{"name": "A"}, {"name": "B"}]
        from semantic_search.services.query_executor_service import (
            QueryExecutorService,
        )
        answer = QueryExecutorService._synthesize_answer(
            "FILTER-AGGREGATE-MEASURE (#1)", [], results, [], skip_llm=True,
        )
        assert answer == "Found 2 entities within the specified radius."


# ── Parser: LLM refinement ───────────────────────────────────────────────

class TestParserRefine:
    @staticmethod
    def _svc_with_labels(templates):
        from sklearn.preprocessing import LabelEncoder
        from semantic_search.services.query_parser_service import (
            QueryParserService,
        )
        svc = QueryParserService.__new__(QueryParserService)  # skip model load
        le = LabelEncoder()
        le.fit(templates)
        svc.label_encoder = le
        return svc

    def test_valid_refine_applied(self, monkeypatch):
        from semantic_search.services.query_parser_service import CONCEPT_TYPES
        svc = self._svc_with_labels([
            "FILTER-AGGREGATE-MEASURE (#1)",
            "OBJECT-FIELD-MEASURE (#2)",
        ])
        monkeypatch.setattr(
            "core.services.llm_service.LLMService.get_instance",
            lambda: _StubLLM(available=True, chat_json={
                "template": "OBJECT-FIELD-MEASURE (#2)",
                "concepts": [
                    {"type": "LOCATION", "text": "Belize City"},
                    {"type": "OBJECT", "text": "cafe"},
                ],
            }),
        )
        out = svc._llm_refine(
            "How far is the nearest cafe in Belize City?",
            "FILTER-AGGREGATE-MEASURE (#1)",
            [{"type": "LOCATION", "text": "How Belize City"}],
        )
        assert out is not None
        new_template, new_concepts = out
        assert new_template == "OBJECT-FIELD-MEASURE (#2)"
        assert new_concepts[0]["type"] == "LOCATION"
        assert new_concepts[0]["text"] == "Belize City"
        assert new_concepts[1]["type"] == "OBJECT"
        assert all(c["type"] in CONCEPT_TYPES for c in new_concepts)

    def test_invalid_template_rejected(self, monkeypatch):
        svc = self._svc_with_labels(["OBJECT-FIELD-MEASURE (#2)"])
        monkeypatch.setattr(
            "core.services.llm_service.LLMService.get_instance",
            lambda: _StubLLM(available=True, chat_json={
                "template": "NOT-A-REAL-TEMPLATE (#99)",
                "concepts": [{"type": "LOCATION", "text": "x"}],
            }),
        )
        assert svc._llm_refine("q", "OBJECT-FIELD-MEASURE (#2)", []) is None

    def test_invalid_concept_type_rejected(self, monkeypatch):
        svc = self._svc_with_labels(["OBJECT-FIELD-MEASURE (#2)"])
        monkeypatch.setattr(
            "core.services.llm_service.LLMService.get_instance",
            lambda: _StubLLM(available=True, chat_json={
                "template": "OBJECT-FIELD-MEASURE (#2)",
                "concepts": [
                    {"type": "NOT-A-CONCEPT", "text": "x"},
                    {"type": "LOCATION", "text": "Belize City"},
                ],
            }),
        )
        out = svc._llm_refine("q", "OBJECT-FIELD-MEASURE (#2)", [])
        assert out is not None
        assert all(c["type"] == "LOCATION" for c in out[1])

    def test_unavailable_llm_returns_none(self, monkeypatch):
        svc = self._svc_with_labels(["OBJECT-FIELD-MEASURE (#2)"])
        monkeypatch.setattr(
            "core.services.llm_service.LLMService.get_instance",
            lambda: _StubLLM(available=False),
        )
        assert svc._llm_refine("q", "OBJECT-FIELD-MEASURE (#2)", []) is None

    def test_garbage_llm_output_returns_none(self, monkeypatch):
        svc = self._svc_with_labels(["OBJECT-FIELD-MEASURE (#2)"])
        monkeypatch.setattr(
            "core.services.llm_service.LLMService.get_instance",
            lambda: _StubLLM(available=True, chat_json=None),  # chat_json → None
        )
        assert svc._llm_refine("q", "OBJECT-FIELD-MEASURE (#2)", []) is None

    def test_threshold_from_env(self, monkeypatch):
        from semantic_search.services.query_parser_service import (
            QueryParserService,
        )
        assert QueryParserService._llm_fallback_threshold() == 0.5
        monkeypatch.setenv("MAPQA_LLM_FALLBACK_CONFIDENCE", "0.8")
        assert QueryParserService._llm_fallback_threshold() == 0.8
