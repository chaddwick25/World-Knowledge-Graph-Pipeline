"""
mapqa_paraphraser.py — lexical variation for self-supervised MapQA rows.

Two tiers ([MAPQA_TEMPLATE_COVERAGE_EXPANSION_PLAN.md] §4):

1. **Rule-based (always on)** — synonym maps and voice/reorder transforms
   applied to the filled question. Each transform is written so it only
   touches function words around the slots, never the slot content itself;
   slot-invariance validation is still applied as a safety net.
2. **LLM-based (optional)** — Ollama via ``LLMService`` (platform LLM, no
   external API), gated by ``MAPQA_LLM_AUGMENTATION_ENABLED``. 3–5
   paraphrases per seed question. **Validation**: any paraphrase that does
   not contain every slot value verbatim (entity names, radius numbers) is
   rejected — slot drift breaks ground truth.

Determinism: rule-based output is a pure function of (question, slots);
only the LLM tier is non-deterministic and it is off by default.
"""

import logging
import re

from django.conf import settings

logger = logging.getLogger(__name__)

# ── Rule-based transforms ──────────────────────────────────────────────────
# (compiled pattern, replacement). Applied independently to the filled
# question; a transform applies only when its pattern matches. Patterns
# capture slot-adjacent structure without altering slot values.
_RULE_TRANSFORMS = [
    # radius phrasing (#1)
    (re.compile(r"\bwithin\s+(\d+\s*m)\s+of\b", re.I), r"inside \1 of"),
    (re.compile(r"\bwithin\s+(\d+\s*m)\s+of\b", re.I), r"no more than \1 from"),
    (re.compile(r"\bwithin\s+(\d+\s*m)\s+of\b", re.I), r"within \1 from"),
    # nearest ↔ closest (#4b, #5b)
    (re.compile(r"\bthe nearest\b", re.I), "the closest"),
    (re.compile(r"\bthe closest\b", re.I), "the nearest"),
    # amenity ↔ facility (#8a)
    (re.compile(r"\bamenity\b", re.I), "facility"),
    # adjacency phrasing (#8b)
    (re.compile(r"\bis right by\b", re.I), "is beside"),
    (re.compile(r"\bis beside\b", re.I), "is right by"),
    (re.compile(r"\bis adjacent to\b", re.I), "is next to"),
    # compare-closer (#4a)
    (re.compile(r"\bwhich is closer to\b", re.I), "which spot is closer to"),
    (re.compile(r"\bwhich spot is closer to\b", re.I), "which place is closer to"),
    # bearing phrasing (#5a)
    (re.compile(r"^which direction is\b", re.I), "what direction is"),
    (re.compile(r"^which direction is\b", re.I), "which way is"),
    # distance phrasing (#2)
    (re.compile(r"^how far is\s+(.+?)\s+from\s+(.+?)\s*\??$", re.I),
     r"how far apart are \1 and \2?"),
    (re.compile(r"^what is the distance between\s+(.+?)\s+and\s+(.+?)\s*\??$", re.I),
     r"how far apart are \1 and \2?"),
    (re.compile(r"^how far apart are\s+(.+?)\s+and\s+(.+?)\s*\??$", re.I),
     r"what is the distance between \1 and \2?"),
    # aggregate lead-ins (#1)
    (re.compile(r"^what are the\b", re.I), "which are the"),
    (re.compile(r"^what are the\b", re.I), "list the"),
]

_LLM_SYSTEM_PROMPT = (
    "You paraphrase geospatial questions for MapQA parser training data "
    "augmentation. Keep the meaning identical and keep every named entity, "
    "place name, amenity type, and numeric radius EXACTLY as given (do not "
    "translate, rename, reorder slot values, or change units). Produce 3-5 "
    "variations. Respond with STRICT JSON only: "
    '{"paraphrases": ["...", "..."]}. Do not add prose or markdown.'
)


class MapQAParaphraser:
    """Rule-based + optional LLM paraphrase tier with slot-invariance gate.

    Args:
        llm_enabled: Override for ``MAPQA_LLM_AUGMENTATION_ENABLED``
            (default: read from settings).
        llm_service: Injectable LLM client (must expose ``is_available()``
            and ``chat_json(messages, ...)``) — used by tests.
        llm_max_paraphrases: Max LLM paraphrases kept per seed question.
    """

    def __init__(self, llm_enabled: bool = None, llm_service=None,
                 llm_max_paraphrases: int = 5):
        if llm_enabled is None:
            llm_enabled = bool(getattr(
                settings, "MAPQA_LLM_AUGMENTATION_ENABLED", False
            ))
        self.llm_enabled = llm_enabled
        self._llm_service = llm_service
        self.llm_max_paraphrases = llm_max_paraphrases

    # ── Public API ──────────────────────────────────────────────────────────

    def paraphrase(self, question: str, slots: list) -> list:
        """Return slot-validated paraphrases of ``question``.

        ``slots`` is the list of slot values slotted into the seed question
        (entity names, radius strings, cardinal words). Any candidate —
        rule-based or LLM — that does not contain every slot value verbatim
        is rejected (slot drift breaks ground truth).
        """
        slots = [s for s in (slots or []) if s]
        out = []
        seen = {self._norm(question)}
        for candidate in self._rule_based(question):
            if self._accept(candidate, question, slots, seen):
                out.append(candidate)
                seen.add(self._norm(candidate))
        if self.llm_enabled:
            for candidate in self._llm_paraphrases(question, slots):
                if self._accept(candidate, question, slots, seen):
                    out.append(candidate)
                    seen.add(self._norm(candidate))
        return out

    # ── Tiers ──────────────────────────────────────────────────────────────

    @classmethod
    def _rule_based(cls, question: str) -> list:
        """Apply each transform independently to the original question."""
        variants = []
        for pattern, replacement in _RULE_TRANSFORMS:
            m = pattern.search(question)
            if m:
                variant = pattern.sub(replacement, question, count=1)
                # Preserve sentence capitalization when the transform lowercases
                # the lead-in ("How far is X from Y?" → "How far apart are ...").
                if question and question[0].isupper() and variant and \
                        variant[0].islower():
                    variant = variant[0].upper() + variant[1:]
                variants.append(variant)
        return variants

    def _llm_paraphrases(self, question: str, slots: list) -> list:
        """Ask the platform LLM for 3–5 paraphrases. Fail-soft: any error,
        unparseable output, or unavailable model returns [] — the rule-based
        tier is never replaced by the LLM tier, only extended."""
        try:
            llm = self._llm_service
            if llm is None:
                from core.services.llm_service import LLMService
                llm = LLMService.get_instance()
            if not llm.is_available():
                return []
            data = llm.chat_json([
                {"role": "system", "content": _LLM_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"Question: {question}\n"
                        f"Slot values that must appear verbatim: "
                        f"{', '.join(slots) or '(none)'}"
                    ),
                },
            ], temperature=0.7, max_tokens=400)
            if not isinstance(data, dict):
                return []
            paraphrases = data.get("paraphrases") or []
            if not isinstance(paraphrases, list):
                return []
            return [str(p).strip() for p in paraphrases
                    if isinstance(p, str) and p.strip()][:self.llm_max_paraphrases]
        except Exception as exc:  # noqa: BLE001 — LLM tier must never break generation
            logger.warning("LLM paraphrase tier failed; keeping rule-based only: %s", exc)
            return []

    # ── Validation ──────────────────────────────────────────────────────────

    @classmethod
    def _accept(cls, candidate: str, original: str, slots: list,
                seen: set) -> bool:
        """Slot-invariance gate: every slot value verbatim (case-insensitive),
        candidate differs from the seed, and is not a duplicate."""
        if not candidate or candidate.strip() == "":
            return False
        norm = cls._norm(candidate)
        if norm == cls._norm(original) or norm in seen:
            return False
        lowered = candidate.lower()
        for slot in slots:
            if slot.lower() not in lowered:
                return False
        return True

    @staticmethod
    def _norm(question: str) -> str:
        import re as _re
        q = question.lower().strip().rstrip("?.").strip()
        q = _re.sub(r"[^\w\s]", "", q)
        return _re.sub(r"\s+", " ", q).strip()
