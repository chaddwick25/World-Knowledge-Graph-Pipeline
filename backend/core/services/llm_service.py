"""
llm_service.py — Provider-abstracted LLM client for the WorldKG platform.

Talks to a local Ollama (default) or any OpenAI-compatible ``/v1`` endpoint
(DeepSeek cloud, OpenAI, ...) over plain ``requests`` — no SDK dependency
(the backend container is Python 3.8 and modern OpenAI/MCP SDKs have dropped
3.8).

Two API styles:

- ``native`` (default) — Ollama's ``/api/chat`` with ``think: false``.
  qwen3-style models default to thinking mode, and the reasoning trace eats
  the token budget, leaving ``content`` empty on the OpenAI-compatible
  endpoint (which ignores ``think``). The native endpoint honors it, so
  every call returns usable content. ``format: "json"`` is used for
  chat_json so the model emits strict JSON.
- ``openai`` — ``/v1/chat/completions`` (+ ``/v1/embeddings``) for cloud
  providers. Set ``LLM_API_STYLE=openai`` when swapping providers.

Config (env vars, read once per instance):
    LLM_ENABLED      (default "1")  — master switch
    LLM_BASE_URL     (default "http://localhost:11434/v1")  — /v1 may be omitted for native
    LLM_MODEL        (default "qwen3:8b")
    LLM_API_STYLE    (default "native")  — "native" | "openai"
    LLM_TIMEOUT      (default 60.0) — read timeout; the connect timeout stays at
                                      2s so a DOWN model fails fast (and the
                                      availability cache gates callers), but a
                                      COLD model (OLLAMA_KEEP_ALIVE expiry) can
                                      take 5-15s to reload into VRAM before the
                                      first token
    LLM_AVAILABILITY_CACHE_SECONDS (default 15)

Every method is fail-soft: returns ``None`` / ``False`` on any error instead
of raising, so pipeline code never breaks because the local model is down.
Callers MUST fall back to their deterministic path when the LLM is
unavailable.
"""

import json
import logging
import os
import time
from typing import List, Optional

import requests

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "http://localhost:11434/v1"
_DEFAULT_MODEL = "qwen3:8b"
_DEFAULT_TIMEOUT = 60.0
_DEFAULT_AVAILABILITY_CACHE_SECONDS = 15


class LLMService:
    """Singleton fail-soft client for Ollama (native) or OpenAI-compatible APIs."""

    _instance = None

    def __init__(self):
        self.enabled = os.environ.get("LLM_ENABLED", "1") not in ("0", "false", "False", "")
        self.style = (os.environ.get("LLM_API_STYLE") or "native").lower()
        base = (os.environ.get("LLM_BASE_URL") or _DEFAULT_BASE_URL).rstrip("/")
        self.base_url = base
        # Native Ollama endpoints live at /api/* (no /v1 prefix); the OpenAI
        # style keeps the base as given (typically including /v1).
        self.root_url = base[:-3] if base.endswith("/v1") else base
        self.model = os.environ.get("LLM_MODEL") or _DEFAULT_MODEL
        try:
            self.timeout = float(os.environ.get("LLM_TIMEOUT", _DEFAULT_TIMEOUT))
        except (TypeError, ValueError):
            self.timeout = _DEFAULT_TIMEOUT
        try:
            self._availability_cache_seconds = float(
                os.environ.get("LLM_AVAILABILITY_CACHE_SECONDS",
                               _DEFAULT_AVAILABILITY_CACHE_SECONDS)
            )
        except (TypeError, ValueError):
            self._availability_cache_seconds = _DEFAULT_AVAILABILITY_CACHE_SECONDS
        self._available = None
        self._available_at = 0.0
        logger.info(
            "LLMService: enabled=%s style=%s base_url=%s model=%s timeout=%ss",
            self.enabled, self.style, self.base_url, self.model, self.timeout,
        )

    @classmethod
    def get_instance(cls) -> "LLMService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Force a fresh instance on next get_instance() — used by tests."""
        cls._instance = None

    # ── Availability ──────────────────────────────────────────────────────

    def is_available(self) -> bool:
        """Cached liveness probe (GET /api/tags or /v1/models).

        Connection-refused is instant, so when Ollama is down this returns
        False quickly and the result is cached to keep the request path fast.
        """
        if not self.enabled:
            return False
        now = time.monotonic()
        if self._available is not None and \
                now - self._available_at < self._availability_cache_seconds:
            return self._available
        ok = False
        try:
            url = f"{self.root_url}/api/tags" if self.style == "native" \
                else f"{self.base_url}/models"
            resp = requests.get(url, timeout=(1.0, 2.0))
            ok = resp.status_code == 200
        except requests.RequestException:
            ok = False
        self._available = ok
        self._available_at = now
        return ok

    # ── Chat ──────────────────────────────────────────────────────────────

    def chat(self, messages, temperature: float = 0.2,
             max_tokens: int = 512) -> Optional[str]:
        """Chat completion. Returns the text or None (never raises)."""
        if not self.enabled:
            return None
        try:
            if self.style == "native":
                payload = {
                    "model": self.model,
                    "messages": messages,
                    "stream": False,
                    # qwen3 thinking mode eats the token budget on /v1; the
                    # native endpoint honors think:false.
                    "think": False,
                    "options": {
                        "temperature": temperature,
                        "num_predict": max_tokens,
                    },
                }
                resp = requests.post(
                    f"{self.root_url}/api/chat",
                    json=payload,
                    timeout=(2.0, self.timeout),
                )
                if resp.status_code != 200:
                    logger.warning("LLM chat HTTP %s: %s", resp.status_code, resp.text[:200])
                    return None
                data = resp.json()
                content = data.get("message", {}).get("content")
                return (content or "").strip() or None
            else:
                payload = {
                    "model": self.model,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                }
                resp = requests.post(
                    f"{self.base_url}/chat/completions",
                    json=payload,
                    timeout=(2.0, self.timeout),
                )
                if resp.status_code != 200:
                    logger.warning("LLM chat HTTP %s: %s", resp.status_code, resp.text[:200])
                    return None
                data = resp.json()
                content = data["choices"][0]["message"].get("content")
                return (content or "").strip() or None
        except (requests.RequestException, KeyError, IndexError, ValueError) as exc:
            logger.warning("LLM chat failed: %s", exc)
            return None

    def chat_stream(self, messages, temperature: float = 0.2,
                    max_tokens: int = 512):
        """Stream chat completion text deltas (generator).

        Native Ollama ``/api/chat`` with ``stream: true`` (NDJSON lines);
        openai style uses ``/v1/chat/completions`` with ``stream: true``
        (SSE ``data:`` frames). Yields content deltas; on any error yields
        nothing (callers must treat an empty stream as an empty answer).
        """
        if not self.enabled:
            return
        try:
            if self.style == "native":
                payload = {
                    "model": self.model,
                    "messages": messages,
                    "stream": True,
                    "think": False,
                    "options": {
                        "temperature": temperature,
                        "num_predict": max_tokens,
                    },
                }
                resp = requests.post(
                    f"{self.root_url}/api/chat",
                    json=payload,
                    timeout=(2.0, self.timeout),
                    stream=True,
                )
                if resp.status_code != 200:
                    logger.warning("LLM chat_stream HTTP %s", resp.status_code)
                    return
                for line in resp.iter_lines(decode_unicode=True):
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                    except ValueError:
                        continue
                    content = chunk.get("message", {}).get("content")
                    if content:
                        yield content
            else:
                payload = {
                    "model": self.model,
                    "messages": messages,
                    "stream": True,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                }
                resp = requests.post(
                    f"{self.base_url}/chat/completions",
                    json=payload,
                    timeout=(2.0, self.timeout),
                    stream=True,
                )
                if resp.status_code != 200:
                    logger.warning("LLM chat_stream HTTP %s", resp.status_code)
                    return
                for line in resp.iter_lines(decode_unicode=True):
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except ValueError:
                        continue
                    delta = chunk.get("choices", [{}])[0].get("delta", {}).get("content")
                    if delta:
                        yield delta
        except (requests.RequestException, ValueError) as exc:
            logger.warning("LLM chat_stream failed: %s", exc)
            return

    def chat_json(self, messages, temperature: float = 0.0,
                  max_tokens: int = 512) -> Optional[dict]:
        """Chat completion parsed as JSON. Returns dict or None (never raises).

        On the native Ollama path uses ``format: "json"`` so the model emits
        strict JSON; the parser still tolerates markdown fences and prose.
        """
        if not self.enabled:
            return None
        try:
            if self.style == "native":
                payload = {
                    "model": self.model,
                    "messages": messages,
                    "stream": False,
                    "think": False,
                    "format": "json",
                    "options": {
                        "temperature": temperature,
                        "num_predict": max_tokens,
                    },
                }
                resp = requests.post(
                    f"{self.root_url}/api/chat",
                    json=payload,
                    timeout=(2.0, self.timeout),
                )
                if resp.status_code != 200:
                    logger.warning("LLM chat_json HTTP %s", resp.status_code)
                    return None
                data = resp.json()
                text = (data.get("message", {}).get("content") or "").strip()
            else:
                text = self.chat(messages, temperature=temperature,
                                 max_tokens=max_tokens)
        except (requests.RequestException, KeyError, ValueError) as exc:
            logger.warning("LLM chat_json failed: %s", exc)
            return None
        if not text:
            return None
        return self._extract_json(text)

    @staticmethod
    def _extract_json(text: str) -> Optional[dict]:
        """Best-effort JSON-object extraction from an LLM response."""
        if not text:
            return None
        stripped = text.strip()
        if stripped.startswith("```"):
            lines = stripped.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            stripped = "\n".join(lines).strip()
        try:
            return json.loads(stripped)
        except ValueError:
            pass
        start, end = stripped.find("{"), stripped.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(stripped[start:end + 1])
            except ValueError:
                return None
        return None

    # ── Embeddings ────────────────────────────────────────────────────────

    def embed(self, texts: List[str]) -> Optional[list]:
        """Embeddings (native /api/embed or /v1/embeddings). list or None."""
        if not self.enabled:
            return None
        try:
            if self.style == "native":
                resp = requests.post(
                    f"{self.root_url}/api/embed",
                    json={"model": self.model, "input": texts},
                    timeout=(2.0, self.timeout),
                )
                if resp.status_code != 200:
                    logger.warning("LLM embed HTTP %s", resp.status_code)
                    return None
                return resp.json().get("embeddings")
            else:
                resp = requests.post(
                    f"{self.base_url}/embeddings",
                    json={"model": self.model, "input": texts},
                    timeout=(2.0, self.timeout),
                )
                if resp.status_code != 200:
                    logger.warning("LLM embed HTTP %s", resp.status_code)
                    return None
                data = resp.json()
                return [item["embedding"] for item in data["data"]]
        except (requests.RequestException, KeyError, IndexError, ValueError) as exc:
            logger.warning("LLM embed failed: %s", exc)
            return None
