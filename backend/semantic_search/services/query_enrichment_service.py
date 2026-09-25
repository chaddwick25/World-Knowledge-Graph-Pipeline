"""
query_enrichment_service.py — LLM-driven answer enrichment via tool research.

After the primary template executor returns a terse answer (e.g. "Found 18
entities within 50km."), the platform LLM is FORCED to do research with the
other tools in the toolset — nameSearch / structuredSearch — before
synthesizing the final enriched answer:

    primary result → LLM selects 1-2 research tools → tools execute
    deterministically (the real search endpoint) → LLM synthesizes a
    grounded enriched answer.

If the LLM selects no valid tools, a default research action is derived from
the result set (dominant amenity → structuredSearch, else top name →
nameSearch), so enrichment always includes tool research when the model is
up. Fail-soft: if the LLM is unavailable, an action errors, or synthesis
fails, the caller keeps the primary (templated) answer.

This realizes the "agent does research with the toolset" pattern
(docs/plans/MCP_AGENT_MVP_PLAN.md §7.5) server-side for the MVP: the LLM
picks the tools, execution stays deterministic.

The direct query path (execute-query/stream) no longer uses this loop —
``synthesize()`` synthesizes from pre-fetched entity context
(EntityContextService: USLP links + communities + class distribution) in
a single LLM call (see docs/plans/DIRECT_PATH_ENRICHMENT_PLAN.md).
``enrich()`` is retained for agent-path / multi-tool orchestration.
"""

import hashlib
import json
import logging
import threading
import time
from collections import Counter
from typing import Optional

from django.conf import settings

logger = logging.getLogger(__name__)

# TODO: dig into this code

# ── Enriched-answer cache (in-process, TTL) ─────────────────────────────
# Repeated demo questions (same question + country + snapshot) return the
# cached enriched answer instantly — no LLM calls. In-process only (per
# Django process); the data is static per snapshot so a TTL is a safety
# valve, not correctness.
_CACHE = {}
_CACHE_LOCK = threading.Lock()
_CACHE_DEFAULT_TTL_SECONDS = 1800.0


def _cache_ttl() -> float:
    try:
        return float(
            getattr(settings, "LLM_ANSWER_CACHE_TTL_SECONDS", "")
            or _CACHE_DEFAULT_TTL_SECONDS
        )
    except (TypeError, ValueError):
        return _CACHE_DEFAULT_TTL_SECONDS


def _cache_key(question: str, template: str, country_code: str,
               snapshot_date: str, namespace: str = None) -> str:
    """Cache key with a version + namespace component.

    ``v2|`` invalidates pre-direct-path cache entries (the cached output
    shape changed from research ``action_outputs`` to entity context).
    The namespace separates the two methods — ``enrich()`` (agent path)
    and ``synthesize()`` (direct path) cache different shapes and must
    never share entries for the same question.
    """
    ns = f"{namespace}|" if namespace else ""
    raw = f"v2|{ns}{question}|{template}|{country_code}|{snapshot_date}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _cache_get(key: str):
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if not entry:
            return None
        if time.monotonic() - entry["at"] > _cache_ttl():
            _CACHE.pop(key, None)
            return None
        return entry["value"]


def _cache_put(key: str, value: dict) -> None:
    with _CACHE_LOCK:
        # Keep the cache bounded — drop the oldest entry past a soft cap.
        if len(_CACHE) > 256:
            oldest = min(_CACHE, key=lambda k: _CACHE[k]["at"])
            _CACHE.pop(oldest, None)
        _CACHE[key] = {"at": time.monotonic(), "value": value}

# OSM keys that assert an amenity type (used for the default research action).
AMENITY_KEYS = ("amenity", "shop", "tourism", "leisure", "office", "craft")

# The research tools the LLM may call — the same endpoints the MCP tools and
# the human UI use.
RESEARCH_TOOLS = {
    "nameSearch": "Find entities by name in any language/script (romanizer). "
                  "Args: {countryCode, naturalQuery, topK?}.",
    "structuredSearch": "Find entities by OSM tags. "
                        "Args: {countryCode, queryTags: {key: value}, topK?}.",
}


class QueryEnrichmentService:
    """Stateless enrichment orchestrator — the LLM chooses, we execute."""

    @classmethod
    def enrich(cls, question: str, template: str, concepts: list,
               results: list, country_code: str = None,
               snapshot_date: str = None,
               event_callback=None) -> Optional[dict]:
        """Enrich a primary answer. Returns None (keep primary) on any failure.

        ``event_callback`` (optional) receives progress events:
            {"event": "research", "tool", "args"}
            {"event": "research_out", "tool", "output"}
            {"event": "answer_delta", "delta"}      (token stream, if streaming)
        Cached answers replay the same events without any LLM call.

        Returns:
            {
              "primary_answer": str,
              "actions": [tool names executed],
              "action_outputs": {tool: output},
              "enriched_answer": str,
            }
        """
        if not results or not isinstance(results, list):
            return None

        key = _cache_key(question, template, country_code or "", snapshot_date or "",
                         namespace="enrich")
        cached = _cache_get(key)
        if cached is not None:
            logger.info("LLM enrichment: cache hit for %r", question[:60])
            cls._replay_events(event_callback, cached)
            return cached

        try:
            from core.services.llm_service import LLMService
            llm = LLMService.get_instance()
            if not llm.is_available():
                return None

            primary_summary = cls._primary_summary(template, concepts, results)

            # 1. LLM selects research tools (1-2).
            decision = llm.chat_json([
                {
                    "role": "system",
                    "content": (
                        "You enrich geospatial answers by doing research with "
                        "tools. Respond with STRICT JSON only: "
                        '{"tools": [{"tool": "nameSearch", "args": {"naturalQuery": "..."}}, '
                        '{"tool": "structuredSearch", "args": {"queryTags": {"amenity": "cafe"}}}]}. '
                        "Select 1-2 tools whose results would most enrich the "
                        "primary answer. No prose."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Question: {question}\n"
                        f"Template: {template}\n"
                        f"Primary result: {primary_summary}\n"
                        f"Available tools: {json.dumps(RESEARCH_TOOLS)}\n"
                        "Select research tool calls."
                    ),
                },
            ], temperature=0.0, max_tokens=250)

            actions = cls._validate_tool_decision(decision)

            # 2. FORCE research: if the LLM selected nothing usable, derive a
            #    default action from the result set so the enriched response
            #    is never produced without at least one tool call.
            if not actions:
                actions = [cls._default_research_action(results, country_code)]
                if actions == [None]:
                    logger.info(
                        "LLM enrichment: no research action derivable; keeping primary"
                    )
                    return None
                logger.info("LLM enrichment: forcing default research action %s", actions[0][0])

            # 3. Execute the selected tools deterministically.
            outputs = {}
            for tool, args in actions:
                if event_callback:
                    event_callback({"event": "research", "tool": tool, "args": args})
                try:
                    outputs[tool] = cls._call_search_tool(
                        tool, args, results, country_code, snapshot_date,
                    )
                except Exception as exc:  # noqa: BLE001 — one bad action must not kill enrichment
                    logger.warning("Enrichment action %s failed: %s", tool, exc)
                    outputs[tool] = None
                if event_callback:
                    event_callback({
                        "event": "research_out", "tool": tool, "output": outputs[tool],
                    })

            # 4. LLM synthesizes the enriched answer (token-streamed when a
            #    callback is present so the user sees it arrive live).
            enriched_answer = cls._synthesize(
                llm, question, template, primary_summary, results, outputs,
                stream=event_callback is not None,
                on_delta=(lambda d: event_callback({"event": "answer_delta", "delta": d}))
                if event_callback else None,
            )
            if not enriched_answer:
                return None

            result = {
                "primary_answer": primary_summary,
                "actions": [t for t, _ in actions],
                "action_outputs": {
                    k: v for k, v in outputs.items() if v is not None
                },
                "enriched_answer": enriched_answer,
            }
            _cache_put(key, result)
            return result
        except Exception as exc:  # noqa: BLE001 — enrichment must never break execute
            logger.warning("Answer enrichment failed; keeping primary answer: %s", exc)
            return None

    @classmethod
    def synthesize(cls, question: str, template: str, concepts: list,
                   results: list, context: dict, country_code: str = None,
                   snapshot_date: str = None,
                   event_callback=None) -> Optional[dict]:
        """Synthesize an enriched answer from pre-fetched entity context.

        Single LLM call — no tool selection step. ``context`` is the dict
        produced by EntityContextService.get_context(): relational (USLP
        links), structural (communities), and semantic (class
        distribution) data the synthesis prompt consumes.

        ``event_callback`` receives:
            {"event": "answer_delta", "delta"}   (token stream)
        The ``context`` event itself is emitted by the caller right after
        ``get_context()``, so cache hits replay only answer deltas.

        Returns:
            {
              "primary_answer": str,
              "actions": [context source names with data],
              "action_outputs": {source: data},
              "enriched_answer": str,
            }
        Fail-soft: returns None (keep the primary answer) on any failure.
        """
        if not results or not isinstance(results, list):
            return None

        key = _cache_key(question, template, country_code or "", snapshot_date or "",
                         namespace="synthesize")
        cached = _cache_get(key)
        if cached is not None:
            logger.info("LLM synthesis: cache hit for %r", question[:60])
            cls._replay_synthesize_events(event_callback, cached)
            return cached

        try:
            from core.services.llm_service import LLMService
            llm = LLMService.get_instance()
            if not llm.is_available():
                return None

            primary_summary = cls._primary_summary(template, concepts, results)

            enriched_answer = cls._synthesize(
                llm, question, template, primary_summary, results,
                outputs=None, context=context or {},
                stream=event_callback is not None,
                on_delta=(lambda d: event_callback({"event": "answer_delta", "delta": d}))
                if event_callback else None,
            )
            if not enriched_answer:
                return None

            used_context = {k: v for k, v in (context or {}).items() if v}
            result = {
                "primary_answer": primary_summary,
                "actions": sorted(used_context.keys()),
                "action_outputs": used_context,
                "enriched_answer": enriched_answer,
            }
            _cache_put(key, result)
            return result
        except Exception as exc:  # noqa: BLE001 — synthesis must never break execute
            logger.warning("Answer synthesis failed; keeping primary answer: %s", exc)
            return None

    @staticmethod
    def _replay_synthesize_events(event_callback, cached: dict) -> None:
        """Re-emit answer deltas from a cached synthesis (no LLM calls)."""
        if not event_callback:
            return
        answer = cached.get("enriched_answer") or ""
        # Chunk the cached answer so the client still sees progressive text.
        for i in range(0, len(answer), 32):
            event_callback({"event": "answer_delta", "delta": answer[i:i + 32]})

    @staticmethod
    def _replay_events(event_callback, cached: dict) -> None:
        """Re-emit progress events from a cached result (no LLM calls)."""
        if not event_callback:
            return
        outputs = cached.get("action_outputs") or {}
        for tool in cached.get("actions") or []:
            event_callback({"event": "research", "tool": tool, "args": {}})
            event_callback({"event": "research_out", "tool": tool, "output": outputs.get(tool)})
        answer = cached.get("enriched_answer") or ""
        # Chunk the cached answer so the client still sees progressive text.
        for i in range(0, len(answer), 32):
            event_callback({"event": "answer_delta", "delta": answer[i:i + 32]})

    # ── Helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _format_context_for_prompt(context: dict, sources=None) -> str:
        """Compact the entity-context dict for the synthesis prompt.

        The raw context (up to USLP_LINK_CAP links + per-entity community
        rows) inflated the prompt to ~1.5K tokens and made fresh queries
        50-115s (vs ~7s pre-refactor). The LLM only needs the shape —
        which relations dominate, how entities cluster, and the class
        mix — so render a compact digest here. The raw dict is unchanged
        in the SSE ``context`` event and ``enrichment.action_outputs``.

        ``sources`` is the per-template fetched-source set (from
        EntityContextService.TEMPLATE_CONTEXT_MAP). Sources that were not
        fetched render as "not applicable to this template", never
        "none" — otherwise the LLM claims the area lacks community data
        we never computed.
        """
        key_to_source = {
            "uslp_links": "uslp",
            "communities": "communities",
            "class_distribution": "classes",
        }

        def _applies(key: str) -> bool:
            return sources is None or key_to_source[key] in sources

        lines = []

        if not _applies("uslp_links"):
            lines.append("USLP links: (not applicable to this template)")
        else:
            links = context.get("uslp_links") or []
            if links:
                rel_counts = Counter(
                    l["relation"] for l in links if l.get("relation")
                )
                rel_summary = ", ".join(
                    f"{rel} × {n}" for rel, n in rel_counts.most_common(5)
                )
                lines.append(
                    f"USLP links: {len(links)} total ({rel_summary})"
                )
                for link in links[:8]:
                    lines.append(
                        f"  - {link.get('head_osm_id')} --{link.get('relation')}-> "
                        f"{link.get('tail_osm_id')} "
                        f"(score {link.get('normalized_score')})"
                    )
            else:
                lines.append("USLP links: none")

        if not _applies("communities"):
            lines.append("Communities: (not applicable to this template)")
        else:
            communities = context.get("communities") or {}
            if communities:
                comm_counts = Counter(
                    str(m.get("community")) for m in communities.values()
                    if m.get("community") is not None
                )
                comm_summary = ", ".join(
                    f"community {c} × {n}"
                    for c, n in comm_counts.most_common(8)
                )
                lines.append(
                    f"Communities: {len(communities)} entities ({comm_summary})"
                )
            else:
                lines.append("Communities: none")

        if not _applies("class_distribution"):
            lines.append("Classes: (not applicable to this template)")
        else:
            classes = context.get("class_distribution") or []
            if classes:
                class_summary = ", ".join(
                    f"{c.get('wkg_class')} × {c.get('count')}" for c in classes
                )
                lines.append(f"Classes: {class_summary}")
            else:
                lines.append("Classes: none")

        return "\n".join(lines)

    @staticmethod
    def _primary_digest(results: list) -> str:
        """Compact digest of the primary result set: class counts + top named
        entities with distances. Always grounds the enriched answer, even
        when research tools return nothing useful."""
        lines = []
        counts = Counter((r.get("wkg_class") or "unclassified") for r in results)
        top_classes = counts.most_common(6)
        lines.append(
            "classes: " + ", ".join(f"{c} ({n})" for c, n in top_classes)
        )
        named = []
        for r in results:
            name = r.get("name") or (r.get("tags") or {}).get("name")
            if not name:
                continue
            item = {"name": name}
            if r.get("distance_m") is not None:
                item["distance_m"] = float(r["distance_m"])
            elif r.get("distance_km") is not None:
                item["distance_km"] = float(r["distance_km"])
            named.append(item)
        named.sort(
            key=lambda x: x.get("distance_m") if "distance_m" in x else (
                x.get("distance_km", float("inf"))
            ),
        )
        for item in named[:8]:
            d = item.get("distance_m")
            if d is not None:
                # Show km AND meters so the model cannot mis-compare raw
                # meters against a km-scale radius (seen in production).
                if d >= 1000:
                    lines.append(f"- {item['name']} ({d / 1000:.1f}km / {d:.0f}m)")
                else:
                    lines.append(f"- {item['name']} ({d:.0f}m)")
            else:
                lines.append(f"- {item['name']}")
        return "\n".join(lines)

    @staticmethod
    def _primary_summary(template: str, concepts: list, results: list) -> str:
        radius = ""
        for c in concepts:
            if c.get("type") == "AMOUNT" and c.get("text"):
                radius = f" within {c['text']}"
                break
        return f"Found {len(results)} entities{radius}."

    @staticmethod
    def _validate_tool_decision(decision) -> list:
        """Validate the LLM's tool selection → [(tool, args), ...] (max 2)."""
        if not isinstance(decision, dict):
            return []
        raw = decision.get("tools")
        if not isinstance(raw, list):
            return []
        chosen = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            tool = item.get("tool")
            if tool not in RESEARCH_TOOLS:
                continue
            args = item.get("args") or {}
            if not isinstance(args, dict):
                args = {}
            if tool == "nameSearch" and not args.get("naturalQuery"):
                continue
            if tool == "structuredSearch" and not args.get("queryTags"):
                continue
            chosen.append((tool, args))
            if len(chosen) == 2:
                break
        return chosen

    @staticmethod
    def _default_research_action(results: list, country_code: str):
        """Derive a research action from the result set when the LLM picks none.

        Priority: dominant amenity tag → structuredSearch; else top named
        entity → nameSearch. Returns (tool, args) or None.
        """
        if not country_code:
            return None
        dominant = QueryEnrichmentService._dominant_amenity(results)
        if dominant:
            key, value = dominant
            return ("structuredSearch", {
                "countryCode": country_code,
                "queryTags": {key: value},
                "topK": 5,
            })
        for r in results:
            name = r.get("name") or (r.get("tags") or {}).get("name")
            if name:
                return ("nameSearch", {
                    "countryCode": country_code,
                    "naturalQuery": name,
                    "topK": 5,
                })
        return None

    @classmethod
    def _call_search_tool(cls, tool: str, args: dict, results: list,
                          country_code: str, snapshot_date: str):
        """Execute nameSearch / structuredSearch against the real endpoint.

        Uses Django's test client internally (no HTTP hop) so the research
        tools are literally the same code path as the MCP tools.
        """
        if not country_code:
            return None
        if tool == "nameSearch":
            payload = {
                "country_code": country_code,
                "natural_query": args.get("naturalQuery"),
                "top_k": int(args.get("topK") or 5),
            }
        elif tool == "structuredSearch":
            query_tags = args.get("queryTags") or {}
            if not query_tags:
                dominant = cls._dominant_amenity(results)
                if not dominant:
                    return None
                query_tags = dict([dominant])
            payload = {
                "country_code": country_code,
                "query_tags": query_tags,
                "top_k": int(args.get("topK") or 5),
            }
        else:
            return None
        if snapshot_date:
            payload["snapshot_date"] = snapshot_date

        from django.test import Client
        resp = Client().post(
            "/api/nca/semantic-triplet-search/",
            data=json.dumps(payload),
            content_type="application/json",
            # The test client defaults to host "testserver", which the live
            # backend's ALLOWED_HOSTS rejects (400 HTML response). "localhost"
            # is the same host the browser uses against this endpoint.
            HTTP_HOST="localhost",
        )
        try:
            data = resp.json()
        except ValueError:
            logger.warning(
                "Enrichment search returned non-JSON (status %s)", resp.status_code,
            )
            return None
        if data.get("error"):
            logger.warning("Enrichment search error: %s", data["error"])
            return None
        return [
            {
                "name": (r.get("tags") or {}).get("name")
                or f"{r.get('osm_type', 'osm')}/{r.get('osm_id', '')}",
                "wkg_class": r.get("wkg_class"),
                "lat": r.get("geom", {}).get("lat") if r.get("geom") else None,
                "lon": r.get("geom", {}).get("lon") if r.get("geom") else None,
                "score": (r.get("scores") or {}).get("final_score"),
            }
            for r in (data.get("results") or [])[:5]
        ]

    @staticmethod
    def _dominant_amenity(results: list):
        """Most common (OSM key, value) pair asserting an amenity type."""
        counts = Counter()
        for r in results:
            tags = r.get("tags") or {}
            for k, v in tags.items():
                if k in AMENITY_KEYS and isinstance(v, str) and v:
                    counts[(k, v)] += 1
        return counts.most_common(1)[0][0] if counts else None

    @staticmethod
    def _synthesize(llm, question: str, template: str, primary_summary: str,
                    results: list, outputs: dict, stream: bool = False,
                    on_delta=None, context: dict = None) -> Optional[str]:
        """Ask the LLM for the enriched answer grounded in the PRIMARY
        entities plus either research outputs (``outputs`` — agent path
        ``enrich()``) or pre-fetched entity context (``context`` —
        direct path ``synthesize()``).

        The primary digest (class counts + top named entities) is always
        included — the LLM can name what the executor found even when
        the extra section is empty.

        With ``stream=True`` uses ``llm.chat_stream`` (token deltas relayed
        through ``on_delta``); falls back to ``llm.chat`` when the client
        has no streaming method (tests / non-Ollama).
        """
        if context is not None:
            # Which context sources were actually fetched? Per-template
            # routing (e.g. LOCATION-BEARING-CLASSIFY fetches only
            # classes) — render unfetched sources as "not applicable to
            # this template", never "none", so the LLM doesn't claim the
            # area lacks community data we never computed.
            from semantic_search.services.entity_context_service import (
                TEMPLATE_CONTEXT_MAP,
            )
            sources = TEMPLATE_CONTEXT_MAP.get(template)

            context_lines = [
                QueryEnrichmentService._format_context_for_prompt(
                    context, sources=sources,
                ),
            ]
            extra_label = ("Entity context (USLP links, community "
                           "structure, class distribution):")
        else:
            context_lines = []
            for name, out in (outputs or {}).items():
                if out is None:
                    continue
                context_lines.append(
                    f"- {name}: {json.dumps(out, ensure_ascii=False)}"
                )
            extra_label = "Research tool outputs:"

        system_content = (
            "You are a geospatial reasoning assistant. Write a concise, "
            "informative answer — at most 3 sentences, under 80 words. "
            "Ground EVERY claim in the provided data only — never "
            "invent entities, counts, distances, or classes. Start with "
            "the primary fact, name specific entities from the PRIMARY "
            "ENTITIES section. IMPORTANT: distances are in METERS "
            "unless labeled km — convert before comparing to a "
            "km-scale radius (e.g. 6219m = 6.2km, which IS within "
            "50km). "
        )
        if context is not None:
            system_content += (
                "Use the entity context to explain how the entities "
                "relate, cluster, and what they are."
            )
        else:
            system_content += "Add anything useful from the research outputs."

        messages = [
            {
                "role": "system",
                "content": system_content,
            },
            {
                "role": "user",
                "content": (
                    f"Question: {question}\n"
                    f"Template: {template}\n"
                    f"Primary result: {primary_summary}\n"
                    "Primary entities (from the executor):\n"
                    f"{QueryEnrichmentService._primary_digest(results)}\n"
                    f"{extra_label}\n"
                    + ("\n".join(context_lines) if context_lines else "(none)")
                ),
            },
        ]

        if stream and hasattr(llm, "chat_stream"):
            parts = []
            for delta in llm.chat_stream(messages, temperature=0.3, max_tokens=200):
                if on_delta:
                    on_delta(delta)
                parts.append(delta)
            answer = "".join(parts).strip()
            return answer or None

        answer = llm.chat(messages, temperature=0.3, max_tokens=200)
        answer = (answer or "").strip()
        return answer or None
