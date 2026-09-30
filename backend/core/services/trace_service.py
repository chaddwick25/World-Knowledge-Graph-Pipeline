"""
trace_service.py — unified LLM/run trace adapter (UNIFIED_LLM_TRACE_PLAN.md).

One normalized trace schema, pluggable sinks, fail-soft everywhere:
never raises, never blocks the request path, zero cost when disabled.
The SSE trace spine (user-facing) is untouched; this is the
developer-facing observability layer under it.

Sinks (env TRACE_SINK):
    none      (default) — every call is a no-op
    console   — JSON lines to stdout
    file      — NDJSON append to the path in TRACE_SINK_URL
    langfuse  — HTTP ingestion to {TRACE_SINK_URL}/ingestion (self-hosted
                Langfuse public API, Basic auth public:secret, no SDK —
                the backend is Python 3.8 and modern SDKs dropped it)
    memory    — in-process list (tests)

Emit layers:
    run_trace()   — one trace per run (research / execute_query /
                    research_chat); sets a thread-local trace_id so LLM
                    spans inside the same worker thread attach as children
    begin_span() / end_span() / span() — timed spans (LLM calls)
    point_span()  — zero-duration markers (deterministic stage events,
                    behind TRACE_DETERMINISTIC_STAGES=1)

Events flow through a bounded queue drained by a daemon thread; on
overflow the oldest event is dropped (drop-oldest). Sampling via
TRACE_SAMPLE_RATE (roll happens once per run). Sink failures are logged
(throttled) and never raised into the caller.
"""

import json
import logging
import queue
import random
import threading
import time
import uuid
from datetime import datetime, timezone
from contextlib import contextmanager

from django.conf import settings

logger = logging.getLogger(__name__)

_SINKS = ("none", "console", "file", "langfuse", "memory")

# Bounded queue + daemon flush thread.
_QUEUE = None
_QUEUE_LOCK = threading.Lock()
_FLUSH_THREAD = None

# memory sink (tests) + config cache.
_MEMORY = []
_MEMORY_LOCK = threading.Lock()
_CFG = None
_CFG_LOCK = threading.Lock()

# Sink error logging throttle (seconds).
_SINK_LOG_WINDOW = 30.0
_last_sink_log = {"at": 0.0}

APP_NAME = "worldkg"


class _ThreadLocal(threading.local):
    def __init__(self):
        self.trace_id = None
        self.stack = []


_local = _ThreadLocal()


class _RunCtx:
    """Context object yielded by run_trace()."""

    def __init__(self, trace_id):
        self.trace_id = trace_id


class TraceService:
    """Stateless trace adapter. All methods are safe to call anywhere."""

    # ── Config ────────────────────────────────────────────────────────────

    @classmethod
    def _load_cfg(cls) -> dict:
        sink = (getattr(settings, "TRACE_SINK", "") or "none").strip().lower()
        if sink not in _SINKS:
            sink = "none"
        try:
            sample_rate = float(getattr(settings, "TRACE_SAMPLE_RATE", "") or "1.0")
            sample_rate = max(0.0, min(1.0, sample_rate))
        except (TypeError, ValueError):
            sample_rate = 1.0
        deterministic = str(
            getattr(settings, "TRACE_DETERMINISTIC_STAGES", "0")
        ) not in ("0", "false", "False", "")
        try:
            queue_size = int(getattr(settings, "TRACE_QUEUE_SIZE", "") or "1024")
            queue_size = max(1, queue_size)
        except (TypeError, ValueError):
            queue_size = 1024
        try:
            flush_interval = float(
                getattr(settings, "TRACE_FLUSH_INTERVAL", "") or "2.0"
            )
            flush_interval = max(0.0, flush_interval)
        except (TypeError, ValueError):
            flush_interval = 2.0
        return {
            "sink": sink,
            "sample_rate": sample_rate,
            "deterministic_stages": deterministic,
            "queue_size": queue_size,
            "flush_interval": flush_interval,
            "url": (getattr(settings, "TRACE_SINK_URL", "") or "").strip(),
            "public_key": (getattr(settings, "TRACE_SINK_PUBLIC_KEY", "") or "").strip(),
            "secret_key": (getattr(settings, "TRACE_SINK_SECRET_KEY", "") or "").strip(),
        }

    @classmethod
    def _cfg(cls) -> dict:
        global _CFG
        if _CFG is None:
            with _CFG_LOCK:
                if _CFG is None:
                    _CFG = cls._load_cfg()
        return _CFG

    @classmethod
    def reset(cls) -> None:
        """Clear the cached config, queue, and memory sink. Tests only."""
        global _CFG, _QUEUE, _FLUSH_THREAD
        with _CFG_LOCK:
            _CFG = None
        with _QUEUE_LOCK:
            _QUEUE = None
            _FLUSH_THREAD = None
        with _MEMORY_LOCK:
            del _MEMORY[:]
        _local.trace_id = None
        _local.stack = []

    @classmethod
    def is_enabled(cls) -> bool:
        return cls._cfg()["sink"] != "none"

    @classmethod
    def deterministic_stages_enabled(cls) -> bool:
        return cls._cfg()["deterministic_stages"]

    # ── Queue / flush thread ──────────────────────────────────────────────

    @classmethod
    def _queue(cls):
        global _QUEUE
        if _QUEUE is None:
            with _QUEUE_LOCK:
                if _QUEUE is None:
                    _QUEUE = queue.Queue(maxsize=cls._cfg()["queue_size"])
        return _QUEUE

    @classmethod
    def _ensure_thread(cls) -> None:
        global _FLUSH_THREAD
        # Fast path: the flush thread already exists and is alive — no lock.
        if _FLUSH_THREAD is not None and _FLUSH_THREAD.is_alive():
            return
        with _QUEUE_LOCK:
            if _FLUSH_THREAD is None or not _FLUSH_THREAD.is_alive():
                _FLUSH_THREAD = threading.Thread(
                    target=cls._flush_loop, daemon=True, name="trace-flush",
                )
                _FLUSH_THREAD.start()

    @staticmethod
    def _collect_batch(q, first, interval: float = None, cap: int = 64) -> list:
        """Drain events for up to ``interval`` seconds after the first
        (capped at ``cap``). Fewer, larger sink POSTs instead of one per
        event — the langfuse sink was chatty at ~1 POST/event on spaced
        research runs. ``interval=None`` → the configured
        TRACE_FLUSH_INTERVAL (default 2.0s); 0 → drain immediately.
        """
        if interval is None:
            interval = TraceService._cfg()["flush_interval"]
        batch = [first]
        deadline = time.monotonic() + interval
        while len(batch) < cap:
            try:
                batch.append(q.get(timeout=0.01))
            except queue.Empty:
                if time.monotonic() >= deadline:
                    break
        return batch

    @classmethod
    def _flush_loop(cls) -> None:
        q = cls._queue()
        while True:
            try:
                first = q.get(timeout=1.0)
            except queue.Empty:
                continue
            batch = cls._collect_batch(q, first)
            try:
                cls._send(batch)
            except Exception:  # noqa: BLE001 — observability must never raise
                cls._log_sink_error()

    @classmethod
    def flush(cls) -> None:
        """Drain the queue synchronously. Tests and shutdown hooks."""
        if not cls.is_enabled():
            return
        q = cls._queue()
        batch = []
        while True:
            try:
                batch.append(q.get_nowait())
            except queue.Empty:
                break
        if batch:
            try:
                cls._send(batch)
            except Exception:  # noqa: BLE001 — observability must never raise
                cls._log_sink_error()

    # ── Emit ──────────────────────────────────────────────────────────────

    @classmethod
    def _emit(cls, kind: str, fields: dict) -> None:
        try:
            if not cls.is_enabled():
                return
            event = {"kind": kind}
            event.update(fields)
            if not cls._validate(event):
                return
            if cls._cfg()["sink"] == "memory":
                with _MEMORY_LOCK:
                    _MEMORY.append(event)
                return
            q = cls._queue()
            try:
                q.put_nowait(event)
            except queue.Full:
                # Drop-oldest: make room for the newest event.
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait(event)
                except queue.Full:
                    pass  # still full — drop the new event
            cls._ensure_thread()
        except Exception:  # noqa: BLE001 — observability must never raise
            pass

    @staticmethod
    def _validate(event: dict) -> bool:
        kind = event.get("kind")
        if kind == "trace":
            ok = all(k in event for k in (
                "trace_id", "run_type", "app", "started_at_ms",
            ))
        elif kind == "span":
            ok = all(k in event for k in (
                "trace_id", "span_id", "name", "start_ms", "duration_ms",
                "status",
            ))
        else:
            ok = False
        if not ok:
            logger.warning("TraceService dropped invalid event: %s", kind)
        return ok

    # ── Public API ────────────────────────────────────────────────────────

    @classmethod
    @contextmanager
    def run_trace(cls, run_type: str, trace_id: str = None,
                  metadata: dict = None):
        """One trace per run. Sets the thread-local trace_id for the
        duration, so spans emitted in the same thread attach as children.
        Sampled out or disabled → yields a ctx with trace_id None.
        """
        if not cls.is_enabled():
            yield _RunCtx(None)
            return
        if trace_id is None:
            trace_id = uuid.uuid4().hex
        if random.random() >= cls._cfg()["sample_rate"]:
            yield _RunCtx(None)
            return
        prev_trace = _local.trace_id
        prev_stack = _local.stack
        _local.trace_id = trace_id
        _local.stack = []
        try:
            cls._emit("trace", {
                "trace_id": trace_id,
                "run_type": run_type,
                "app": APP_NAME,
                "started_at_ms": int(time.time() * 1000),
                "metadata": metadata or {},
            })
            yield _RunCtx(trace_id)
        finally:
            _local.trace_id = prev_trace
            _local.stack = prev_stack

    @classmethod
    def begin_span(cls, name: str, attributes: dict = None):
        """Start a timed span attached to the current run trace.
        Returns a token for end_span(), or None (disabled / no active
        trace / sampled out). Never raises.
        """
        try:
            if not cls.is_enabled() or _local.trace_id is None:
                return None
            parent = _local.stack[-1] if _local.stack else None
            token = {
                "span_id": uuid.uuid4().hex,
                "name": name,
                "attributes": dict(attributes or {}),
                "start_ms": int(time.time() * 1000),
                "parent_id": parent,
            }
            _local.stack.append(token["span_id"])
            return token
        except Exception:  # noqa: BLE001
            return None

    @classmethod
    def end_span(cls, token, error: str = None, attributes: dict = None) -> None:
        """Close a span from begin_span(). No-op for None tokens."""
        if token is None:
            return
        try:
            if _local.stack and _local.stack[-1] == token["span_id"]:
                _local.stack.pop()
            attrs = dict(token["attributes"])
            if attributes:
                attrs.update(attributes)
            if error:
                attrs["error"] = error
            cls._emit("span", {
                "trace_id": _local.trace_id,
                "span_id": token["span_id"],
                "parent_id": token["parent_id"],
                "name": token["name"],
                "start_ms": token["start_ms"],
                "duration_ms": int(time.time() * 1000) - token["start_ms"],
                "status": "error" if error else "ok",
                "attributes": attrs,
            })
        except Exception:  # noqa: BLE001 — observability must never raise
            pass

    @classmethod
    @contextmanager
    def span(cls, name: str, attributes: dict = None):
        """Context-manager form of begin_span()/end_span()."""
        token = cls.begin_span(name, attributes)
        try:
            yield
        except Exception:
            cls.end_span(token, error="exception")
            raise
        else:
            cls.end_span(token)

    @classmethod
    def point_span(cls, name: str, attributes: dict = None) -> None:
        """Zero-duration marker attached to the current run trace
        (deterministic stage events)."""
        try:
            if not cls.is_enabled() or _local.trace_id is None:
                return
            parent = _local.stack[-1] if _local.stack else None
            now = int(time.time() * 1000)
            cls._emit("span", {
                "trace_id": _local.trace_id,
                "span_id": uuid.uuid4().hex,
                "parent_id": parent,
                "name": name,
                "start_ms": now,
                "duration_ms": 0,
                "status": "ok",
                "attributes": dict(attributes or {}),
            })
        except Exception:  # noqa: BLE001 — observability must never raise
            pass

    @classmethod
    def stage_event(cls, event: str, payload: dict) -> None:
        """Map orchestrator/executor progress events to point spans,
        behind TRACE_DETERMINISTIC_STAGES=1."""
        try:
            if not cls.deterministic_stages_enabled() or _local.trace_id is None:
                return
            if event == "question":
                cls.point_span(
                    "question:%s" % payload.get("index"),
                    attributes={
                        "template": payload.get("template"),
                        "result_count": payload.get("result_count"),
                        "error": payload.get("error"),
                    },
                )
            elif event == "tool":
                cls.point_span("tool", attributes={
                    "tool": payload.get("tool"),
                    "args": payload.get("args"),
                })
        except Exception:  # noqa: BLE001 — observability must never raise
            pass

    # ── Sinks ─────────────────────────────────────────────────────────────

    @classmethod
    def _send(cls, batch: list) -> None:
        sink = cls._cfg()["sink"]
        if sink == "console":
            for event in batch:
                print(json.dumps(event, default=str, ensure_ascii=False))
        elif sink == "file":
            path = cls._cfg()["url"]
            if not path:
                cls._log_sink_error()
                return
            with open(path, "a", encoding="utf-8") as fh:
                for event in batch:
                    fh.write(json.dumps(event, default=str, ensure_ascii=False) + "\n")
        elif sink == "langfuse":
            cls._send_langfuse(batch)

    @classmethod
    def _send_langfuse(cls, batch: list) -> None:
        import requests

        cfg = cls._cfg()
        base = cfg["url"].rstrip("/")
        if not base:
            cls._log_sink_error()
            return
        auth = (cfg["public_key"], cfg["secret_key"])
        payload = {"batch": cls._langfuse_payloads(batch)}
        resp = requests.post(
            f"{base}/ingestion", json=payload, auth=auth,
            timeout=(2.0, 5.0),
        )
        if resp.status_code >= 400:
            logger.warning(
                "TraceService langfuse HTTP %s: %s",
                resp.status_code, resp.text[:200],
            )

    @staticmethod
    def _iso(ms: int) -> str:
        return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).isoformat()

    @classmethod
    def _langfuse_payloads(cls, batch: list) -> list:
        out = []
        for event in batch:
            if event["kind"] == "trace":
                out.append({
                    "id": uuid.uuid4().hex,
                    "type": "trace-create",
                    "timestamp": cls._iso(event["started_at_ms"]),
                    "body": {
                        "id": event["trace_id"],
                        "name": event["run_type"],
                        "metadata": {
                            "app": event["app"],
                            **event.get("metadata", {}),
                        },
                    },
                })
            else:  # span
                start = cls._iso(event["start_ms"])
                end = cls._iso(event["start_ms"] + event["duration_ms"])
                body = {
                    "id": event["span_id"],
                    "traceId": event["trace_id"],
                    "name": event["name"],
                    "startTime": start,
                    "endTime": end,
                    "metadata": event.get("attributes") or {},
                }
                if event.get("status") == "error":
                    body["level"] = "ERROR"
                out.append({
                    "id": uuid.uuid4().hex,
                    "type": "span-create",
                    "timestamp": start,
                    "body": body,
                })
        return out

    @classmethod
    def _log_sink_error(cls) -> None:
        now = time.monotonic()
        if now - _last_sink_log["at"] >= _SINK_LOG_WINDOW:
            _last_sink_log["at"] = now
            logger.warning(
                "TraceService sink failed (throttled); dropping events",
            )
