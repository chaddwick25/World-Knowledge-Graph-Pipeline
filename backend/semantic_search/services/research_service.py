"""
research_service.py — Batch research orchestrator.

One big prompt ("plan a 2-day trip to Belize City") decomposes into
parser-ready questions, each executed deterministically through MapQA,
and the orchestrator assembles a final summary grounded in the primary
answers. Adapts the K80 plan's orchestrator role
(docs/plans/later-stages/K80_LLM_MIGRATION_PLAN.md) to this box: the
orchestrator LLM runs on the RTX 2070 (RESEARCH_LLM_*, env-switchable),
the interactive enrichment LLM on the 4070 stays out of the loop.

The loop:
    decompose   — LLM (chat_json) → 4-6 parser-ready questions
    execute     — parser + executor (deterministic, skip_enrichment=True)
    follow-ups  — optional (RESEARCH_FOLLOWUP_TOOLS=1): LLM picks 0-2
                  nameSearch / structuredSearch calls
    assemble    — LLM (chat_stream) → final summary

One LLM is in the loop: the orchestrator. It reasons over deterministic
primary answers; the final summary is the enriched deliverable.

Fail-soft everywhere: a failed question records an error and the loop
continues; LLM unavailability returns the deterministic answers without
a summary.
"""

import json
import logging
import re

from django.conf import settings

logger = logging.getLogger(__name__)

# The parser is trained on exactly these 5 template shapes. The decompose
# prompt constrains the LLM to this vocabulary — the K80 plan's gate: the
# orchestrator must speak the parser's template language, or questions fail
# fast and the loop wastes turns.
TEMPLATE_MANIFEST = {
    "FILTER-AGGREGATE-MEASURE (#1)": (
        '"Which X are within Y of Z?" — radius question with an explicit '
        'distance, e.g. "Which hotels are within 2km of the centre of '
        'Belize City?"'
    ),
    "OBJECT-FIELD-MEASURE (#2)": (
        '"How far is X from Y?" — distance between two named places'
    ),
    "GEOCODE-BATCH-COMPARE (#4)": (
        '"Which is closer to Z: X or Y?" — compare two candidates against '
        "one anchor"
    ),
    "LOCATION-BEARING-CLASSIFY (#5)": (
        '"Which direction is X from Y?" — compass direction between two '
        "named places"
    ),
    "PLACE-ATTRIBUTE-QUERY (#8)": (
        '"What amenities are near X?" — open-ended attribute lookup'
    ),
}

MAX_QUESTIONS = 6

# Anchor qualifiers that break the geocoder. "the centre of Belize City"
# is not a name; the executor's geocode step returns null and the spatial
# filter is skipped. The loop strips the qualifier and retries once.
_ANCHOR_QUALIFIER_RE = re.compile(
    r"\bthe\s+(?:centre|center|downtown|heart)\s+of\s+", re.IGNORECASE,
)

_FOLLOWUP_ENABLED = str(
    getattr(settings, "RESEARCH_FOLLOWUP_TOOLS", "0")
) not in ("0", "false", "False", "")

_DECOMPOSE_SYSTEM_PROMPT = (
    "You are a research planner for a geospatial question-answering system. "
    "Decompose the user's research request into {max_q} self-contained "
    "questions the system's parser can answer. The parser is trained on "
    "exactly these template shapes:\n{manifest}\n"
    "Rules:\n"
    "- One entity class per question (hotels, cafes, restaurants, museums, "
    "beaches, parks, ...).\n"
    "- Radius questions must state an explicit distance in meters or km.\n"
    "- Scale the radius to the anchor's extent: a city-scale anchor "
    "(Dublin, Seoul, Rome) needs at least 2 km, prefer 3-5 km; a venue, "
    "building, or neighbourhood anchor works at 500m-2km. Never use 1 km "
    "for a city.\n"
    "- Anchor every question at a named place from the request, or at a "
    "place named by an earlier question, written as 'the top <class>' "
    "(e.g. 'the top hotel').\n"
    "- Use the plain place name as the anchor, never a qualifier: "
    "'Belize City', not 'the centre of Belize City' or 'downtown "
    "Belize City'.\n"
    "- Each question must be a complete natural-language question, "
    "grammatically valid on its own.\n"
    "- Return STRICT JSON only, no prose: "
    '{{"questions": [{{"question": "...", "why": "..."}}]}}'
)

_FOLLOWUP_SYSTEM_PROMPT = (
    "You enrich research findings by selecting 0-2 additional searches "
    "that fill gaps. Respond with STRICT JSON only: "
    '{"tools": [{"tool": "nameSearch", "args": {"naturalQuery": "..."}}, '
    '{"tool": "structuredSearch", "args": {"queryTags": {"amenity": "cafe"}}}]}. '
    "Select zero tools when the answers already cover the request. No prose."
)

_ASSEMBLE_SYSTEM_PROMPT = (
    "You are a geospatial research assistant. Write a structured summary "
    "that answers the user's research request. Ground EVERY claim in the "
    "provided answers only — never invent entities, counts, distances, "
    "classes, or opening hours. Use the exact entity names from the "
    "answers. Organize the summary into short labeled sections (for a trip "
    "plan: where to stay, where to eat, what to see, getting around). "
    "When an answer is missing or errored, say so instead of guessing. "
    "Never introduce a place, category, count, or distance that does not "
    "appear in the answers. When a question found nothing, write that "
    "nothing was found for that question; do not generalize to nearby "
    "categories or invent alternatives."
)

# The KE interviewer (Knowledge Engineer) — the interactive LLM on the
# 4070. Turns the user's vague idea into a precise research brief. The KE
# ONLY interviews: it never emits the brief itself. The system builds the
# brief via structured extraction (finalize_brief), so free-form model
# output (the runaway place/distance list, observed 2026-09-18) cannot
# reach the run.
KE_SYSTEM_PROMPT = (
    "You are the research interviewer for a geospatial trip-planning "
    "system. Turn the user's idea into a precise research brief.\n"
    "Ask ONE short clarifying question at a time. Gather: destination, "
    "dates or duration, party size, interests, must-sees, constraints "
    "(budget, mobility, pace).\n"
    "If the user's first message already contains enough detail, skip the "
    "questions and say you are ready.\n"
    "When you have enough detail, say you are ready to run the research.\n"
    "Never output a 'BRIEF:' line, a plan, a list of places, or distances. "
    "You only interview; the system builds the brief from your interview.\n"
    "Keep questions short and conversational. Do not repeat the user's "
    "answers back at length."
)

# Structured brief extraction (finalize_brief). chat_json uses
# format: "json" on the native Ollama path, so the reply is constrained
# to a JSON object — the runaway free-form list cannot occur here.
FINALIZE_SYSTEM_PROMPT = (
    "You extract trip-planning facts from a research interview. "
    "Respond with STRICT JSON only, no prose:\n"
    '{"destination": "...", "duration": "...", "party_size": "...", '
    '"budget": "...", "interests": "...", "constraints": "..."}\n'
    "Rules:\n"
    "- Use ONLY facts the user stated. Empty string when not stated.\n"
    "- duration: short form like '2-day'.\n"
    "- party_size: include the noun, e.g. '2 adults'.\n"
    "- interests: a short comma-separated phrase.\n"
    "- constraints: only limits the user stated (mobility, pace, things "
    "to avoid). Empty string when the user stated none.\n"
    "- Never invent places, distances, numbers, or opening hours."
)


class ResearchOrchestratorService:
    """Stateless research orchestrator — the LLM decomposes, we execute."""

    # ── Public API ──────────────────────────────────────────────────────────

    @classmethod
    def plan(cls, prompt: str, country_code: str = None,
             snapshot_date: str = None, event_callback=None) -> dict:
        """Run the full research loop.

        ``event_callback`` (optional) receives progress events:
            {"event": "plan", "questions": [...]}
            {"event": "question", "index", "question", "template",
             "answer", "result_count", "error"?}
            {"event": "tool", "tool", "args"}
            {"event": "tool_out", "tool", "output"}
            {"event": "summary_delta", "delta"}   (token stream)

        Returns:
            {
              "prompt": str,
              "country_code": str,
              "snapshot_date": str,
              "questions": [{question, why, template, confidence, answer,
                             digest, result_count, error?}],
              "tool_calls": [{tool, args, output}],
              "summary": str (None when the LLM is unavailable),
              "errors": [{index, question, error}],
            }
        """
        from core.services.llm_service import LLMService

        llm = LLMService.get_research_instance()
        if not llm.is_available():
            logger.warning("Research orchestrator: LLM unavailable")
            return {
                "prompt": prompt, "country_code": country_code,
                "snapshot_date": snapshot_date, "questions": [],
                "tool_calls": [], "summary": None,
                "errors": [{"error": "research LLM unavailable"}],
            }

        # Phase 1: decompose.
        questions = cls._decompose(llm, prompt, country_code)
        if not questions:
            return {
                "prompt": prompt, "country_code": country_code,
                "snapshot_date": snapshot_date, "questions": [],
                "tool_calls": [], "summary": None,
                "errors": [{"error": "no parser-ready questions decomposed"}],
            }
        if event_callback:
            event_callback({"event": "plan", "questions": questions})

        # Phase 2: execute each question deterministically.
        records, errors, last_top_entity, last_results = cls._execute_questions(
            questions, country_code, snapshot_date, event_callback,
        )

        # Phase 3: optional follow-up tool calls.
        tool_calls = cls._followup_tools(
            llm, prompt, records, last_results, country_code,
            snapshot_date, event_callback,
        )

        # Phase 4: assemble the final summary (the enriched deliverable).
        summary = cls._assemble(
            llm, prompt, records, tool_calls, event_callback,
        )

        return {
            "prompt": prompt,
            "country_code": country_code,
            "snapshot_date": snapshot_date,
            "questions": records,
            "tool_calls": tool_calls,
            "summary": summary,
            "errors": errors,
        }

    # ── KE interviewer chat ───────────────────────────────────────────────

    @staticmethod
    def _message_text(m: dict) -> str:
        """Extract text from a client message.

        Accepts both the AI SDK UIMessage shape (``parts: [{type: "text",
        text: "..."}]`` — what useChat sends) and the plain
        ``{role, content}`` shape. Returns None for empty/unsupported.
        """
        if not isinstance(m, dict):
            return None
        parts = m.get("parts")
        if isinstance(parts, list):
            text = "".join(
                p.get("text", "")
                for p in parts
                if isinstance(p, dict) and p.get("type") == "text"
            ).strip()
            return text or None
        content = m.get("content")
        if isinstance(content, str):
            content = content.strip()
            return content or None
        return None

    @staticmethod
    def chat_messages(messages: list, country_code: str = None,
                      snapshot_date: str = None) -> list:
        """Full message list for the KE interviewer (system + cleaned history).

        ``messages`` is the raw conversation from the client (AI SDK
        UIMessage dicts or plain role/content dicts); only user and
        assistant turns with text survive. The system prompt carries the
        interview rules plus the country and snapshot scope.
        """
        context = (
            f"Country: {country_code or 'unspecified'}. "
            f"Snapshot: {snapshot_date or 'latest'}."
        )
        full = [
            {"role": "system", "content": f"{KE_SYSTEM_PROMPT}\n{context}"},
        ]
        for m in messages or []:
            if not isinstance(m, dict):
                continue
            role = m.get("role")
            text = ResearchOrchestratorService._message_text(m)
            if role in ("user", "assistant") and text:
                full.append({"role": role, "content": text})
        return full

    @classmethod
    def chat(cls, messages: list, country_code: str = None,
             snapshot_date: str = None, on_delta=None) -> str:
        """Stream the KE interviewer reply. Returns the full reply or None.

        The interviewer is the interactive LLM (``LLMService.get_instance``,
        the 4070) — the conversation is the interactive path. ``on_delta``
        receives each content delta for live streaming.
        """
        from core.services.llm_service import LLMService
        llm = LLMService.get_instance()
        if not llm.is_available():
            return None
        full = cls.chat_messages(messages, country_code, snapshot_date)
        parts = []
        for delta in llm.chat_stream(full, temperature=0.4, max_tokens=400):
            if on_delta:
                on_delta(delta)
            parts.append(delta)
        reply = "".join(parts).strip()
        return reply or None

    # ── Brief finalize (structured extraction) ────────────────────────────

    @staticmethod
    def _render_brief(fields: dict) -> str:
        """Render the research brief deterministically from extracted fields.

        The render is bounded and cannot produce the runaway place/distance
        list failure (that came from free-form model output). Returns None
        when no destination is stated.
        """
        f = {
            k: (v or "").strip().replace("\n", " ")[:100]
            for k, v in (fields or {}).items()
            if isinstance(v, str)
        }
        destination = f.get("destination") or ""
        if not destination:
            return None

        duration = f.get("duration") or ""
        parts = []
        if duration:
            parts.append(f"Plan a {duration} trip to {destination}")
        else:
            parts.append(f"Plan a trip to {destination}")
        party = f.get("party_size") or ""
        if party:
            parts.append(f"for {party}")
        budget = f.get("budget") or ""
        if budget:
            parts.append(f"with a {budget} budget")
        brief = " ".join(parts)

        interests = f.get("interests") or ""
        if interests:
            brief += f", focused on {interests}"
        constraints = f.get("constraints") or ""
        if constraints:
            brief += f". Constraints: {constraints}"
        return brief[:400] or None

    @classmethod
    def finalize_brief(cls, messages: list, country_code: str = None,
                       snapshot_date: str = None) -> dict:
        """Extract a structured brief from the interview conversation.

        Returns {"brief", "fields", "source"} or None.
        ``source`` is "structured" (extracted via chat_json + deterministic
        render) or "fallback" (the first user message, when the LLM is down
        or extraction fails). The KE never emits free-form briefs, so the
        runaway place/distance list cannot reach the run.
        """
        from core.services.llm_service import LLMService

        llm = LLMService.get_instance()

        user_texts = [
            cls._message_text(m)
            for m in messages or []
            if isinstance(m, dict) and m.get("role") == "user"
        ]
        user_texts = [t for t in user_texts if t]
        fallback = user_texts[0] if user_texts else None

        if llm.is_available():
            # Last 8 turns, truncated — enough for the facts, keeps the
            # prompt small (and a past runaway reply out of the extraction).
            turns = []
            for m in (messages or [])[-8:]:
                if not isinstance(m, dict):
                    continue
                role = m.get("role")
                text = cls._message_text(m)
                if role in ("user", "assistant") and text:
                    turns.append(f"{role}: {text[:400]}")
            conversation = "\n".join(turns)
            fields = llm.chat_json([
                {"role": "system", "content": FINALIZE_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"Country: {country_code or 'unspecified'}. "
                        f"Snapshot: {snapshot_date or 'latest'}.\n"
                        f"Interview:\n{conversation}\n"
                        "Extract the trip-planning facts as JSON."
                    ),
                },
            ], temperature=0.0, max_tokens=300)
            brief = cls._render_brief(fields)
            if brief:
                return {
                    "brief": brief,
                    "fields": {k: v for k, v in (fields or {}).items()
                               if isinstance(v, str)} or {},
                    "source": "structured",
                }

        if fallback:
            return {"brief": fallback, "fields": {}, "source": "fallback"}
        return None

    # ── Phase 1: decompose ─────────────────────────────────────────────────

    @classmethod
    def _decompose(cls, llm, prompt: str, country_code: str) -> list:
        """LLM decomposes the prompt into parser-ready questions."""
        manifest = "\n".join(
            f"- {name}: {desc}" for name, desc in TEMPLATE_MANIFEST.items()
        )
        decision = llm.chat_json([
            {
                "role": "system",
                "content": _DECOMPOSE_SYSTEM_PROMPT.format(
                    max_q=MAX_QUESTIONS, manifest=manifest,
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Research request: {prompt}\n"
                    f"Country: {country_code or 'unspecified'}\n"
                    "Decompose the request into parser-ready questions."
                ),
            },
        ], temperature=0.0, max_tokens=900)
        questions = cls._validate_questions(decision)
        logger.info(
            "Research decompose: %d questions for %r", len(questions),
            prompt[:60],
        )
        return questions

    @staticmethod
    def _validate_questions(decision) -> list:
        """Validate the decompose JSON → [{question, why}, ...] (max 6)."""
        if not isinstance(decision, dict):
            return []
        raw = decision.get("questions")
        if not isinstance(raw, list):
            return []
        questions = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            q = (item.get("question") or "").strip()
            if len(q) < 8:
                continue
            questions.append({
                "question": q,
                "why": (item.get("why") or "").strip(),
            })
            if len(questions) >= MAX_QUESTIONS:
                break
        return questions

    # ── Phase 2: execute ───────────────────────────────────────────────────

    @classmethod
    def _execute_questions(cls, questions: list, country_code: str,
                           snapshot_date: str, event_callback):
        """Run parser + executor per question. Returns
        (records, errors, last_top_entity, last_results)."""
        from semantic_search.services.query_parser_service import QueryParserService
        from semantic_search.services.query_executor_service import (
            DEFAULT_NEAR_RADIUS_M,
            QueryExecutorService,
        )
        from semantic_search.services.query_enrichment_service import (
            QueryEnrichmentService,
        )

        records = []
        errors = []
        last_top_entity = None
        last_results = []

        for index, item in enumerate(questions):
            qtext = cls._substitute_placeholders(
                item["question"], last_top_entity,
            )
            record = {
                "index": index,
                "question": qtext,
                "why": item.get("why", ""),
            }
            try:
                parsed = QueryParserService.get_instance().parse(qtext)
                template = (parsed or {}).get("template")
                if not template:
                    raise ValueError("parser returned no template")
                result = QueryExecutorService.execute(
                    parsed, country_code, snapshot_date,
                    question=qtext, skip_enrichment=True,
                )
                if result.get("error"):
                    raise ValueError(result["error"])
                results = result.get("results") or []

                # Anchor-qualifier retry: "the centre of Belize City"
                # geocodes to null (spatial_filter_skipped) — strip the
                # qualifier and run once more with the plain place name.
                if cls._anchor_geocode_failed(result):
                    alt = _ANCHOR_QUALIFIER_RE.sub("", qtext).strip()
                    if alt and alt != qtext:
                        logger.info(
                            "Research retry with stripped anchor: %r", alt,
                        )
                        alt_parsed = QueryParserService.get_instance().parse(alt)
                        if (alt_parsed or {}).get("template"):
                            result = QueryExecutorService.execute(
                                alt_parsed, country_code, snapshot_date,
                                question=alt, skip_enrichment=True,
                            )
                            if result.get("error"):
                                raise ValueError(result["error"])
                            results = result.get("results") or []
                            qtext = alt
                            parsed = alt_parsed

                # Small-radius empty escalation: a 1 km question about a
                # city anchor returns nothing ("1 km is too small to search
                # Dublin"), and an empty answer invites the assembler to
                # invent content. When a #1 question with an explicit
                # radius below the open-ended default returns zero results,
                # widen the radius to DEFAULT_NEAR_RADIUS_M and run once.
                if (
                    template == "FILTER-AGGREGATE-MEASURE (#1)"
                    and not cls._anchor_geocode_failed(result)
                    and (len(results) if isinstance(results, list) else 0) == 0
                ):
                    radius_m = cls._parse_radius_m(
                        cls._amount_text(parsed) or qtext,
                    )
                    if radius_m is not None and radius_m < DEFAULT_NEAR_RADIUS_M:
                        widened = cls._widen_radius(qtext, DEFAULT_NEAR_RADIUS_M)
                        if widened and widened != qtext:
                            logger.info(
                                "Research radius escalation: %r -> %r",
                                qtext, widened,
                            )
                            wide_parsed = QueryParserService.get_instance().parse(
                                widened,
                            )
                            if (wide_parsed or {}).get("template"):
                                result = QueryExecutorService.execute(
                                    wide_parsed, country_code, snapshot_date,
                                    question=widened, skip_enrichment=True,
                                )
                                if result.get("error"):
                                    raise ValueError(result["error"])
                                results = result.get("results") or []
                                qtext = widened
                                parsed = wide_parsed
                                record["radius_escalated"] = True

                last_results = results
                record.update({
                    "question": qtext,
                    "template": parsed["template"],
                    "confidence": parsed.get("confidence"),
                    "answer": result.get("answer"),
                    "digest": QueryEnrichmentService._primary_digest(results),
                    "result_count": (
                        len(results) if isinstance(results, list) else 0
                    ),
                })
                # The anchor itself can appear as a result ("belize city"
                # node at 152m) — exclude it so 'the top hotel' resolves to
                # a real hotel, not the anchor. Only overwrite on a hit: an
                # empty question must not wipe the placeholder for the next.
                anchor_text = cls._first_location(parsed)
                top = cls._top_entity_name(results, exclude=anchor_text)
                if top:
                    last_top_entity = top
            except Exception as exc:  # noqa: BLE001 — one bad question must not kill the loop
                logger.warning("Research question %d failed: %s", index, exc)
                record["error"] = str(exc)
                errors.append({
                    "index": index, "question": qtext, "error": str(exc),
                })
            records.append(record)
            if event_callback:
                event_callback({
                    "event": "question",
                    "index": index,
                    "question": qtext,
                    "template": record.get("template"),
                    "answer": record.get("answer"),
                    # Top entity names + classes, for value highlighting
                    # (ResearchPanel renders them dark blue in the answer).
                    "digest": record.get("digest") or "",
                    "result_count": record.get("result_count", 0),
                    "error": record.get("error"),
                    "radius_escalated": record.get("radius_escalated", False),
                })

        return records, errors, last_top_entity, last_results

    @staticmethod
    def _substitute_placeholders(qtext: str, last_top_entity: str) -> str:
        """Replace 'the top <class>' with the prior question's top entity.

        The decompose prompt may emit "Which cafes are within 1km of the
        top hotel?"; the loop substitutes the top hotel's name from the
        previous question's results so the parser sees a named anchor.
        """
        if not last_top_entity:
            return qtext
        return re.sub(
            r"\bthe\s+(?:top|best|nearest|first)\s+\w+",
            last_top_entity, qtext, count=1, flags=re.IGNORECASE,
        )

    @staticmethod
    def _amount_text(parsed: dict) -> str:
        """The parsed AMOUNT concept text (the radius), if any."""
        for c in (parsed or {}).get("concepts") or []:
            if c.get("type") == "AMOUNT" and c.get("text"):
                return c["text"]
        return None

    _RADIUS_TOKEN_RE = re.compile(
        r"\b(\d+(?:\.\d+)?)\s*(km|kilometers?|meters?|m)\b", re.IGNORECASE,
    )

    @classmethod
    def _parse_radius_m(cls, text: str):
        """Parse a radius from text ('1km', '500m') → meters, or None.

        Local to the loop (the executor is patched in tests and its
        ``_parse_radius`` is executor-internal); same token family as
        ``_RADIUS_TOKEN_RE``.
        """
        if not text:
            return None
        m = cls._RADIUS_TOKEN_RE.search(text)
        if not m:
            return None
        value = float(m.group(1))
        unit = m.group(2).lower()
        if unit.startswith("k"):
            return int(value * 1000)
        return int(value)

    @classmethod
    def _widen_radius(cls, qtext: str, radius_m: int) -> str:
        """Rewrite the question's radius token to a fixed radius."""
        target = f"{radius_m / 1000.0:g} km"
        return cls._RADIUS_TOKEN_RE.sub(target, qtext, count=1)

    @staticmethod
    def _top_entity_name(results: list, exclude: str = None) -> str:
        """First named entity in the result list (already ranked).

        ``exclude`` (the question's own anchor text) is skipped so the
        anchor itself never becomes 'the top <class>'.
        """
        exclude_l = (exclude or "").strip().lower()
        for r in results or []:
            name = r.get("name") or (r.get("tags") or {}).get("name")
            if name and name.strip().lower() != exclude_l:
                return name
        return None

    @staticmethod
    def _first_location(parsed: dict) -> str:
        """The parsed LOCATION concept text (the question's anchor)."""
        for c in (parsed or {}).get("concepts") or []:
            if c.get("type") == "LOCATION" and c.get("text"):
                return c["text"]
        return None

    @staticmethod
    def _anchor_geocode_failed(result: dict) -> bool:
        """True when the executor skipped the spatial filter (anchor null)."""
        trace = result.get("trace") or []
        return any(
            isinstance(step, dict) and step.get("step") == "spatial_filter_skipped"
            for step in trace
        )

    # ── Phase 3: follow-up tools ───────────────────────────────────────────

    @classmethod
    def _followup_tools(cls, llm, prompt: str, records: list,
                        last_results: list, country_code: str,
                        snapshot_date: str, event_callback) -> list:
        """Optional LLM-selected nameSearch / structuredSearch follow-ups."""
        if not _FOLLOWUP_ENABLED:
            return []

        from semantic_search.services.query_enrichment_service import (
            RESEARCH_TOOLS,
            QueryEnrichmentService,
        )

        decision = llm.chat_json([
            {"role": "system", "content": _FOLLOWUP_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Research request: {prompt}\n"
                    f"Answers so far:\n{cls._answers_text(records)}\n"
                    f"Available tools: {json.dumps(RESEARCH_TOOLS)}\n"
                    "Select 0-2 follow-up searches that would fill gaps."
                ),
            },
        ], temperature=0.0, max_tokens=300)

        tool_calls = []
        for tool, args in QueryEnrichmentService._validate_tool_decision(decision):
            if event_callback:
                event_callback({"event": "tool", "tool": tool, "args": args})
            output = None
            try:
                output = QueryEnrichmentService._call_search_tool(
                    tool, args, last_results, country_code, snapshot_date,
                )
            except Exception as exc:  # noqa: BLE001 — one bad tool must not kill the loop
                logger.warning("Research tool %s failed: %s", tool, exc)
            tool_calls.append({"tool": tool, "args": args, "output": output})
            if event_callback:
                event_callback({
                    "event": "tool_out", "tool": tool, "output": output,
                })
        return tool_calls

    # ── Phase 4: assemble ──────────────────────────────────────────────────

    @classmethod
    def _assemble(cls, llm, prompt: str, records: list, tool_calls: list,
                  event_callback) -> str:
        """Final summary grounded in the collected primary answers."""
        user_content = (
            f"Research request: {prompt}\n"
            "Answers from the geospatial engine (each is a deterministic "
            "templated answer):\n"
            f"{cls._answers_text(records)}\n"
        )
        if tool_calls:
            tool_lines = []
            for call in tool_calls:
                tool_lines.append(
                    f"- {call['tool']} {json.dumps(call['args'])}: "
                    f"{json.dumps(call.get('output'), ensure_ascii=False)}"
                )
            user_content += "Follow-up tool outputs:\n" + "\n".join(tool_lines)
        user_content += "\nWrite the final summary."

        messages = [
            {"role": "system", "content": _ASSEMBLE_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]

        parts = []
        for delta in llm.chat_stream(messages, temperature=0.3, max_tokens=1200):
            if event_callback:
                event_callback({"event": "summary_delta", "delta": delta})
            parts.append(delta)
        summary = "".join(parts).strip()
        if not summary:
            summary = llm.chat(messages, temperature=0.3, max_tokens=1200)
            summary = (summary or "").strip()
        return summary or None

    # ── Helpers ────────────────────────────────────────────────────────────

    @staticmethod
    def _answers_text(records: list) -> str:
        """Compact per-question lines for the follow-up and assemble prompts."""
        lines = []
        for record in records:
            q = record.get("question", "")
            if record.get("error"):
                lines.append(f"Q: {q}\n  error: {record['error']}")
                continue
            template = record.get("template", "")
            answer = record.get("answer") or ""
            digest = record.get("digest") or ""
            lines.append(
                f"Q: {q}\n  template: {template}\n  answer: {answer}\n"
                f"  top entities: {digest}"
            )
        return "\n".join(lines)
