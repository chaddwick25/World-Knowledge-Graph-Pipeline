"""
research_service.py — Batch research orchestrator.

One big prompt ("how well is Belize City served by public transit?")
decomposes into parser-ready questions, each executed deterministically
through MapQA, and the orchestrator assembles a final summary grounded in
the primary answers. The plan type is a recipe
(``research_recipes.py``): the default is the food & drink FOOD_REPORT
(vendor census, popular-brand tally, local-vs-international cuisine,
distances/clustering between food spots). Adapts the K80 plan's
orchestrator role
(docs/plans/later-stages/K80_LLM_MIGRATION_PLAN.md) to this box: the
orchestrator LLM runs on the RTX 2070 (RESEARCH_LLM_*, env-switchable),
the interactive enrichment LLM on the 4070 stays out of the loop.

The loop:
    decompose   — LLM (chat_json) → 4-6 parser-ready questions, each with
                  a slot id (the plan's criteria); one validation retry
                  feeds the schema error back (RESEARCH_LLM_THINK=1 turns
                  on Qwen3 thinking for the planner)
    execute     — parser + executor (deterministic, skip_enrichment=True)
    repair      — coverage-led gap repair: an empty #1 slot is probed as
                  an open-ended #8, class-swapped to a class the snapshot
                  actually has (zero LLM), then — if still empty —
                  rewritten once by the LLM (RESEARCH_REPLAN_LLM=1,
                  bounded); the probe digest becomes the nearest evidence;
                  the coverage ledger drives the summary
    follow-ups  — optional (RESEARCH_FOLLOWUP_TOOLS=1): LLM picks 0-2
                  nameSearch / structuredSearch calls
    assemble    — LLM (chat_stream, always non-thinking) → final summary
                  organized by the coverage ledger's slots

One LLM is in the loop: the orchestrator. It reasons over deterministic
primary answers; the final summary is the enriched deliverable.

Fail-soft everywhere: a failed question records an error and the loop
continues; LLM unavailability returns the deterministic answers without
a summary.
"""

import json
import logging
import re
from collections import Counter

from django.conf import settings

from semantic_search.services.query_executor_service._constants import (
    CATEGORY_TAG_TARGETS,
    TAG_RULES,
)
from semantic_search.services.research_recipes import (
    DEFAULT_RECIPE_KEY,
    get_recipe,
    route_to_recipe,
)

# (key, value) → natural category token ("natural","peak" → "peak").
# The class-swap re-asks with the natural token so the executor's
# _resolve_tag_target round-trips it to the same tag filter. First
# registration wins: coarse aliases registered later ("tavern" →
# amenity=pub, "club" → amenity=nightclub) must not steal the canonical
# token from the plain category name (2026-10-02).
_TAG_TARGET_TO_TOKEN = {}
for _k, _v in CATEGORY_TAG_TARGETS.items():
    _TAG_TARGET_TO_TOKEN.setdefault(_v, _k)

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

# Follow-up tools default ON (2026-10-01): the orchestrator may pick 0-2
# structuredSearch (OSM tag query) calls per run — the agent's access to
# the tag-query surface. Set RESEARCH_FOLLOWUP_TOOLS=0 to disable.
_FOLLOWUP_ENABLED = str(
    getattr(settings, "RESEARCH_FOLLOWUP_TOOLS", "1")
) not in ("0", "false", "False", "")

# Qwen3 hybrid-thinking routing (RESEARCH_LLM_THINK=1): the planner
# (decompose) thinks when enabled — it is batch work on the 2070, nobody
# waits; the grounded summary assembly is always non-thinking. One env
# switch to A/B the planner's reasoning against today's output.
_RESEARCH_THINK_ENABLED = str(
    getattr(settings, "RESEARCH_LLM_THINK", "0")
) not in ("0", "false", "False", "")

# LLM follow-up replan (default ON, bounded): when the deterministic
# repair (radius widen → class-swap → #8 evidence) still leaves a slot
# empty, one chat_json call rewrites the failing question with a changed
# strategy (different anchor, class, or radius) and it is re-run once.
# Hard caps: one rewrite per slot, MAX_REPLAN_REWRITES per run — the 2070
# budget stays bounded.
_REPLAN_LLM_ENABLED = str(
    getattr(settings, "RESEARCH_REPLAN_LLM", "1")
) not in ("0", "false", "False", "")
MAX_REPLAN_REWRITES = 2

# Post-summary continuation rounds (2026-09-30): unmet slots (missing /
# missing_with_evidence / class_mismatch) drive a bounded round-2 pass of
# deterministic follow-up questions, then an addendum extends the summary.
# One extra round keeps the 2070 latency bounded; the follow-ups are
# deterministic (wider radius / concrete adventure targets), so the only
# new LLM cost is the addendum stream.
MAX_CONTINUATION_ROUNDS = 1

_ADDENDUM_SYSTEM_PROMPT = (
    "You extend a geospatial research summary with a continuation round. "
    "Write a short addendum that starts with 'Continuing the plan' and "
    "covers ONLY the follow-up questions and answers in the provided "
    "ledger. State what each follow-up found; when a follow-up still "
    "found nothing, say so and name the closest evidence from its answer. "
    "Ground every claim in the provided answers only. NEVER invent a "
    "follow-up question, a template name, an entity count, a class, or a "
    "distance — if the ledger has no entry for a topic, do not write "
    "about it, and never reproduce a question that is not in the provided "
    "list (2026-10-01)."
)

_REPLAN_SYSTEM_PROMPT = (
    "You rewrite ONE research question that returned no results so it can "
    "be re-run. Change the strategy: try a different anchor place, a "
    "broader or different food/drink vendor class (prefer one that exists "
    "nearby per the class counts), or a larger radius. One entity class per "
    "question; anchor at a named place; radius questions state an "
    "explicit distance in meters or km (city anchors need at least 2 km). "
    "The rewrite MUST stay inside the parser's template vocabulary: a "
    "radius question (#1), a distance between two NAMED places (#2), a "
    "nearest/compare (#4), a direction (#5), or an open-ended "
    "place-attribute lookup (#8). Never write an open-ended 'what is the "
    "significance or meaning of X' question — the parser cannot answer "
    "it (2026-10-01). Return STRICT JSON only: {\"question\": \"...\"}"
)

# Country → primary hub city. The deterministic re-anchor fallback: when a
# question's anchor fails to geocode (an interest-derived place like
# "Jerk Town" that no snapshot entity carries), the loop re-anchors once at
# the country's hub instead of leaving the slot unanswerable. Unknown
# countries get no fallback (existing behavior).
COUNTRY_HUBS = {
    "JM": "Kingston", "BZ": "Belize City", "IE": "Dublin", "GB": "London",
    "US": "Washington", "CA": "Ottawa", "MX": "Mexico City",
    "BR": "Rio de Janeiro", "AR": "Buenos Aires", "CO": "Bogota",
    "PE": "Lima", "CL": "Santiago", "TH": "Bangkok", "VN": "Ho Chi Minh City",
    "ID": "Jakarta", "MY": "Kuala Lumpur", "PH": "Manila", "SG": "Singapore",
    "IN": "New Delhi", "JP": "Tokyo", "KR": "Seoul", "TW": "Taipei",
    "HK": "Hong Kong", "CN": "Shanghai", "AU": "Sydney", "NZ": "Auckland",
    "DE": "Berlin", "FR": "Paris", "IT": "Rome", "ES": "Barcelona",
    "PT": "Lisbon", "NL": "Amsterdam", "GR": "Athens", "TR": "Istanbul",
    "EG": "Cairo", "ZA": "Cape Town", "KE": "Nairobi", "NG": "Lagos",
    "MA": "Marrakesh", "DO": "Santo Domingo", "CR": "San Jose",
    "PA": "Panama City", "CU": "Havana", "BS": "Nassau", "BB": "Bridgetown",
}

# Country → local cuisine tag tokens (food report, 2026-10-02). The
# derived cuisine tally marks values matching these as "local", the rest
# "other"; unknown/missing cuisine tags are neither.
COUNTRY_CUISINE = {
    "BZ": ("belizean", "caribbean"),
    "CU": ("cuban", "caribbean"),
    "CV": ("cape verdean", "cape_verde", "portuguese"),
    "CY": ("cypriot", "greek", "mediterranean"),
    "GT": ("guatemalan", "latin", "central_american"),
    "IE": ("irish",),
    "IS": ("icelandic",),
    "IT": ("italian",),
    "JM": ("jamaican", "jerk", "caribbean"),
    "KR": ("korean",),
    "LK": ("sri_lankan", "sri lankan"),
    "MA": ("moroccan", "tagine"),
    "MC": ("french", "mediterranean"),
    "MX": ("mexican",),
    "NI": ("nicaraguan", "latin", "central_american"),
    "NL": ("dutch",),
}

# Intent word → candidate wkgs: class substrings for class-aware entity
# selection and the class-swap repair — food & drink vendors only
# (2026-10-02: the food report dropped shops/services/transit/nature
# classes). Matched with a trailing word boundary (see _class_matches) so
# "pub" cannot match wkgs:Public_transport.
_CLASS_ALIASES = {
    "cafe": ("cafe",),
    "restaurant": ("restaurant",),
    "bar": ("bar", "pub"),
    "pub": ("pub", "bar"),
    "tavern": ("pub", "bar"),
    "biergarten": ("biergarten",),
    "nightclub": ("nightclub", "bar"),
    "fast food": ("fast_food", "restaurant"),
    "food truck": ("fast_food",),
    "food court": ("food_court",),
    "street food": ("fast_food", "food_court", "marketplace"),
    "food stand": ("fast_food", "food_court"),
    "snack bar": ("fast_food",),
    "ice cream": ("ice_cream",),
    "vending machine": ("vending_machine",),
    "bakery": ("bakery",),
    "market": ("marketplace",),
    # Cuisine intents ("jerk restaurant", "jamaican food") — the tag
    # matching lives in TAG_RULES (cuisine tag family); the class
    # substring here is the (rarely used) wkg_class fallback.
    "jerk": ("jerk", "cuisine"),
    "jamaican": ("jamaican",),
    "caribbean": ("caribbean",),
    "taco": ("restaurant", "fast_food", "cuisine"),
    "tacos": ("restaurant", "fast_food", "cuisine"),
    "mexican": ("restaurant", "fast_food", "cuisine"),
    "bbq": ("restaurant", "fast_food", "cuisine"),
    "cookout": ("restaurant", "fast_food", "cuisine"),
}

def _render_decompose_prompt(recipe: dict) -> str:
    """The decompose system prompt for a recipe (criteria schema over the
    template manifest). ``{max_q}`` / ``{manifest}`` stay as format
    placeholders for ``_decompose``."""
    slots = ", ".join(
        s["id"] for s in recipe.get("slots", ()) if not s.get("optional")
    )
    slot_ids = ", ".join(s["id"] for s in recipe.get("slots", ()))
    # The parser can only resolve classes the tag vocabulary knows — the
    # planner must stay inside it (the template-manifest gate applied to
    # OBJECTs, 2026-10-01). The recipe's class_vocabulary narrows the
    # whitelist to the recipe's domain (food & drink vendors for
    # FOOD_REPORT); CATEGORY_TAG_TARGETS is the fallback superset.
    resolvable = (
        recipe.get("class_vocabulary")
        or ", ".join(sorted(CATEGORY_TAG_TARGETS))
    )
    return (
        f"You are the research planner for a {recipe.get('label', 'plan')}. "
        "Decompose the user's research request into {max_q} self-contained "
        "questions the system's parser can answer. The parser is trained on "
        "exactly these template shapes:\n{manifest}\n"
        "Rules:\n"
        "- One entity class per question, chosen ONLY from the resolvable "
        f"class vocabulary: {resolvable}. Do NOT ask about vague or "
        "class-less concepts (a nice meal, nightlife, somewhere to hang "
        "out) — rephrase them as their resolvable class (nightlife → "
        "bars, a meal → restaurants).\n"
        "- Every required slot must have at least one question. Required "
        f"slots: {slots}. A missing required slot fails validation.\n"
        "- Use 'the top <class>' placeholders ONLY in radius (#1) "
        "questions (e.g. 'the top cafe'); distance (#2) and compare (#4) "
        "questions must use concrete named places from the request.\n"
        "- Decide each question's search radius yourself and output it as "
        "radius_m (integer meters) — see the radius policy.\n"
        f"- {recipe.get('radius_policy', '')}\n"
        "- Anchor every question at a named place from the request, or at a "
        "place named by an earlier question, written as 'the top <class>' "
        "(e.g. 'the top restaurant').\n"
        f"- {recipe.get('anchor_policy', '')}\n"
        "- When the request names a country or region rather than a city, "
        "pick a real hub city (the capital or primary population centre) "
        "as the base anchor and state it in the questions. NEVER anchor the "
        "plan on a place derived from an interest (e.g. 'Jerk Town' for "
        "jerk food, 'Scuba Bay' for diving) — interests become questions "
        "around the hub, not anchors.\n"
        "- Use the plain place name as the anchor, never a qualifier: "
        "'Belize City', not 'the centre of Belize City' or 'downtown "
        "Belize City'.\n"
        "- Each question must be a complete natural-language question, "
        "grammatically valid on its own.\n"
        "- Give each question a slot id naming the plan section it fills. "
        f"Use ONLY these slot ids: {slot_ids}. Never invent a new slot "
        "id. The slots are the plan's criteria: the summary is organized "
        "by them.\n"
        "- Return STRICT JSON only, no prose: "
        '{{"questions": [{{"question": "...", "why": "...", "slot": "...", '
        '"radius_m": 2000}}]}}'
    )


def _render_assemble_prompt(recipe: dict) -> str:
    """The assemble system prompt for a recipe (sections come from the
    recipe's ``assemble_sections``)."""
    sections = ", ".join(recipe.get("assemble_sections") or ())
    section_guide = (
        f" (for a {recipe.get('label', 'plan')}: {sections})"
        if sections else ""
    )
    return (
        "You are a geospatial research assistant. Write a structured "
        "summary that answers the user's research request. Ground EVERY "
        "claim in the provided answers only — never invent entities, "
        "counts, distances, classes, or opening hours. Use the exact "
        "entity names from the answers. Organize the summary by the "
        "coverage ledger's slots — one short labeled section per slot"
        + section_guide
        + ". Write a section for every slot, including gaps: when a slot "
        "is missing or missing_with_evidence, state that the requested "
        "category was not found and, when the ledger provides nearest "
        "evidence, name those entities as the closest alternatives. When "
        "a slot is filled_by_repair, use the original ask's wording to "
        "state the category was not found and present the repaired class "
        "as the closest match. When a slot's coverage marks "
        "class_mismatch, state that no entities of the asked class were "
        "found and that the results are of other classes — name them but "
        "do not present them as the requested category, and never "
        "speculate about what they might be. When an entity's class is "
        "coarse (wkgs:Amenity, wkgs:Shop, wkgs:Tourism) do NOT guess its "
        "subtype from its name or location ('likely a restaurant', 'may "
        "be a market') — state the class and the entity's tags as given, "
        "and when the data does not distinguish the subtype, say so "
        "explicitly (2026-10-01). When a slot's distances are "
        "flagged degenerate, do not cite distances for it. Never "
        "introduce a place, category, count, or distance that does not "
        "appear in the answers, and never fill an empty slot by "
        "generalizing to nearby categories or inventing alternatives."
        + (recipe.get("assemble_notes") or "")
    )


# Default rendered from the default recipe — the tests import it by
# name; runtime renders per recipe (route_to_recipe).
_DECOMPOSE_SYSTEM_PROMPT = _render_decompose_prompt(get_recipe())

_FOLLOWUP_SYSTEM_PROMPT = (
    "You enrich research findings by selecting 0-2 additional searches "
    "that fill gaps. Select zero tools when the answers already cover the "
    "request. Use the provided tools only."
)

# The KE interviewer (Knowledge Engineer) — the interactive LLM on the
# 4070. Turns the user's vague idea into a precise research brief. The KE
# ONLY interviews: it never emits the brief itself. The system builds the
# brief via structured extraction (finalize_brief), so free-form model
# output (the runaway place/distance list, observed 2026-09-18) cannot
# reach the run.
KE_SYSTEM_PROMPT = (
    "You are the research interviewer for a geospatial food & drink "
    "research system. Turn the user's idea into a precise research "
    "brief.\n"
    "Ask ONE short clarifying question at a time. Gather: the area of "
    "interest, any vendor-type or cuisine focus (restaurants, cafes, "
    "bars, pubs, food trucks, street vendors, vending machines, a "
    "cuisine such as korean or jerk), the scope or extent, and any "
    "constraints. The system only researches food & drink vendors — do "
    "not ask about shops, services, transit, or infrastructure.\n"
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
    "You extract research facts from a geospatial research interview. "
    "Respond with STRICT JSON only, no prose:\n"
    '{"area": "...", "focus": "...", "scope": "...", "constraints": "..."}\n'
    "Rules:\n"
    "- Use ONLY facts the user stated. Empty string when not stated.\n"
    "- area: the place or region the research is about.\n"
    "- focus: the food & drink specifics the user stated (a vendor "
    "type — restaurants, cafes, bars, food trucks, street vendors — "
    "or a cuisine such as korean or jerk). Empty string for a "
    "general eat-and-drink survey.\n"
    "- scope: any extent the user stated (a radius, a district, ...).\n"
    "- constraints: only limits the user stated. Empty string when the "
    "user stated none.\n"
    "- Never invent places, distances, numbers, or opening hours."
)


class ResearchOrchestratorService:
    """Stateless research orchestrator — the LLM decomposes, we execute."""

    # ── Public API ──────────────────────────────────────────────────────────

    @classmethod
    def plan(cls, prompt: str, country_code: str = None,
             snapshot_date: str = None, event_callback=None,
             subdivision_qid: str = None) -> dict:
        """Run the full research loop.

        ``subdivision_qid`` (2026-10-01) scopes every executed question's
        RESULTS to the subdivision polygon (the executor's
        ``_scope_to_subdivision``); the planner anchors inside the scope
        and the repair/continuation passes stay scoped too.

        ``event_callback`` (optional) receives progress events:
            {"event": "plan", "questions": [...]}
            {"event": "question", "index", "question", "template",
             "answer", "result_count", "error?"}
            {"event": "follow_up", "repair_of", "kind": "probe" |
             "class_swap" | "llm_replan", "question", "template",
             "answer", "result_count"}   (visible gap-repair re-asks)
            {"event": "replan", "repairs": [...]}   (gap-repair summary)
            {"event": "round", "round", "unmet", "questions"}
             (post-summary continuation round, 2026-09-30)
            {"event": "tool", "tool", "args"}
            {"event": "tool_out", "tool", "output"}
            {"event": "summary_delta", "delta"}   (token stream)

        Returns:
            {
              "prompt": str,
              "recipe": str (the recipe key the router picked),
              "country_code": str,
              "snapshot_date": str,
              "questions": [{question, why, slot, template, confidence,
                             answer, digest, result_count, error?,
                             repair?, evidence_*?}],
              "coverage": [{slot, status, template, result_count, note?}],
              "tool_calls": [{tool, args, output}],
              "summary": str (None when the LLM is unavailable; may
                             carry a "\n\nContinuing the plan" addendum),
              "errors": [{index, question, error}],
              "rounds": int (1, or 2 when a continuation round ran),
              "rounds_meta": [{round, questions}],
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

        # Recipe routing: the plan-type schema this run fills (food_report
        # by default; further recipes plug in via research_recipes.py).
        recipe = route_to_recipe(prompt)

        # Phase 1: decompose.
        questions = cls._decompose(llm, prompt, country_code, recipe=recipe)
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
            subdivision_qid=subdivision_qid,
        )

        # Phase 2b: coverage-led gap repair. Empty radius slots are probed
        # (#8), class-swapped to a class the snapshot actually has, and —
        # only if those deterministic steps fail — rewritten once by the
        # LLM (RESEARCH_REPLAN_LLM=1, bounded). The probe digest becomes
        # the nearest evidence the assembler may cite.
        repairs = cls._repair_empty_slots(
            records, llm, country_code, snapshot_date, event_callback,
            recipe=recipe, subdivision_qid=subdivision_qid,
        )
        if repairs and event_callback:
            event_callback({"event": "replan", "repairs": repairs})
        coverage = cls._coverage_ledger(records)

        # Phase 3: optional follow-up tool calls.
        tool_calls = cls._followup_tools(
            llm, prompt, records, last_results, country_code,
            snapshot_date, event_callback,
        )

        # Phase 4: assemble the final summary (the enriched deliverable).
        summary = cls._assemble(
            llm, prompt, records, tool_calls, coverage, event_callback,
            recipe=recipe, country_code=country_code,
        )
        rounds = 1
        rounds_meta = []

        # Phase 5: post-summary continuation (2026-09-30). Unmet slots
        # (missing / missing_with_evidence / class_mismatch) drive one
        # bounded round of deterministic follow-up questions, then an
        # addendum extends the summary — the plan keeps going instead of
        # ending on the gaps.
        if summary and MAX_CONTINUATION_ROUNDS >= 1:
            unmet = [
                c for c in coverage
                if c["status"] in (
                    "missing", "missing_with_evidence", "class_mismatch",
                )
            ]
            follow_ups = cls._continuation_questions(
                records, unmet, country_code, snapshot_date,
            )
            if follow_ups:
                round_no = 2
                if event_callback:
                    event_callback({
                        "event": "round",
                        "round": round_no,
                        "unmet": [c["slot"] for c in unmet],
                        "questions": [f["question"] for f in follow_ups],
                    })
                round_records = []
                for i, fu in enumerate(follow_ups):
                    round_records.append(cls._execute_followup_question(
                        fu["question"], fu["slot"], len(records) + i,
                        round_no, country_code, snapshot_date,
                        event_callback, subdivision_qid=subdivision_qid,
                    ))
                records.extend(round_records)
                coverage = cls._coverage_ledger(records)
                addendum = cls._assemble_addendum(
                    llm, prompt, round_records, event_callback, recipe=recipe,
                )
                if addendum:
                    summary = f"{summary}\n\n{addendum}"
                rounds = round_no
                rounds_meta = [{
                    "round": round_no,
                    "questions": [f["question"] for f in follow_ups],
                }]

        return {
            "prompt": prompt,
            "recipe": recipe.get("key") or DEFAULT_RECIPE_KEY,
            "country_code": country_code,
            "snapshot_date": snapshot_date,
            "questions": records,
            "coverage": coverage,
            "tool_calls": tool_calls,
            "summary": summary,
            "errors": errors,
            "rounds": rounds,
            "rounds_meta": rounds_meta,
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
                      snapshot_date: str = None,
                      subdivision_qid: str = None) -> list:
        """Full message list for the KE interviewer (system + cleaned history).

        ``messages`` is the raw conversation from the client (AI SDK
        UIMessage dicts or plain role/content dicts); only user and
        assistant turns with text survive. The system prompt carries the
        interview rules plus the country, snapshot, and (optionally,
        2026-10-01) subdivision scope.
        """
        context = (
            f"Country: {country_code or 'unspecified'}. "
            f"Snapshot: {snapshot_date or 'latest'}."
            + (
                f" Scope: subdivision {subdivision_qid}."
                if subdivision_qid else ""
            )
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
             snapshot_date: str = None, on_delta=None,
             subdivision_qid: str = None) -> str:
        """Stream the KE interviewer reply. Returns the full reply or None.

        The interviewer is the interactive LLM (``LLMService.get_instance``,
        the 4070) — the conversation is the interactive path. ``on_delta``
        receives each content delta for live streaming.
        """
        from core.services.llm_service import LLMService
        llm = LLMService.get_instance()
        if not llm.is_available():
            return None
        full = cls.chat_messages(
            messages, country_code, snapshot_date,
            subdivision_qid=subdivision_qid,
        )
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
        when no area is stated.
        """
        f = {
            k: (v or "").strip().replace("\n", " ")[:100]
            for k, v in (fields or {}).items()
            if isinstance(v, str)
        }
        area = f.get("area") or ""
        if not area:
            return None

        parts = [f"Research {area}"]
        scope = f.get("scope") or ""
        if scope:
            parts.append(f"({scope})")
        focus = f.get("focus") or ""
        if focus:
            parts.append(f"focusing on {focus}")
        brief = " ".join(parts)

        constraints = f.get("constraints") or ""
        if constraints:
            brief += f". Constraints: {constraints}"
        return brief[:400] or None

    @classmethod
    def finalize_brief(cls, messages: list, country_code: str = None,
                       snapshot_date: str = None,
                       subdivision_qid: str = None) -> dict:
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
                        f"Snapshot: {snapshot_date or 'latest'}."
                        + (
                            f" Scope: subdivision {subdivision_qid}."
                            if subdivision_qid else ""
                        )
                        + f"\nInterview:\n{conversation}\n"
                        "Extract the research facts as JSON."
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
    def _decompose(cls, llm, prompt: str, country_code: str,
                   recipe: dict = None) -> list:
        """LLM decomposes the prompt into parser-ready questions for the
        recipe's criteria schema.

        Qwen3 hybrid-thinking routing: the planner thinks when
        RESEARCH_LLM_THINK=1 (batch work, nobody waits). Schema-validation
        retry (DeepSeek-style): when the first JSON response yields no
        valid questions, the validation failure is fed back once and the
        model retries before the loop gives up.
        """
        manifest = "\n".join(
            f"- {name}: {desc}" for name, desc in TEMPLATE_MANIFEST.items()
        )
        system = {
            "role": "system",
            "content": _render_decompose_prompt(recipe or get_recipe()).format(
                max_q=MAX_QUESTIONS, manifest=manifest,
            ),
        }
        user = {
            "role": "user",
            "content": (
                f"Research request: {prompt}\n"
                f"Country: {country_code or 'unspecified'}\n"
                "Decompose the request into parser-ready questions."
            ),
        }
        decision = llm.chat_json(
            [system, user], temperature=0.0, max_tokens=900,
            think=_RESEARCH_THINK_ENABLED,
        )
        questions = cls._validate_questions(decision)
        missing = cls._missing_required_slots(questions, recipe)
        if (not questions or missing) and decision:
            reason = (
                "no valid questions"
                if not questions
                else "missing required slots: " + ", ".join(missing)
            )
            logger.info(
                "Research decompose: %s — one validation retry", reason,
            )
            decision = llm.chat_json(
                [
                    system,
                    user,
                    {"role": "assistant", "content": cls._decision_text(decision)},
                    {
                        "role": "user",
                        "content": (
                            "Your previous response produced " + reason + ". "
                            "Return STRICT JSON with a non-empty \"questions\" "
                            "array — each item needs a complete question "
                            "(8+ characters), a why, a slot id, and a "
                            "radius_m."
                        ),
                    },
                ],
                temperature=0.0, max_tokens=900,
                think=_RESEARCH_THINK_ENABLED,
            )
            questions = cls._validate_questions(decision)
        # Guaranteed slot coverage: deterministically fill required slots
        # the planner still skipped (2026-10-01).
        questions = cls._fill_missing_slots(questions, recipe)
        logger.info(
            "Research decompose: %d questions for %r", len(questions),
            prompt[:60],
        )
        return questions

    @staticmethod
    def _decision_text(decision) -> str:
        """The assistant turn text for the validation-retry prompt.

        The previous (invalid) response is echoed back as the assistant
        turn so the model can see and fix it. Never raises (fail-soft).
        """
        try:
            return json.dumps(decision, ensure_ascii=False)[:2000]
        except (TypeError, ValueError):
            return ""

    @classmethod
    def _validate_questions(cls, decision) -> list:
        """Validate the decompose JSON → [{question, why, slot, radius_m}, ...]
        (max 6).

        ``slot`` is the plan-section id the question fills (eat, shop,
        services, move, infra, ...); missing slots default to "general".
        ``radius_m`` (2026-10-01) is the agent-decided search radius,
        clamped to [100, 50000] via ``_clamp_radius_m``; None when absent
        or not a usable number (the executor then parses the question text).
        """
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
            slot = (item.get("slot") or "").strip() or "general"
            questions.append({
                "question": q,
                "why": (item.get("why") or "").strip(),
                "slot": slot,
                "radius_m": cls._clamp_radius_m(item.get("radius_m")),
            })
            if len(questions) >= MAX_QUESTIONS:
                break
        return questions

    @staticmethod
    def _clamp_radius_m(value) -> int:
        """Agent-decided radius → clamped meters, or None when unusable.

        The agent decides the radius within the recipe's bands; this only
        rejects nonsense (non-numeric, < 100 m, > 50 km). ``None`` means
        "unspecified" — the executor parses the question text as before.
        """
        try:
            radius_m = int(float(value))
        except (TypeError, ValueError):
            return None
        if radius_m < 100:
            return None
        return min(radius_m, 50000)

    @staticmethod
    def _missing_required_slots(questions: list, recipe: dict) -> list:
        """Required (non-optional) recipe slots with no question yet."""
        if not recipe:
            return []
        required = [
            s["id"] for s in recipe.get("slots", ())
            if not s.get("optional") and not s.get("derived")
        ]
        present = {q.get("slot") for q in (questions or [])}
        return [s for s in required if s not in present]

    @classmethod
    def _fill_missing_slots(cls, questions: list, recipe: dict) -> list:
        """Deterministic fill for required slots the planner skipped.

        Uses the recipe's ``slot_defaults`` ("Which restaurants are within
        3km of {anchor}?"), anchored at the first question's anchor. When
        no anchor can be extracted the slot is left missing — the coverage
        ledger / continuation round surfaces it. Guarantees every required
        slot has >= 1 question (2026-10-01 — the planner used to spend all
        questions on one section and leave services/move/infra empty).
        """
        if not recipe:
            return questions or []
        defaults = recipe.get("slot_defaults") or {}
        anchor = None
        for q in questions or []:
            anchor = cls._anchor_from_question(q.get("question", ""))
            if anchor:
                break
        filled = list(questions or [])
        for slot in cls._missing_required_slots(filled, recipe):
            template = defaults.get(slot)
            if not template or not anchor:
                continue
            question = template.format(anchor=anchor)
            if any(f.get("question") == question for f in filled):
                continue
            filled.append({
                "question": question,
                "why": f"default fill for missing {slot} slot",
                "slot": slot,
                "radius_m": cls._parse_radius_m(question),
            })
        return filled

    # ── Phase 2: execute ───────────────────────────────────────────────────

    @classmethod
    def _execute_questions(cls, questions: list, country_code: str,
                           snapshot_date: str, event_callback,
                           subdivision_qid: str = None):
        """Run parser + executor per question. Returns
        (records, errors, last_top_entity, last_results)."""
        from semantic_search.services.query_parser_service import QueryParserService
        from semantic_search.services.query_executor_service import (
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
            # Class-aware placeholder (2026-09-30): "the top hotel" resolves
            # to a hotel-class entity from the prior question's results when
            # one exists; the plain top entity is the fallback.
            hint = cls._placeholder_class(item["question"])
            top = None
            if hint and last_results:
                top = cls._top_entity_name(
                    last_results, class_hint=hint,
                )
            qtext = cls._substitute_placeholders(
                item["question"], top or last_top_entity,
            )
            record = {
                "index": index,
                "question": qtext,
                "why": item.get("why", ""),
                "slot": item.get("slot", "general"),
            }
            try:
                parsed = QueryParserService.get_instance().parse(qtext)
                template = (parsed or {}).get("template")
                if not template:
                    raise ValueError("parser returned no template")
                # Agent-decided radius (2026-10-01) — override the parsed
                # AMOUNT concept so the executor uses the planner's
                # radius_m instead of re-parsing (or defaulting).
                parsed = cls._inject_radius(parsed, item.get("radius_m"))
                result = QueryExecutorService.execute(
                    parsed, country_code, snapshot_date,
                    question=qtext, skip_enrichment=True,
                    subdivision_qid=subdivision_qid,
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
                            alt_parsed = cls._inject_radius(
                                alt_parsed, item.get("radius_m"),
                            )
                            result = QueryExecutorService.execute(
                                alt_parsed, country_code, snapshot_date,
                                question=alt, skip_enrichment=True,
                                subdivision_qid=subdivision_qid,
                            )
                            if result.get("error"):
                                raise ValueError(result["error"])
                            results = result.get("results") or []
                            qtext = alt
                            parsed = alt_parsed

                # Country-hub re-anchor (2026-09-30): the anchor still fails
                # to geocode — an interest-derived place ("Jerk Town" for
                # jerk food) that no snapshot entity carries. Re-anchor once
                # at the country's hub instead of leaving the slot
                # unanswerable.
                if cls._anchor_geocode_failed(result):
                    hub = cls._hub_for_country(country_code)
                    hub_q = (
                        cls._reanchor_question(qtext, hub) if hub else None
                    )
                    if hub_q and hub_q != qtext:
                        logger.info(
                            "Research re-anchor at country hub: %r", hub_q,
                        )
                        hub_parsed = QueryParserService.get_instance().parse(
                            hub_q,
                        )
                        if (hub_parsed or {}).get("template"):
                            hub_parsed = cls._inject_radius(
                                hub_parsed, item.get("radius_m"),
                            )
                            result = QueryExecutorService.execute(
                                hub_parsed, country_code, snapshot_date,
                                question=hub_q, skip_enrichment=True,
                                subdivision_qid=subdivision_qid,
                            )
                            if result.get("error"):
                                raise ValueError(result["error"])
                            results = result.get("results") or []
                            qtext = hub_q
                            parsed = hub_parsed
                            record["hub_reanchored"] = True

                # No deterministic radius escalation (removed 2026-10-01):
                # the agent decides radii (radius_m, validated); an empty
                # result now flows to the repair pipeline, where the agent
                # (probe → class-swap → llm_replan) chooses a better
                # radius — the loop never overrides the agent behind its
                # back. The continuation round's wider-radius generator
                # still covers unmet slots.

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
                    "result_classes": dict(Counter(
                        (r.get("wkg_class") or "unclassified")
                        for r in (results if isinstance(results, list) else [])
                    )),
                    # Food-report derived tallies (2026-10-02): name
                    # frequency = chain signal ("Starbucks" ×14 → a
                    # popular brand); cuisine-tag frequency = the
                    # local-vs-international split. Multi-value OSM
                    # cuisine tags ("regional;chicken") split on ';'.
                    "brand_counts": dict(Counter(
                        (r.get("name") or "").strip()
                        for r in (results if isinstance(results, list) else [])
                        if (r.get("name") or "").strip()
                    )),
                    "cuisine_counts": dict(Counter(
                        v.strip()
                        for r in (results if isinstance(results, list) else [])
                        for v in str(
                            (r.get("tags") or {}).get("cuisine") or ""
                        ).split(";")
                        if v.strip()
                    )),
                    "degenerate_distances": cls._degenerate_distances(results),
                    "class_mismatch": cls._results_mismatch(results, qtext),
                })
                # The anchor itself can appear as a result ("belize city"
                # node at 152m) — exclude it so 'the top hotel' resolves to
                # a real hotel, not the anchor. Only overwrite on a hit: an
                # empty question must not wipe the placeholder for the next.
                anchor_text = cls._first_location(parsed)
                top = cls._top_entity_name(
                    results, exclude=anchor_text,
                    class_hint=cls._question_class(qtext),
                )
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
    def _inject_radius(parsed: dict, radius_m: int) -> dict:
        """Agent-decided radius (2026-10-01) → the parsed AMOUNT concept.

        The executor's ``_parse_radius`` reads AMOUNT for radius-carrying
        templates; overriding the concept (replacing an existing AMOUNT or
        appending one, formatted "<m>m") makes the planner's radius_m the
        source of truth instead of re-parsing the text. Only
        FILTER-AGGREGATE-MEASURE (#1) consumes AMOUNT as a radius; other
        templates keep their defaults. Returns ``parsed`` unchanged when
        nothing applies.
        """
        if not parsed or not radius_m:
            return parsed
        if parsed.get("template") != "FILTER-AGGREGATE-MEASURE (#1)":
            return parsed
        concepts = parsed.setdefault("concepts", [])
        text = f"{int(radius_m)}m"
        for c in concepts:
            if isinstance(c, dict) and c.get("type") == "AMOUNT":
                c["text"] = text
                return parsed
        concepts.append({"type": "AMOUNT", "text": text})
        return parsed

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
    def _pluralize_category(cls, token: str) -> str:
        """Pluralize a category token for a re-ask ("beach" → "beaches",
        "guest house" → "guest houses", "church" → "churches")."""
        words = (token or "").split()
        if not words:
            return token
        last = words[-1]
        if last.endswith(("s", "x", "z", "ch", "sh")):
            last += "es"
        else:
            last += "s"
        return " ".join(words[:-1] + [last])

    @staticmethod
    def _singularize(word: str) -> str:
        """Rough singularization for class matching ('hotels' → 'hotel')."""
        w = (word or "").strip().lower()
        if len(w) > 3 and w.endswith("ies"):
            return w[:-3] + "y"
        if len(w) > 1 and w.endswith("s"):
            return w[:-1]
        return w

    @classmethod
    def _class_hint_from(cls, word: str):
        """The _CLASS_ALIASES key nearest to a class word, or None."""
        singular = cls._singularize(word)
        if singular in _CLASS_ALIASES:
            return singular
        # Cuisine intents win in compound phrases: "jerk restaurant" must
        # resolve to the jerk cuisine family, not to "restaurant" (the
        # first substring match in dict order).
        words = set(singular.split())
        for cuisine in ("jerk", "jamaican", "caribbean"):
            if cuisine in words and cuisine in _CLASS_ALIASES:
                return cuisine
        best = None
        for key in _CLASS_ALIASES:
            if singular in key or key in singular:
                if best is None or len(key) > len(best):
                    best = key
        return best

    @classmethod
    def _question_class(cls, qtext: str) -> str:
        """The entity-class token of a radius question ("Which hotels are
        within 2km of X?" → "hotel"), or None for open phrasings."""
        m = re.match(
            r"^which\s+(.+?)\s+(?:are|is)\s+(?:within|near|around|by)\b",
            (qtext or "").strip(), re.IGNORECASE,
        )
        if not m:
            return None
        return cls._singularize(m.group(1))

    @classmethod
    def _placeholder_class(cls, qtext: str) -> str:
        """The class token of a placeholder ("the top hotel" → "hotel")."""
        m = re.search(
            r"\bthe\s+(?:top|best|nearest|first)\s+(\w+)",
            qtext or "", re.IGNORECASE,
        )
        return cls._singularize(m.group(1)) if m else None

    @classmethod
    def _class_matches(cls, wkg_class: str, hint: str) -> bool:
        """Whether an entity's wkg_class matches an intent hint (substring
        on the aliases: "hotel" matches wkgs:Hotel and wkgs:TourismHotel).
        The class column is coarse (a hotel may be wkgs:Building) — prefer
        ``_entity_matches``, which checks the OSM tags first."""
        key = cls._class_hint_from(hint)
        if key is None:
            return False
        cls_name = (wkg_class or "").lower()
        # Trailing word boundary (2026-10-02): keeps the loose-prefix
        # match ("hotel" in wkgs:TourismHotel) but stops "pub" matching
        # wkgs:Public_transport and "bar" matching wkgs:Barrier.
        return any(
            re.search(re.escape(s) + r"\b", cls_name)
            for s in _CLASS_ALIASES[key]
        )

    @classmethod
    def _entity_matches(cls, entity: dict, hint: str) -> bool:
        """Whether an entity matches an intent hint. OSM tags first (the
        real semantics — Grand Lido Negril is wkgs:Building with
        tourism=hotel), then the coarse wkg_class substring as fallback.
        Multi-value OSM tags ("cuisine=regional;chicken") match by
        substring."""
        key = cls._class_hint_from(hint)
        if key is None:
            return False
        tags = (entity or {}).get("tags") or {}
        for tag_key, values in TAG_RULES.get(key, ()):
            v = tags.get(tag_key)
            if v and any(val in v.lower() for val in values):
                return True
        return cls._class_matches((entity or {}).get("wkg_class"), hint)

    @classmethod
    def _results_mismatch(cls, results: list, qtext: str) -> bool:
        """True when a filled slot's entities none match the asked class
        ("Which hotels..." returning only shops). Reads the OSM tags, not
        the coarse class column — a tourism=hotel result IS a hotel."""
        if not isinstance(results, list) or not results:
            return False
        hint = cls._question_class(qtext)
        if not hint:
            return False
        return not any(cls._entity_matches(r, hint) for r in results)

    @staticmethod
    def _top_entity_name(results: list, exclude: str = None,
                         class_hint: str = None) -> str:
        """First named entity in the result list (already ranked).

        With ``class_hint``, class-matching entities are preferred — "the
        top hotel" resolves to a hotel when one exists, not to whatever
        named entity ranks first (Annali's Restaurant became a hotel,
        2026-09-30). When no class match exists, any named entity is used
        so placeholder chains stay alive; slot-level honesty is the
        coverage ledger's ``class_mismatch`` flag, not the anchor.
        ``exclude`` (the question's own anchor text) is skipped so the
        anchor itself never becomes 'the top <class>'.
        """
        exclude_l = (exclude or "").strip().lower()
        fallback = None
        for r in results or []:
            name = r.get("name") or (r.get("tags") or {}).get("name")
            if not name or name.strip().lower() == exclude_l:
                continue
            if class_hint and not ResearchOrchestratorService._entity_matches(
                r, class_hint,
            ):
                if fallback is None:
                    fallback = name
                continue
            return name
        return fallback

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

    # ── Phase 2b: coverage-led deterministic repair ───────────────────────

    # The trailing named place of a radius question ("...of Belize City",
    # "...near Jovilee Apartments") — the anchor for the #8 fallback.
    _ANCHOR_SUFFIX_RE = re.compile(
        r"\b(?:near|around|of|from|to)\s+(.+?)\s*$", re.IGNORECASE,
    )

    @staticmethod
    def _hub_for_country(country_code: str) -> str:
        """The primary hub city for a country code, or None (unknown)."""
        return COUNTRY_HUBS.get((country_code or "").upper())

    # Matches "of Jerk Town", "near Bull Bay", ... keeping the preposition
    # so the re-anchor reads "of Kingston" not "Kingston".
    _ANCHOR_WITH_PREP_RE = re.compile(
        r"\b(near|around|of|from|to)\s+[^?]+\s*$", re.IGNORECASE,
    )

    @classmethod
    def _reanchor_question(cls, qtext: str, hub: str) -> str:
        """Re-anchor a question's trailing place at the hub city."""
        if not qtext or not hub:
            return None
        # The trailing "?" must go first so the anchor regex reaches
        # end-of-string (same handling as _anchor_from_question); it is
        # re-appended so the rewritten question keeps its surface form.
        had_q = qtext.rstrip().endswith("?")
        cleaned = qtext.strip().rstrip("?.")
        q = cls._ANCHOR_WITH_PREP_RE.sub(
            lambda m: f"{m.group(1)} {hub}", cleaned, count=1,
        )
        return (q + "?") if had_q else q

    @classmethod
    def _anchor_from_question(cls, qtext: str) -> str:
        """The trailing anchor phrase of a question, or None.

        Parser-independent (concepts are patched in tests and may be
        empty): "Which hotels are within 2km of Belize City?" →
        "Belize City". The trailing "?" is stripped first so the regex
        reaches end-of-string.
        """
        if not qtext:
            return None
        cleaned = qtext.strip().rstrip("?.")
        m = cls._ANCHOR_SUFFIX_RE.search(cleaned)
        if not m:
            return None
        anchor = m.group(1).strip()
        return anchor or None

    @classmethod
    def _repair_empty_slots(cls, records: list, llm, country_code: str,
                            snapshot_date: str, event_callback,
                            recipe: dict = None,
                            subdivision_qid: str = None) -> list:
        """Coverage-led gap repair for empty radius (#1) slots.

        Pipeline per empty slot (deterministic except the final step):
          1. **probe** — re-ask the anchor as an open-ended #8 ("What
             amenities are near X?") to learn what classes actually exist
             nearby; the digest is the availability oracle.
          2. **class-swap** — re-ask the #1 with the most relevant class
             the probe found present ("hikes" → wkgs:Peak/Natural/
             Viewpoint; "museums" → wkgs:HistoricChurch when that is what
             the snapshot has). Zero LLM calls.
          3. **llm replan** (RESEARCH_REPLAN_LLM=1, default ON, bounded) —
             one chat_json rewrites the failing question with a changed
             strategy (anchor / class / radius); re-run once.
          4. **evidence fallback** — if everything is still empty, the
             probe digest is recorded as the slot's *nearest evidence*:
             the assembler may name those entities as the closest
             alternatives instead of leaving the section silent.

        Returns the repair decisions (also attached to each record):
            [{index, question, status, repair, rewritten?, evidence?}]
        """
        from semantic_search.services.query_executor_service import (
            DEFAULT_NEAR_RADIUS_M,
            QueryExecutorService,
        )
        from semantic_search.services.query_enrichment_service import (
            QueryEnrichmentService,
        )
        from semantic_search.services.query_parser_service import (
            QueryParserService,
        )

        repairs = []
        rewrites_left = MAX_REPLAN_REWRITES
        for index, record in enumerate(records):
            if record.get("error") or record.get("repair"):
                continue
            if record.get("template") != "FILTER-AGGREGATE-MEASURE (#1)":
                continue
            if record.get("result_count", 0) > 0:
                continue
            original = record.get("question", "")
            anchor = cls._anchor_from_question(original)
            if not anchor:
                continue
            probe = f"What amenities are near {anchor}?"
            if probe == original:
                continue  # already the open-ended form — nothing to repair

            # 1. probe — the availability oracle.
            probe_result = {}
            try:
                probe_parsed = QueryParserService.get_instance().parse(probe)
                if (probe_parsed or {}).get("template"):
                    probe_result = QueryExecutorService.execute(
                        probe_parsed, country_code, snapshot_date,
                        question=probe, skip_enrichment=True,
                        subdivision_qid=subdivision_qid,
                    )
            except Exception as exc:  # noqa: BLE001 — repair must never kill the loop
                logger.warning("Research probe failed: %s", exc)
            if not isinstance(probe_result, dict) or probe_result.get("error"):
                probe_result = {}
            probe_results = [
                r for r in (probe_result.get("results") or [])
                if cls._evidence_worthy(
                    r, cls._slot_allows_transport(recipe, record.get("slot")),
                )
            ]
            probe_digest = QueryEnrichmentService._primary_digest(probe_results)
            if event_callback:
                event_callback({
                    "event": "follow_up",
                    "repair_of": index,
                    "kind": "probe",
                    "question": probe,
                    "template": (probe_parsed or {}).get("template"),
                    "answer": probe_result.get("answer"),
                    "result_count": (
                        len(probe_results) if isinstance(probe_results, list) else 0
                    ),
                })

            # 2. class-swap — re-ask with a class the probe found present.
            swapped = cls._class_swap_question(
                original, anchor, probe_results,
            )
            if swapped and cls._rerun(
                record, swapped, country_code, snapshot_date,
                repair="class_swap", subdivision_qid=subdivision_qid,
            ):
                if event_callback:
                    event_callback({
                        "event": "follow_up",
                        "repair_of": index,
                        "kind": "class_swap",
                        "question": swapped,
                        "template": record.get("template"),
                        "answer": record.get("answer"),
                        "result_count": record.get("result_count", 0),
                    })
                repairs.append({
                    "index": index, "question": original,
                    "status": "filled_by_repair",
                    "repair": "class_swap", "rewritten": swapped,
                })
                continue

            # 3. llm replan — bounded, default ON. The attempt is surfaced
            # (visible follow-up row) whether or not it finds results.
            if _REPLAN_LLM_ENABLED and rewrites_left > 0:
                rewritten = cls._llm_replan(llm, record, probe_digest)
                if rewritten:
                    rewrites_left -= 1
                    filled = cls._rerun(
                        record, rewritten, country_code, snapshot_date,
                        repair="llm_replan", subdivision_qid=subdivision_qid,
                    )
                    if event_callback:
                        event_callback({
                            "event": "follow_up",
                            "repair_of": index,
                            "kind": "llm_replan",
                            "question": rewritten,
                            "template": record.get("template") if filled else None,
                            "answer": record.get("answer") if filled else None,
                            "result_count": (
                                record.get("result_count", 0) if filled else 0
                            ),
                        })
                    if filled:
                        repairs.append({
                            "index": index, "question": original,
                            "status": "filled_by_repair",
                            "repair": "llm_replan", "rewritten": rewritten,
                        })
                        continue

            # 4. evidence fallback — the probe digest is the nearest evidence.
            record["repair"] = "fallback_evidence"
            record["evidence_question"] = probe
            record["evidence_answer"] = probe_result.get("answer")
            record["evidence_digest"] = probe_digest
            record["evidence_result_count"] = (
                len(probe_results) if isinstance(probe_results, list) else 0
            )
            repairs.append({
                "index": index,
                "question": original,
                "status": (
                    "missing_with_evidence"
                    if record["evidence_result_count"] else "missing"
                ),
                "evidence": probe,
                "evidence_result_count": record["evidence_result_count"],
            })
        return repairs

    @classmethod
    def _class_swap_question(cls, original: str, anchor: str,
                             probe_results: list) -> str:
        """Re-ask a #1 radius question with the most relevant category the
        probe found present near the anchor.

        The probe entities' OSM **tags** decide (not the coarse class
        column): for "hikes", if the probe found natural=peak, re-ask
        "Which peaks are within {radius} of {anchor}?" — the executor
        resolves "peaks" to ("natural", "peak") and the exact-tag tier
        filters precisely. Returns None when no present category family
        maps to a resolvable tag target.
        """
        hint = cls._question_class(original)
        if not hint:
            return None
        key = cls._class_hint_from(hint)
        families = TAG_RULES.get(key)
        if not families:
            return None
        # Which (key, value) pairs from the intent's families did the
        # probe actually find present? (multi-value tags match by
        # substring — "cuisine=regional;chicken")
        present = set()
        for r in (probe_results or []):
            tags = r.get("tags") or {}
            for tag_key, values in families:
                v = tags.get(tag_key)
                if not v:
                    continue
                v_l = v.lower()
                for val in values:
                    if val in v_l:
                        present.add((tag_key, val))
        if not present:
            return None
        # First present family in priority order → natural category token
        # ("guest house", "peak", "waterfall") the parser/executor round-trip.
        token = None
        for tag_key, values in families:
            for val in values:
                if (tag_key, val) in present:
                    token = _TAG_TARGET_TO_TOKEN.get((tag_key, val))
                    if token:
                        break
            if token:
                break
        if not token:
            return None
        radius = cls._parse_radius_m(original)
        if radius is None:
            from semantic_search.services.query_executor_service import (
                DEFAULT_NEAR_RADIUS_M,
            )
            radius = DEFAULT_NEAR_RADIUS_M
        radius_text = (
            f"{radius / 1000.0:g} km" if radius >= 1000 else f"{radius:g} m"
        )
        # "guest house" → "guest houses", "beach" → "beaches".
        plural = cls._pluralize_category(token)
        return f"Which {plural} are within {radius_text} of {anchor}?"

    @classmethod
    def _llm_replan(cls, llm, record: dict, probe_digest: str) -> str:
        """One bounded chat_json rewrite of a failing slot question.

        Returns the rewritten question text or None (fail-soft). The
        rewrite is validated before use: a dict with a "question" string
        8+ characters.
        """
        if not llm or not _REPLAN_LLM_ENABLED:
            return None
        try:
            decision = llm.chat_json([
                {"role": "system", "content": _REPLAN_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"Original question: {record.get('question')}\n"
                        f"Template: {record.get('template')}\n"
                        "Result: no entities found.\n"
                        "Classes present near the anchor: "
                        f"{probe_digest or 'none'}\n"
                        "Rewrite the question so it can find results."
                    ),
                },
            ], temperature=0.0, max_tokens=300, think=_RESEARCH_THINK_ENABLED)
        except Exception as exc:  # noqa: BLE001 — fail-soft
            logger.warning("Research replan failed: %s", exc)
            return None
        if not isinstance(decision, dict):
            return None
        q = (decision.get("question") or "").strip()
        return q if len(q) >= 8 else None

    @classmethod
    def _rerun(cls, record: dict, qtext: str, country_code: str,
               snapshot_date: str, repair: str,
               subdivision_qid: str = None) -> bool:
        """Parse + execute a repaired question; on results, update the
        record in place (the original ask is kept for the trace) and
        return True. Fail-soft: False on any failure.
        """
        from semantic_search.services.query_executor_service import (
            QueryExecutorService,
        )
        from semantic_search.services.query_enrichment_service import (
            QueryEnrichmentService,
        )
        from semantic_search.services.query_parser_service import (
            QueryParserService,
        )
        try:
            parsed = QueryParserService.get_instance().parse(qtext)
            template = (parsed or {}).get("template")
            # Template-vocabulary gate (2026-10-01): a repair rewrite (e.g.
            # an LLM replan) that parses outside the 5 trained shapes is
            # discarded — executing it yields garbage ("CULTURAL-CONTEXT-
            # EXPLORATION" answers observed on the Mexico City run).
            if not template or template not in TEMPLATE_MANIFEST:
                logger.info(
                    "Research rerun: %s outside the template vocabulary, "
                    "discarded: %r", template or "no template", qtext,
                )
                return False
            result = QueryExecutorService.execute(
                parsed, country_code, snapshot_date,
                question=qtext, skip_enrichment=True,
                subdivision_qid=subdivision_qid,
            )
            if result.get("error"):
                return False
            results = result.get("results") or []
            if not (isinstance(results, list) and len(results) > 0):
                return False
        except Exception as exc:  # noqa: BLE001 — repair must never kill the loop
            logger.warning("Research repair re-run failed for %r: %s", qtext, exc)
            return False
        record.setdefault("original_question", record.get("question"))
        record.update({
            "question": qtext,
            "template": parsed["template"],
            "confidence": parsed.get("confidence"),
            "answer": result.get("answer"),
            "digest": QueryEnrichmentService._primary_digest(results),
            "result_count": len(results),
            "repair": repair,
            "result_classes": dict(Counter(
                (r.get("wkg_class") or "unclassified") for r in results
            )),
            "degenerate_distances": cls._degenerate_distances(results),
            "class_mismatch": cls._results_mismatch(results, qtext),
        })
        return True

    @staticmethod
    def _degenerate_distances(results) -> bool:
        """True when 2+ results all carry the same distance (e.g. all 0m)
        — the distances are unreliable and must not be cited. Observed
        2026-09-30: eight restaurants at 0m from the anchor."""
        if not isinstance(results, list):
            return False
        ds = [
            r.get("distance_m") for r in results
            if r.get("distance_m") is not None
        ]
        return len(ds) >= 2 and len(set(ds)) == 1

    @classmethod
    def _coverage_ledger(cls, records: list) -> list:
        """Slot coverage after execution + repair.

        Per record: {slot, status, template, result_count, question, note,
        class_mismatch, degenerate_distances}. ``question`` is the
        original ask (kept when a repair rewrote it).
        Status is the plan's honesty contract:
            filled                 — the slot question returned results
            filled_by_repair       — repaired (class-swap / llm replan)
                                     and now has results
            class_mismatch         — results exist but none match the asked
                                     class ("jerk restaurants" → a grocery
                                     store): the slot is UNMET
            error                  — the slot question failed
            missing_with_evidence  — the requested category was not found,
                                     but the #8 probe found nearby
                                     entities (evidence_digest)
            missing                — nothing found anywhere
        """
        ledger = []
        for record in records:
            if record.get("error"):
                status = "error"
            elif record.get("result_count", 0) > 0:
                if record.get("class_mismatch"):
                    # Results exist but none match the asked class ("jerk
                    # restaurants" → a grocery store; 108 amenities for
                    # "historic sites") — the slot is UNMET, not filled.
                    status = "class_mismatch"
                else:
                    status = "filled_by_repair" if record.get("repair") else "filled"
            elif record.get("repair") == "fallback_evidence":
                status = (
                    "missing_with_evidence"
                    if record.get("evidence_result_count", 0) > 0
                    else "missing"
                )
            else:
                status = "missing"
            ledger.append({
                "slot": record.get("slot", "general"),
                "status": status,
                "template": record.get("template"),
                "result_count": record.get("result_count", 0),
                "question": (
                    record.get("original_question")
                    or record.get("question", "")
                ),
                "note": record.get("repair"),
                "class_mismatch": cls._class_mismatch(record),
                "degenerate_distances": record.get(
                    "degenerate_distances", False,
                ),
            })
        return ledger

    @staticmethod
    def _evidence_worthy(entity: dict, allow_transport: bool = False) -> bool:
        """Whether an entity is worth surfacing as nearest evidence.

        Excludes transport infrastructure (bus stops, transit, rail) —
        "5, 8, 12 Bus Stop" pollutes the evidence digest for a "hikes"
        slot (2026-09-30). The probe's digest is the availability oracle
        AND the nearest evidence, so it must not be mostly transit noise.

        A recipe slot whose target class IS transport (the place-report
        "getting around" slot) passes ``allow_transport=True`` — there
        the transit abundance is the signal, not noise (2026-10-01).
        """
        if allow_transport:
            return True
        tags = (entity or {}).get("tags") or {}
        if tags.get("highway") == "bus_stop":
            return False
        if tags.get("place") == "bus_stop":
            return False
        if any(k in tags for k in ("public_transport", "railway")):
            return False
        return True

    @staticmethod
    def _slot_allows_transport(recipe: dict, slot_id: str) -> bool:
        """Whether a recipe slot treats transport as target-class evidence.

        Declared per slot on the recipe row (``transport_evidence``); the
        repair pipeline reads it so the probe digest for a transit slot
        keeps the bus stops that a tourism slot would treat as noise.
        """
        if not recipe:
            return False
        for slot in recipe.get("slots", ()):
            if slot.get("id") == slot_id:
                return bool(slot.get("transport_evidence"))
        return False

    # ── Phase 4b: post-summary continuation round ─────────────────────────

    @classmethod
    def _continuation_questions(cls, records: list, unmet: list,
                                country_code: str,
                                snapshot_date: str) -> list:
        """Deterministic round-2 follow-ups for unmet slots (max 2).

        Two generators, both grounded in the shared tag vocabulary:
          - an intent-family question for outdoor interests ("hikes" →
            "Which peaks are within 30 km of X?" — the concrete adventure
            target the snapshot maps, from TAG_RULES);
          - a widened-radius re-ask of the original category ("Which
            restaurants are within 2 km of Jerk Town?" → 6 km) so a
            too-small radius stops hiding real results (restaurants exist
            in Kingston, just not within 2 km of the jerk stand).
        """
        from semantic_search.services.query_executor_service import (
            DEFAULT_NEAR_RADIUS_M,
        )

        follow_ups = []
        for c in unmet or []:
            if len(follow_ups) >= 2:
                break
            if c["status"] not in (
                "missing", "missing_with_evidence", "class_mismatch",
            ):
                continue
            record = None
            for r in records:
                if (r.get("original_question") or r.get("question")) == \
                        c["question"]:
                    record = r
                    break
            if not record:
                continue
            qtext = record.get("question", "")
            hint = cls._question_class(qtext)
            anchor = cls._anchor_from_question(qtext)
            if not hint or not anchor:
                continue

            # Intent-family question first (outdoor interests).
            key = cls._class_hint_from(hint)
            family = TAG_RULES.get(key) if key else None
            if family and any(
                tk in ("natural", "waterway", "leisure", "route", "highway")
                for tk, _vals in family
            ):
                token = None
                for tag_key, values in family:
                    for val in values:
                        token = _TAG_TARGET_TO_TOKEN.get((tag_key, val))
                        if token:
                            break
                    if token:
                        break
                if token:
                    plural = cls._pluralize_category(token)
                    follow_ups.append({
                        "question": (
                            f"Which {plural} are within 30 km of {anchor}?"
                        ),
                        "slot": c["slot"],
                    })
                    continue

            # Widened-radius re-ask of the original category.
            radius = cls._parse_radius_m(qtext) or DEFAULT_NEAR_RADIUS_M
            wider = max(int(radius * 3), 5000)
            words = hint.split()
            plural = " ".join(words[:-1] + [words[-1] + "s"])
            follow_ups.append({
                "question": (
                    f"Which {plural} are within {wider / 1000.0:g} km "
                    f"of {anchor}?"
                ),
                "slot": c["slot"],
            })
        return follow_ups

    @classmethod
    def _execute_followup_question(cls, qtext: str, slot: str, index: int,
                                   round_no: int, country_code: str,
                                   snapshot_date: str,
                                   event_callback,
                                   subdivision_qid: str = None) -> dict:
        """Execute one round-2 follow-up (no placeholders/retries — the
        continuation questions are concrete). Emits its question event."""
        from semantic_search.services.query_parser_service import (
            QueryParserService,
        )
        from semantic_search.services.query_executor_service import (
            QueryExecutorService,
        )
        from semantic_search.services.query_enrichment_service import (
            QueryEnrichmentService,
        )

        record = {
            "index": index, "question": qtext, "slot": slot,
            "round": round_no,
        }
        try:
            parsed = QueryParserService.get_instance().parse(qtext)
            if not (parsed or {}).get("template"):
                raise ValueError("parser returned no template")
            result = QueryExecutorService.execute(
                parsed, country_code, snapshot_date,
                question=qtext, skip_enrichment=True,
                subdivision_qid=subdivision_qid,
            )
            if result.get("error"):
                raise ValueError(result["error"])
            results = result.get("results") or []
            record.update({
                "template": parsed["template"],
                "confidence": parsed.get("confidence"),
                "answer": result.get("answer"),
                "digest": QueryEnrichmentService._primary_digest(results),
                "result_count": (
                    len(results) if isinstance(results, list) else 0
                ),
                "result_classes": dict(Counter(
                    (r.get("wkg_class") or "unclassified")
                    for r in (results if isinstance(results, list) else [])
                )),
                "degenerate_distances": cls._degenerate_distances(results),
                "class_mismatch": cls._results_mismatch(results, qtext),
            })
        except Exception as exc:  # noqa: BLE001 — one bad follow-up must not kill the loop
            record["error"] = str(exc)
        if event_callback:
            event_callback({
                "event": "question",
                "index": index, "round": round_no,
                "question": qtext,
                "template": record.get("template"),
                "answer": record.get("answer"),
                "digest": record.get("digest") or "",
                "result_count": record.get("result_count", 0),
                "error": record.get("error"),
            })
        return record

    @classmethod
    def _assemble_addendum(cls, llm, prompt: str, followup_records: list,
                           event_callback, recipe: dict = None) -> str:
        """The continuation addendum, grounded in the round-2 answers.
        Always non-thinking (same as the main assembly)."""
        user_content = (
            f"Research request: {prompt}\n"
            "Round 2 follow-up answers (deterministic templated answers):\n"
            f"{cls._answers_text(followup_records)}\n"
            "Write the continuation addendum."
        )
        messages = [
            {"role": "system", "content": _ADDENDUM_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]
        parts = []
        for delta in llm.chat_stream(
            messages, temperature=0.3, max_tokens=600, think=False,
        ):
            if event_callback:
                event_callback({"event": "summary_delta", "delta": delta})
            parts.append(delta)
        addendum = "".join(parts).strip()
        if not addendum:
            addendum = llm.chat(
                messages, temperature=0.3, max_tokens=600, think=False,
            )
            addendum = (addendum or "").strip()
        return addendum or None

    @staticmethod
    def _class_mismatch(record: dict) -> bool:
        """Reader for the flag computed at record-write time from the raw
        results (``_results_mismatch`` — tags, not the coarse class)."""
        return bool(record.get("class_mismatch", False))

    # ── Phase 3: follow-up tools ───────────────────────────────────────────

    @classmethod
    def _followup_tools(cls, llm, prompt: str, records: list,
                        last_results: list, country_code: str,
                        snapshot_date: str, event_callback) -> list:
        """Optional LLM-selected nameSearch / structuredSearch follow-ups.

        Uses native Ollama tool calling (``tools`` in the request,
        ``tool_calls`` in the response) instead of prompt-injected JSON;
        the tools schema lives in ``RESEARCH_TOOLS`` and each executed tool
        is traced as an ``execute_tool`` span by ``_call_search_tool``.
        """
        if not _FOLLOWUP_ENABLED:
            return []

        from semantic_search.services.query_enrichment_service import (
            QueryEnrichmentService,
        )

        _, calls = llm.chat_tools(
            [
                {"role": "system", "content": _FOLLOWUP_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"Research request: {prompt}\n"
                        f"Answers so far:\n{cls._answers_text(records)}\n"
                        "Select 0-2 follow-up searches that would fill gaps."
                    ),
                },
            ],
            tools=QueryEnrichmentService._research_tools_schema(),
            temperature=0.0,
            max_tokens=300,
        )

        tool_calls = []
        for tool, args in QueryEnrichmentService._validate_tool_calls(calls):
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
                  coverage: list, event_callback, recipe: dict = None,
                  country_code: str = None) -> str:
        """Final summary grounded in the collected primary answers.

        The coverage ledger rides along so the assembler writes every
        slot's section and declares gaps explicitly instead of papering
        over them (or silently skipping them). The summary's sections
        come from the recipe. Always non-thinking: the content is already
        grounded, thinking only adds latency.
        """
        user_content = (
            f"Research request: {prompt}\n"
            "Answers from the geospatial engine (each is a deterministic "
            "templated answer):\n"
            f"{cls._answers_text(records)}\n"
        )
        if coverage:
            user_content += (
                "Coverage ledger (slot: status):\n"
                + "\n".join(
                    f"- {c['slot']}: {c['status']}"
                    + (f" ({c['result_count']} results)"
                       if c["status"] in ("filled", "filled_by_repair") else "")
                    + (" [results are not the asked class]"
                       if c["class_mismatch"]
                       and c["status"] != "class_mismatch" else "")
                    + (" [distances unreliable]" if c["degenerate_distances"] else "")
                    for c in coverage
                )
                + "\n"
            )
        if tool_calls:
            tool_lines = []
            for call in tool_calls:
                tool_lines.append(
                    f"- {call['tool']} {json.dumps(call['args'])}: "
                    f"{json.dumps(call.get('output'), ensure_ascii=False)}"
                )
            user_content += "Follow-up tool outputs:\n" + "\n".join(tool_lines)
        derived = cls._derived_tallies(records, country_code)
        if derived:
            user_content += (
                "Derived tallies (computed from all results — cite these "
                "exactly, never estimate):\n" + derived + "\n"
            )
        user_content += "\nWrite the final summary."

        messages = [
            {
                "role": "system",
                "content": _render_assemble_prompt(recipe or get_recipe()),
            },
            {"role": "user", "content": user_content},
        ]

        parts = []
        for delta in llm.chat_stream(
            messages, temperature=0.3, max_tokens=1200, think=False,
        ):
            if event_callback:
                event_callback({"event": "summary_delta", "delta": delta})
            parts.append(delta)
        summary = "".join(parts).strip()
        if not summary:
            summary = llm.chat(
                messages, temperature=0.3, max_tokens=1200, think=False,
            )
            summary = (summary or "").strip()
        return summary or None

    @staticmethod
    def _derived_tallies(records: list, country_code: str = None) -> str:
        """Deterministic aggregates across all result sets (food report,
        2026-10-02): the popular-brands tally — a name repeated across
        places is a chain — and the cuisine-tag split into local vs
        other via COUNTRY_CUISINE. Injected into the assemble context so
        the LLM cites computed counts instead of estimating."""
        brands = Counter()
        cuisines = Counter()
        for record in records or []:
            brands.update(record.get("brand_counts") or {})
            cuisines.update(record.get("cuisine_counts") or {})
        lines = []
        chains = [(n, c) for n, c in brands.items() if c >= 2]
        if chains:
            top = sorted(chains, key=lambda kv: (-kv[1], kv[0]))[:10]
            lines.append(
                "popular brands (name ×locations): "
                + ", ".join(f"{n} ×{c}" for n, c in top)
            )
        if cuisines:
            local_tokens = COUNTRY_CUISINE.get(country_code or "", ())
            local = {
                k: v for k, v in cuisines.items()
                if any(t in k.lower() for t in local_tokens)
            }
            other = {
                k: v for k, v in cuisines.items() if k not in local
            }
            parts = []
            if local:
                parts.append(
                    "local: " + ", ".join(
                        f"{k} ×{v}"
                        for k, v in sorted(
                            local.items(), key=lambda kv: -kv[1])[:6]
                    )
                )
            if other:
                parts.append(
                    "other: " + ", ".join(
                        f"{k} ×{v}"
                        for k, v in sorted(
                            other.items(), key=lambda kv: -kv[1])[:8]
                    )
                )
            if parts:
                lines.append("cuisine mix — " + " | ".join(parts))
        return "\n".join(lines)

    # ── Helpers ────────────────────────────────────────────────────────────

    @staticmethod
    def _answers_text(records: list) -> str:
        """Compact per-question lines for the follow-up and assemble prompts.

        Repaired slots carry their #8 fallback as *nearest evidence* — the
        assembler may cite it as the closest alternative for a missing
        category, never as the category itself.
        """
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
            # Food-report census lines (2026-10-02): the wkgs-class
            # distribution, repeated names (chains), and cuisine-tag
            # counts ride each answer so the assembler cites computed
            # numbers — "173 found: Amenity ×171, Shop ×2".
            classes = record.get("result_classes") or {}
            if classes:
                top = sorted(classes.items(), key=lambda kv: -kv[1])[:4]
                lines.append(
                    "  classes: "
                    + ", ".join(f"{k} ×{v}" for k, v in top)
                )
            chains = [
                (n, c)
                for n, c in (record.get("brand_counts") or {}).items()
                if c >= 2
            ]
            if chains:
                top = sorted(chains, key=lambda kv: -kv[1])[:6]
                lines.append(
                    "  repeated names: "
                    + ", ".join(f"{n} ×{c}" for n, c in top)
                )
            cuisines = record.get("cuisine_counts") or {}
            if cuisines:
                top = sorted(cuisines.items(), key=lambda kv: -kv[1])[:8]
                lines.append(
                    "  cuisine tags: "
                    + ", ".join(f"{k} ×{v}" for k, v in top)
                )
            if record.get("original_question"):
                lines.append(
                    f"  original ask: {record['original_question']}"
                )
            if record.get("repair"):
                lines.append(f"  repair: {record['repair']}")
            if record.get("degenerate_distances"):
                lines.append(
                    "  warning: all distances in this result set are "
                    "identical — do not cite distances for this slot"
                )
            if record.get("evidence_digest"):
                lines.append(
                    f"  nearest evidence ({record.get('evidence_question')}): "
                    f"{record['evidence_digest']}"
                )
        return "\n".join(lines)
