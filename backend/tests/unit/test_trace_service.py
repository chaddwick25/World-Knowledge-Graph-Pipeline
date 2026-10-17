"""Unit tests for the unified LLM trace adapter.

Covers:
  - config resolution: sink, sample rate, deterministic stages flag
  - TraceService: run_trace / begin+end span / point_span / stage_event,
    schema validation, fail-soft (disabled, sink down, no active trace),
    drop-oldest overflow, thread-local propagation
  - LLMService instrumentation: chat / chat_stream / chat_json emit spans
    with gen_ai.* request / usage attributes (OpenTelemetry GenAI names)

No network required (LLM HTTP is mocked; sinks are memory/file).
"""

import asyncio
import json
import queue
import threading
from unittest import mock

import pytest
from asgiref.sync import async_to_sync
from django.conf import settings

import core.services.trace_service as trace_service_module
from core.services.llm_service import LLMService
from core.services.trace_service import TraceService
from semantic_search.services.research_service import ResearchOrchestratorService


def _memory():
    return trace_service_module._MEMORY


@pytest.fixture(autouse=True)
def _clean_trace(monkeypatch):
    """Every test starts with unset TRACE_* settings and fresh singletons."""
    for var in (
        "TRACE_SINK", "TRACE_SINK_URL", "TRACE_SINK_PUBLIC_KEY",
        "TRACE_SINK_SECRET_KEY", "TRACE_SAMPLE_RATE",
        "TRACE_DETERMINISTIC_STAGES", "TRACE_QUEUE_SIZE",
        "TRACE_FLUSH_INTERVAL",
    ):
        monkeypatch.setattr(settings, var, "")
    TraceService.reset()
    LLMService.reset_instance()
    yield
    TraceService.reset()
    LLMService.reset_instance()


def _spans():
    return [e for e in _memory() if e["kind"] == "span"]


def _traces():
    return [e for e in _memory() if e["kind"] == "trace"]


# ── Config ────────────────────────────────────────────────────────────────

class TestConfig:
    def test_default_disabled(self):
        assert TraceService.is_enabled() is False
        assert TraceService.deterministic_stages_enabled() is False

    def test_console_enabled(self, monkeypatch):
        monkeypatch.setattr(settings, "TRACE_SINK", "console")
        assert TraceService.is_enabled() is True

    def test_sample_rate_clamped(self, monkeypatch):
        monkeypatch.setattr(settings, "TRACE_SINK", "memory")
        monkeypatch.setattr(settings, "TRACE_SAMPLE_RATE", "7")
        assert TraceService._cfg()["sample_rate"] == 1.0
        monkeypatch.setattr(settings, "TRACE_SAMPLE_RATE", "-1")
        TraceService.reset()  # env changed mid-test: reload the cached config
        assert TraceService._cfg()["sample_rate"] == 0.0

    def test_deterministic_flag(self, monkeypatch):
        monkeypatch.setattr(settings, "TRACE_DETERMINISTIC_STAGES", "1")
        assert TraceService.deterministic_stages_enabled() is True

    def test_unknown_sink_falls_back_to_none(self, monkeypatch):
        monkeypatch.setattr(settings, "TRACE_SINK", "bogus")
        assert TraceService.is_enabled() is False


# ── TraceService core ─────────────────────────────────────────────────────

class TestTraceService:
    def test_disabled_is_total_noop(self):
        with TraceService.run_trace("research", trace_id="t") as ctx:
            TraceService.point_span("x")
            assert TraceService.begin_span("y") is None
        assert ctx.trace_id is None
        assert _memory() == []

    def test_run_trace_and_spans_parented(self, monkeypatch):
        monkeypatch.setattr(settings, "TRACE_SINK", "memory")
        with TraceService.run_trace("research", trace_id="t1") as ctx:
            assert ctx.trace_id == "t1"
            tok = TraceService.begin_span("decompose")
            inner = TraceService.begin_span("question:0")
            TraceService.end_span(inner)
            TraceService.end_span(tok)
        traces = _traces()
        assert len(traces) == 1
        assert traces[0]["run_type"] == "research"
        assert traces[0]["app"] == "worldkg"
        spans = _spans()
        by_name = {s["name"]: s for s in spans}
        assert set(by_name) == {"decompose", "question:0"}
        assert by_name["decompose"]["trace_id"] == "t1"
        assert by_name["decompose"]["parent_id"] is None
        assert by_name["question:0"]["parent_id"] == by_name["decompose"]["span_id"]
        assert all(s["status"] == "ok" for s in spans)

    def test_span_error_status(self, monkeypatch):
        monkeypatch.setattr(settings, "TRACE_SINK", "memory")
        with TraceService.run_trace("research", trace_id="t-e"):
            tok = TraceService.begin_span("chat")
            TraceService.end_span(tok, error="boom")
        span = _spans()[0]
        assert span["status"] == "error"
        assert span["attributes"]["error"] == "boom"

    def test_span_context_manager_records_exception(self, monkeypatch):
        monkeypatch.setattr(settings, "TRACE_SINK", "memory")
        with TraceService.run_trace("research", trace_id="t-c"):
            with pytest.raises(RuntimeError):
                with TraceService.span("stage"):
                    raise RuntimeError("nope")
        assert _spans()[0]["status"] == "error"

    def test_no_active_trace_skips_spans(self, monkeypatch):
        monkeypatch.setattr(settings, "TRACE_SINK", "memory")
        tok = TraceService.begin_span("orphan")
        assert tok is None
        TraceService.end_span(tok)
        TraceService.point_span("orphan2")
        assert _spans() == []

    def test_sampling_zero_skips_run(self, monkeypatch):
        monkeypatch.setattr(settings, "TRACE_SINK", "memory")
        monkeypatch.setattr(settings, "TRACE_SAMPLE_RATE", "0")
        with TraceService.run_trace("research", trace_id="t-s") as ctx:
            assert ctx.trace_id is None
            TraceService.point_span("x")
        assert _traces() == []
        assert _spans() == []

    def test_thread_local_propagation(self, monkeypatch):
        monkeypatch.setattr(settings, "TRACE_SINK", "memory")
        seen = {}

        def worker():
            with TraceService.run_trace("research", trace_id="t-thread"):
                seen["trace"] = trace_service_module._local.trace_id
                TraceService.point_span("question:0", attributes={"template": "#1"})

        t = threading.Thread(target=worker)
        t.start()
        t.join()
        assert seen["trace"] == "t-thread"
        assert _spans()[0]["trace_id"] == "t-thread"

    def test_stage_event_default_off(self, monkeypatch):
        monkeypatch.setattr(settings, "TRACE_SINK", "memory")
        with TraceService.run_trace("research", trace_id="t-st"):
            TraceService.stage_event("question", {"index": 0, "template": "#1"})
        assert _spans() == []

    def test_stage_event_when_enabled(self, monkeypatch):
        monkeypatch.setattr(settings, "TRACE_SINK", "memory")
        monkeypatch.setattr(settings, "TRACE_DETERMINISTIC_STAGES", "1")
        with TraceService.run_trace("research", trace_id="t-st"):
            TraceService.stage_event(
                "question",
                {"index": 2, "template": "FILTER-AGGREGATE-MEASURE (#1)",
                 "result_count": 5},
            )
            TraceService.stage_event("tool", {"tool": "nameSearch", "args": {}})
        names = [s["name"] for s in _spans()]
        assert names == ["question:2", "tool"]
        assert _spans()[0]["attributes"]["result_count"] == 5

    def test_drop_oldest_on_overflow(self, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "TRACE_SINK", "file")
        monkeypatch.setattr(settings, "TRACE_SINK_URL", str(tmp_path / "trace.ndjson"))
        monkeypatch.setattr(settings, "TRACE_QUEUE_SIZE", "4")
        # No daemon thread: drain deterministically via flush().
        monkeypatch.setattr(TraceService, "_ensure_thread", lambda cls: None)
        with TraceService.run_trace("research", trace_id="t-ovf"):
            for i in range(10):
                TraceService.point_span("s%d" % i)
        TraceService.flush()
        lines = (tmp_path / "trace.ndjson").read_text().splitlines()
        assert len(lines) <= 4
        # The newest event survives; the oldest were dropped.
        assert json.loads(lines[-1])["name"] == "s9"
        names = [json.loads(l)["name"] for l in lines]
        assert "s0" not in names

    def test_sink_down_fail_soft(self, monkeypatch):
        monkeypatch.setattr(settings, "TRACE_SINK", "langfuse")
        monkeypatch.setattr(settings, "TRACE_SINK_URL", "http://127.0.0.1:1/api/public")
        monkeypatch.setattr(settings, "TRACE_SINK_PUBLIC_KEY", "pk")
        monkeypatch.setattr(settings, "TRACE_SINK_SECRET_KEY", "sk")
        monkeypatch.setattr(TraceService, "_ensure_thread", lambda cls: None)
        with TraceService.run_trace("research", trace_id="t-down"):
            TraceService.point_span("x")
        TraceService.flush()  # must not raise

    def test_collect_batch_windows_by_interval_and_cap(self):
        q = queue.Queue()
        for i in range(3):
            q.put_nowait({"n": i})
        # Long interval, small cap: the cap wins, no waiting on a full queue.
        batch = TraceService._collect_batch(q, q.get(), interval=10.0, cap=2)
        assert [b["n"] for b in batch] == [0, 1]
        # Zero interval: drain what is already queued, then stop.
        batch2 = TraceService._collect_batch(q, q.get(), interval=0.0, cap=64)
        assert [b["n"] for b in batch2] == [2]


# ── LLMService instrumentation ────────────────────────────────────────────

class _FakeResp:
    def __init__(self, payload, status_code=200, text=""):
        self._payload = payload
        self.status_code = status_code
        self.text = text

    def json(self):
        return self._payload


class _FakeStreamResp:
    def __init__(self, lines):
        self._lines = lines
        self.status_code = 200

    def iter_lines(self, decode_unicode=False):
        for line in self._lines:
            yield line


class TestLLMInstrumentation:
    def test_chat_emits_span(self, monkeypatch):
        monkeypatch.setattr(settings, "TRACE_SINK", "memory")

        def fake_post(url, **kwargs):
            return _FakeResp({
                "message": {"content": "hello"},
                "prompt_eval_count": 10,
                "eval_count": 5,
            })

        monkeypatch.setattr("core.services.llm_service.requests.post", fake_post)
        llm = LLMService()
        with TraceService.run_trace("execute_query", trace_id="t1"):
            out = llm.chat([{"role": "user", "content": "hi"}])
        assert out == "hello"
        span = _spans()[0]
        assert span["name"] == "chat"
        assert span["trace_id"] == "t1"
        assert span["status"] == "ok"
        assert span["attributes"]["gen_ai.request.model"] == "qwen3:8b"
        assert span["attributes"]["gen_ai.request.temperature"] == 0.2
        assert span["attributes"]["gen_ai.usage.input_tokens"] == 10
        assert span["attributes"]["gen_ai.usage.output_tokens"] == 5

    def test_chat_failure_records_error(self, monkeypatch):
        monkeypatch.setattr(settings, "TRACE_SINK", "memory")

        def fake_post(url, **kwargs):
            return _FakeResp({}, status_code=500)

        monkeypatch.setattr("core.services.llm_service.requests.post", fake_post)
        llm = LLMService()
        with TraceService.run_trace("execute_query", trace_id="t2"):
            assert llm.chat([{"role": "user", "content": "hi"}]) is None
        assert _spans()[0]["status"] == "ok"  # fail-soft: 500 → None, not an exception

    def test_chat_stream_emits_span(self, monkeypatch):
        monkeypatch.setattr(settings, "TRACE_SINK", "memory")

        def fake_post(url, **kwargs):
            return _FakeStreamResp([
                json.dumps({"message": {"content": "a"}}),
                json.dumps({"message": {"content": "b"}}),
            ])

        monkeypatch.setattr("core.services.llm_service.requests.post", fake_post)
        llm = LLMService()
        with TraceService.run_trace("research", trace_id="t3"):
            parts = list(llm.chat_stream([{"role": "user", "content": "hi"}]))
        assert parts == ["a", "b"]
        span = _spans()[0]
        assert span["name"] == "chat_stream"
        assert span["attributes"]["output_chars"] == 2

    def test_chat_json_emits_span(self, monkeypatch):
        monkeypatch.setattr(settings, "TRACE_SINK", "memory")

        def fake_post(url, **kwargs):
            return _FakeResp({
                "message": {"content": '{"questions": []}'},
                "prompt_eval_count": 8,
                "eval_count": 4,
            })

        monkeypatch.setattr("core.services.llm_service.requests.post", fake_post)
        llm = LLMService()
        with TraceService.run_trace("research", trace_id="t4"):
            decision = llm.chat_json([{"role": "user", "content": "x"}])
        assert decision == {"questions": []}
        span = _spans()[0]
        assert span["name"] == "chat_json"
        assert span["attributes"]["gen_ai.usage.input_tokens"] == 8
        assert span["attributes"]["gen_ai.usage.output_tokens"] == 4

    def test_llm_calls_outside_run_trace_not_recorded(self, monkeypatch):
        monkeypatch.setattr(settings, "TRACE_SINK", "memory")

        def fake_post(url, **kwargs):
            return _FakeResp({"message": {"content": "hi"}})

        monkeypatch.setattr("core.services.llm_service.requests.post", fake_post)
        llm = LLMService()
        assert llm.chat([{"role": "user", "content": "hi"}]) == "hi"
        assert _spans() == []


# ── View integration (research_stream emit path) ──────────────────────────

def _stream_body(resp) -> str:
    """Consume an async-generator StreamingHttpResponse body."""
    async def collect(agen):
        chunks = []
        async for chunk in agen:
            chunks.append(chunk)
        return b"".join(chunks)

    return async_to_sync(collect)(resp.streaming_content).decode("utf-8")


@pytest.mark.django_db(transaction=False, databases=["default"])
class TestResearchStreamView:
    """Regression: the emit path must not reference TraceService out of
    scope (NameError observed 2026-09-19 when TRACE_DETERMINISTIC_STAGES=1)."""

    @staticmethod
    def _fake_plan(prompt, country_code, snapshot_date, event_callback=None,
                   subdivision_qid=None):
        if event_callback:
            event_callback({"event": "plan", "questions": [
                {"question": "Which hotels are within 2km of Belize City?"},
            ]})
            event_callback({
                "event": "question", "index": 0,
                "question": "Which hotels are within 2km of Belize City?",
                "template": "FILTER-AGGREGATE-MEASURE (#1)",
                "answer": "Found 1 entities within 2km.",
                "result_count": 1,
            })
        return {
            "prompt": prompt, "country_code": country_code,
            "snapshot_date": snapshot_date,
            "questions": [{"index": 0, "question": "Q?", "template": "#1",
                           "answer": "A", "result_count": 1}],
            "tool_calls": [], "summary": "summary", "errors": [],
        }

    def test_stream_runs_with_deterministic_stages(self, client, monkeypatch):
        monkeypatch.setattr(settings, "TRACE_SINK", "memory")
        monkeypatch.setattr(settings, "TRACE_DETERMINISTIC_STAGES", "1")

        with mock.patch.object(
            ResearchOrchestratorService, "plan", self._fake_plan,
        ):
            resp = client.get(
                "/api/nca/research/stream/",
                {"prompt": "plan a trip to Belize City", "country_code": "BZ"},
                HTTP_HOST="localhost",
            )
        assert resp.status_code == 200
        assert resp["X-Trace-Id"]
        body = _stream_body(resp)
        assert "event: plan" in body
        assert "event: done" in body
        assert resp["X-Trace-Id"] in body  # trace_id surfaced in the done payload

        # stage_event fired from the emit path → question point span recorded.
        names = [s["name"] for s in _spans()]
        assert "question:0" in names
        assert _spans()[0]["trace_id"] == resp["X-Trace-Id"]

    def test_stream_persists_research_run(self, client, monkeypatch):
        """The SSE event log lands on a ResearchRun row: ordered events,
        terminal result, duration, trace_id join key."""
        from core.models import ResearchRun

        monkeypatch.setattr(settings, "TRACE_SINK", "memory")

        with mock.patch.object(
            ResearchOrchestratorService, "plan", self._fake_plan,
        ):
            resp = client.get(
                "/api/nca/research/stream/",
                {"prompt": "plan a trip to Belize City", "country_code": "BZ"},
                HTTP_HOST="localhost",
            )
            # The async generator is lazy — consume inside the patch so
            # the worker thread runs the fake plan, not the real LLM.
            _stream_body(resp)
        run = ResearchRun.objects.get(trace_id=resp["X-Trace-Id"])
        assert run.status == "done"
        assert run.country_code == "BZ"
        assert run.duration_ms is not None
        assert run.finished_at is not None
        assert [e["event"] for e in run.events] == ["plan", "question", "done"]
        assert run.result["summary"] == "summary"
        assert run.result["trace_id"] == resp["X-Trace-Id"]

    def test_stream_persists_error_and_coalesces_deltas(
            self, client, monkeypatch):
        """summary_delta tokens coalesce into one events row; a plan()
        failure records status=error with the message and still
        terminates the stream."""
        from core.models import ResearchRun

        def failing_plan(prompt, country_code, snapshot_date,
                         event_callback=None, subdivision_qid=None):
            if event_callback:
                event_callback({"event": "summary_delta", "delta": "Hel"})
                event_callback({"event": "summary_delta", "delta": "lo"})
            raise RuntimeError("boom")

        with mock.patch.object(
            ResearchOrchestratorService, "plan", failing_plan,
        ):
            resp = client.get(
                "/api/nca/research/stream/",
                {"prompt": "x", "country_code": "BZ"},
                HTTP_HOST="localhost",
            )
            body = _stream_body(resp)
        assert "event: error" in body
        run = ResearchRun.objects.get(trace_id=resp["X-Trace-Id"])
        assert run.status == "error"
        assert "boom" in run.error
        deltas = [e for e in run.events if e["event"] == "summary_delta"]
        assert len(deltas) == 1
        assert deltas[0]["delta"] == "Hello"
        assert run.events[-1]["event"] == "error"


@pytest.mark.django_db(transaction=False, databases=["default"])
class TestResearchRunsEndpoint:
    """Queryable run history: list + detail views over ResearchRun."""

    def _make_run(self, **kw):
        from core.models import ResearchRun

        defaults = {
            "trace_id": "tr-1", "prompt": "find caves",
            "country_code": "JM", "snapshot_date": "2025_12_31",
            "status": "done", "recipe": "doppelganger_report",
            "events": [{"event": "plan", "questions": []}],
            "result": {"summary": "s"}, "duration_ms": 42,
        }
        defaults.update(kw)
        return ResearchRun.objects.create(**defaults)

    def test_list_returns_compact_rows(self, client):
        run = self._make_run()
        resp = client.get("/api/nca/research/runs/", HTTP_HOST="localhost")
        assert resp.status_code == 200
        rows = resp.json()["runs"]
        assert rows[0]["id"] == str(run.id)
        assert rows[0]["trace_id"] == "tr-1"
        assert rows[0]["recipe"] == "doppelganger_report"
        assert "events" not in rows[0]  # compact list; detail carries events

    def test_list_filters_by_country_and_status(self, client):
        self._make_run(trace_id="tr-jm", country_code="JM")
        self._make_run(trace_id="tr-bz", country_code="BZ", status="error")
        resp = client.get(
            "/api/nca/research/runs/", {"country_code": "jm"},
            HTTP_HOST="localhost",
        )
        assert [r["trace_id"] for r in resp.json()["runs"]] == ["tr-jm"]
        resp = client.get(
            "/api/nca/research/runs/", {"status": "error"},
            HTTP_HOST="localhost",
        )
        rows = resp.json()["runs"]
        assert "tr-bz" in [r["trace_id"] for r in rows]
        assert all(r["status"] == "error" for r in rows)

    def test_list_filters_by_trace_id(self, client):
        self._make_run(trace_id="abc")
        self._make_run(trace_id="def")
        resp = client.get(
            "/api/nca/research/runs/", {"trace_id": "abc"},
            HTTP_HOST="localhost",
        )
        assert [r["trace_id"] for r in resp.json()["runs"]] == ["abc"]

    def test_detail_returns_events_and_result(self, client):
        run = self._make_run()
        resp = client.get(
            f"/api/nca/research/runs/{run.id}/", HTTP_HOST="localhost",
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["events"] == [{"event": "plan", "questions": []}]
        assert body["result"] == {"summary": "s"}
        assert body["prompt"] == "find caves"

    def test_detail_404_for_unknown_id(self, client):
        import uuid as _uuid
        resp = client.get(
            f"/api/nca/research/runs/{_uuid.uuid4()}/", HTTP_HOST="localhost",
        )
        assert resp.status_code == 404
