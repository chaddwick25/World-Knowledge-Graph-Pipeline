"""
QueryParserService — MapQA TF-IDF parser (control plane).

Parses a natural-language geospatial question into a GeoFlow Graph spec:
  1. Classify the question into one of 5 macro-templates (TF-IDF + MultinomialNB)
  2. Extract spatial core concepts (multi-label OneVsRest Logistic Regression)
  3. Assign functional roles to each concept (multi-class Logistic Regression)
  4. Compose the DAG from the template skeleton + concepts + roles
  5. Validate the role-precedence invariant (G2: SUB_COND ≺ COND ≺ SUPPORT ≺ MEASURE)

This is the control plane. It does NOT touch the data plane (FastText, PostGIS).
The executor (QueryExecutorService) consumes the parsed output and runs the
data-plane operators.

Implements MAPQA_TO_EXECUTION_PLAN.md §1.2.2 and MAPQA_PARSER_BUILD_ORDER.md §7.
"""

import json
import logging
import pickle
import re
from pathlib import Path

from django.conf import settings

logger = logging.getLogger(__name__)

# Role precedence — numeric, not lexicographic ([SPATIAL_AGENT:§B.3] Eq. 7)
ROLE_ORDER = {
    "EXTENT": 0, "TEXTENT": 0,
    "SUB_COND": 1, "COND": 2,
    "SUPPORT": 3, "MEASURE": 4,
}

# Core concept types (must match train_mapqa_parser.CONCEPT_TYPES)
CONCEPT_TYPES = [
    "LOCATION", "OBJECT", "FIELD", "EVENT",
    "NETWORK", "AMOUNT", "PROPORTION",
]


class QueryParserService:
    """Parse a natural-language geospatial question into a DAG spec.

    Singleton — model artifacts are loaded once at first use.
    """

    _instance = None

    # Words that should never be treated as named entities in the regex fallback.
    _QUESTION_WORDS = frozenset({
        "Which", "What", "Where", "When", "Who", "Why", "How",
        "Is", "Are", "Was", "Were", "Do", "Does", "Did", "Can", "Could",
        "Would", "Will", "Shall", "There", "Their", "Then", "Than",
        "Has", "Have", "Had",
    })

    # Cardinal directions must never be extracted as entity names — they are
    # spatial modifiers in 5b cone-search queries ("What is west of X?").
    # Without this exclusion, the multi-entity supplement steals the cardinal
    # as a second LOCATION, bypassing the 5b cone-search branch and computing
    # a meaningless bearing from a place that happens to be named "West"/"East".
    _CARDINAL_DIRECTIONS = frozenset({
        "North", "South", "East", "West",
        "Northeast", "Southeast", "Southwest", "Northwest",
        "N", "S", "E", "W", "NE", "SE", "SW", "NW",
    })

    def __init__(self):
        data_dir = Path(settings.MAPQA_PARSER_DATA_DIR)
        art = data_dir / "artifacts"
        self.vectorizer = self._load(art / "vectorizer.pkl")
        self.classifier = self._load(art / "template_classifier.pkl")
        self.label_encoder = self._load(art / "label_encoder.pkl")
        self.template_specs = self._load_json(art / "template_specs.json")
        self.amenity_vocab = self._load_json(art / "amenity_vocab.json")
        # Concept/role models are optional (trained with --no-concept-models skips them)
        self.concept_extractor = self._load_optional(art / "concept_extractor.pkl")
        self.role_assigner = self._load_optional(art / "role_assigner.pkl")
        self.role_encoder = self._load_optional(art / "role_encoder.pkl")
        logger.info("QueryParserService loaded (TF-IDF + MultinomialNB)")

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls):
        """Force reload on next get_instance() — useful for tests."""
        cls._instance = None

    # ── Public API ──────────────────────────────────────────────────────────

    def parse(self, question: str) -> dict:
        """Parse a NL question → {template, concepts, roles, dag, confidence, validation}.

        Following [SPATIAL_AGENT:§3.2-3.4]:
          1. Classify template (retrieval-augmented orchestration)
          2. Extract spatial core concepts
          3. Assign functional roles
          4. Compose GeoFlow Graph
          5. Validate well-formedness (G2)
        """
        # Stage 1: classify template
        X = self.vectorizer.transform([question])
        label_idx = self.classifier.predict(X)[0]
        template = self.label_encoder.inverse_transform([label_idx])[0]
        confidence = float(max(self.classifier.predict_proba(X)[0]))

        # Stage 1b: deterministic override — "within X(km|m) of" is always
        # FILTER-AGGREGATE-MEASURE regardless of what the TF-IDF classifier
        # says. The classifier sometimes misclassifies these as
        # PLACE-ATTRIBUTE-QUERY when the amenity word dominates the signal.
        if re.search(r"\bwithin\s+\d+\s*(km|m)\b", question, re.IGNORECASE):
            if template != "FILTER-AGGREGATE-MEASURE (#1)":
                logger.info(
                    "Template override: %s → FILTER-AGGREGATE-MEASURE (#1) "
                    "(matched 'within Xkm' pattern)", template
                )
                template = "FILTER-AGGREGATE-MEASURE (#1)"

        # Stage 2: extract concepts
        concepts = self._extract_concepts(question, template)

        # Stage 2b: LLM refinement for low-confidence parses. The trained
        # TF-IDF/NB classifier is confident on template-bearing phrasings but
        # degrades on novel wording; when its confidence is below the
        # threshold, ask the platform LLM (Ollama — see
        # core/services/llm_service.py) to re-classify template + concepts.
        # Any failure, invalid output, or disabled model keeps the heuristic
        # parse — the LLM is an accelerator, never a single point of failure.
        if confidence < self._llm_fallback_threshold():
            refined = self._llm_refine(question, template, concepts)
            if refined is not None:
                template, concepts = refined
                # Re-apply the deterministic radius override invariant — the
                # LLM must not route "within X of" away from
                # FILTER-AGGREGATE-MEASURE (#1).
                if re.search(r"\bwithin\s+\d+\s*(km|m)\b", question, re.IGNORECASE):
                    template = "FILTER-AGGREGATE-MEASURE (#1)"

        # Stage 3: assign roles
        roles = self._assign_roles(question, template, concepts)

        # Stage 4: compose DAG
        dag = self._compose_dag(template, concepts, roles)

        # Stage 5: validate
        validation = self._validate_dag(dag)
        if not validation["valid"]:
            logger.error("DAG validation failed: %s", validation["violations"])

        return {
            "template": template,
            "concepts": concepts,
            "roles": roles,
            "dag": dag,
            "confidence": confidence,
            "validation": validation,
        }

    # ── Concept extraction ──────────────────────────────────────────────────

    def _extract_concepts(self, question: str, template: str) -> list:
        """Extract spatial core concepts from the question.

        Uses the trained multi-label classifier if available, plus
        deterministic span extraction for each detected concept type.
        """
        if self.concept_extractor is not None:
            X = self.vectorizer.transform([question])
            concept_vec = self.concept_extractor.predict(X)[0]
            concept_probs = self.concept_extractor.predict_proba(X)[0]
            # Safety net: the trained multi-label classifier misses rare
            # amenity vocabulary — e.g. "bus station within 50km of X" gets
            # OBJECT prob 0.4963, just under the 0.5 decision threshold,
            # because "station" is dominated by LOCATION usage in the MapQA
            # training data. OR the deterministic heuristic presence back in
            # so a near-threshold miss is recovered. Span extraction is
            # open-vocabulary, so extracting a concept is always safe.
            heuristic_vec, heuristic_probs = self._heuristic_concepts(question, template)
            concept_vec = [int(a or b) for a, b in zip(concept_vec, heuristic_vec)]
            concept_probs = [max(a, b) for a, b in zip(concept_probs, heuristic_probs)]
        else:
            # Fallback: heuristic concept detection
            concept_vec, concept_probs = self._heuristic_concepts(question, template)

        concepts = []
        for i, present in enumerate(concept_vec):
            if present:
                ctype = CONCEPT_TYPES[i]
                text_span = self._extract_concept_span(question, ctype, template)
                concepts.append({
                    "type": ctype,
                    "text": text_span,
                    "confidence": float(concept_probs[i]) if i < len(concept_probs) else 1.0,
                    "resolved_value": None,  # filled by executor
                })
        return concepts

    def _heuristic_concepts(self, question: str, template: str) -> tuple:
        """Fallback concept detection when trained model is unavailable."""
        q_lower = question.lower()
        present = [0] * len(CONCEPT_TYPES)
        probs = [0.0] * len(CONCEPT_TYPES)

        if re.search(r"\d+\s*m\b", q_lower) or "how far" in q_lower:
            idx = CONCEPT_TYPES.index("AMOUNT")
            present[idx] = 1
            probs[idx] = 0.9
        if any(sig in q_lower for sig in
               ("bar", "restaurant", "cafe", "hotel", "school", "hospital",
                "shop", "amenity", "pub", "bank", "pharmacy",
                "bus", "station", "train", "taxi", "airport", "ferry",
                # 2026-09-19: "Which museums are within 2km of Belfast?"
                # extracted no OBJECT (trained model missed it, list below
                # lacked it) → the executor skipped the search entirely and
                # the research summary reported "no museums found".
                # The documented sample_questions.md amenity vocabulary:
                "museum", "beach", "park", "supermarket", "gas station",
                "bakery", "library", "cinema", "clinic", "church",
                "university", "gallery", "theatre", "theater", "stadium",
                "swimming", "playground", "brewery", "distillery")):
            idx = CONCEPT_TYPES.index("OBJECT")
            present[idx] = 1
            probs[idx] = 0.85
        if any(sig in q_lower for sig in
               ("near", "of", "from", "to", "around", "by", "beside",
                "closer", "how far")):
            idx = CONCEPT_TYPES.index("LOCATION")
            present[idx] = 1
            probs[idx] = 0.8
        if any(sig in q_lower for sig in ("amenity", "available", "present", "attribute")):
            idx = CONCEPT_TYPES.index("FIELD")
            present[idx] = 1
            probs[idx] = 0.7
        if not any(present):
            idx = CONCEPT_TYPES.index("LOCATION")
            present[idx] = 1
            probs[idx] = 0.5
        return present, probs

    def _extract_concept_span(self, question: str, concept_type: str,
                              template: str) -> str:
        """Extract the text phrase that instantiates a concept."""
        if concept_type == "AMOUNT":
            m = re.search(r"(\d+)\s*m\b", question, re.I)
            if m:
                return f"{m.group(1)}m"
            m = re.search(r"(\d+)\s*km\b", question, re.I)
            if m:
                return f"{m.group(1)}km"
            return None

        if concept_type == "OBJECT":
            lower = question.lower()
            # Split on prepositions — the OBJECT is what comes BEFORE the
            # preposition (the search target), the LOCATION is what comes
            # AFTER (the anchor).  This prevents "cafe near a school" from
            # matching "school" as the OBJECT.
            #
            # Find the FIRST preposition in the text (not iterate in order)
            # so "chinese food within 100m of a park" splits at "within",
            # not at "of".
            preps = ("near", "around", "by", "beside", "of", "from", "to",
                     "within", "close to", "next to", "right by", "adjacent to")
            object_part = lower
            earliest_match = None
            for prep in preps:
                pattern = rf"\b{prep}\b"
                m = re.search(pattern, lower)
                if m and (earliest_match is None or m.start() < earliest_match.start()):
                    earliest_match = m
            if earliest_match:
                object_part = lower[:earliest_match.start()].strip()

            # Closed-vocabulary slot match: try amenity_vocab first.
            # This handles "restaurant", "cafe", "bus_station", etc.
            # ([MAPQA_BUILD:§3.2] — closed-vocab slots are exact-match)
            # Vocab entries use underscores ("bus_station") while the
            # extracted span is space-separated ("bus station") — compare
            # normalized forms so multi-word amenities match, and return the
            # canonical vocab form so the executor's exact-tag tier hits the
            # real OSM tag value.
            best = None
            for amenity in self.amenity_vocab:
                if amenity.lower().replace("_", " ") in object_part:
                    if best is None or len(amenity) > len(best):
                        best = amenity
            if best is not None:
                return best

            # Open-vocabulary slot: no amenity_vocab match found.
            # Return the raw phrase before the preposition.  The executor's
            # data-plane 3-tier fallback (exact tag → ontology class →
            # FastText semantic) will resolve it to OSM entities.
            # ([MAPQA_BUILD:§3.2] — open-vocab slots are extraction problems;
            #  [MAPQA_TO_EXECUTION_PLAN:§4.1] — parser is control plane,
            #  FastText is data plane, they don't share models)
            #
            # Strip leading question words ("what", "which", "is", "are")
            # to get the clean noun phrase.
            stripped = re.sub(
                r"^(what|which|is|are|the|a|an)\s+", "", object_part
            ).strip()
            if stripped:
                return stripped
            return object_part if object_part else None

        if concept_type == "LOCATION":
            return self._extract_entity_name(question, template)

        if concept_type == "FIELD":
            if "amenity" in question.lower():
                return "amenity"
            return None

        return None

    @staticmethod
    def _levenshtein(a: str, b: str) -> int:
        """Plain edit distance (parser is control plane — no DB)."""
        if len(a) < len(b):
            a, b = b, a
        prev = list(range(len(b) + 1))
        for i, ca in enumerate(a, 1):
            cur = [i]
            for j, cb in enumerate(b, 1):
                cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
            prev = cur
        return prev[-1]

    @staticmethod
    def _normalize_question_words(question: str) -> str:
        """Correct near-miss question words ("Whichs" → "Which") before
        pattern matching and entity extraction.

        A typo'd question word defeats the compare-closer patterns AND sneaks
        through the capitalized-token fallback as a named entity, corrupting
        multi-entity extraction. Regression: "Whichs is closer to Moher
        Cottage: Cliff Coast Coffee or the Cliffs of Moher?" extracted
        'Whichs' as an entity, cascading into a wrong anchor ("Moher" →
        "Moher West") and a truncated-span false positive ("Cliffs" →
        "Cliffs of Howth").
        """
        out = []
        for tok in question.split():
            stripped = tok.strip(",.!?;:")
            punct = tok[len(stripped):]
            if len(stripped) >= 4:
                low = stripped.lower()
                for qw in QueryParserService._QUESTION_WORDS:
                    qw_low = qw.lower()
                    # The typo must preserve the question word as a PREFIX
                    # (or be a prefix of it): "Whichs" ⊃ "Which", "Wich" ⊂
                    # "Which". This excludes false positives where an entity
                    # word is merely edit-close ("Mill" → "Will", "What" →
                    # "Who" — neither is a prefix of the other).
                    prefix_linked = (
                        low.startswith(qw_low) or qw_low.startswith(low)
                    )
                    if prefix_linked and low != qw_low and \
                            QueryParserService._levenshtein(low, qw_low) <= 2:
                        stripped = qw
                        break
            out.append(stripped + punct)
        return " ".join(out)

    @staticmethod
    def _extract_entity_name(question: str, template: str) -> str:
        """Extract proper-noun entity names from the question.

        For templates with two entities (compare-closer, distance, direction),
        returns the first entity. The executor handles multi-entity extraction
        separately.
        """
        # Remove signal phrases and extract the remainder
        q = QueryParserService._normalize_question_words(question.strip())
        # Common patterns: "X near Y", "X within 50m of Y", "how far is X from Y"
        # Find the EARLIEST preposition so the structural anchor wins over a
        # later "of" ("Which is closer to Moher Cottage: ... the Cliffs OF
        # Moher?" must anchor on "Moher Cottage", not "Moher"). ":" terminates
        # the span so "X: A or B" compare phrasings stop at the colon.
        preps = ("of", "from", "to", "near", "around", "by", "beside")
        earliest = None
        for prep in preps:
            m = re.search(rf"\b{prep}\s+", q, re.I)
            if m and (earliest is None or m.start() < earliest.start()):
                earliest = m
        if earliest:
            rest = q[earliest.end():]
            m2 = re.search(r"(.+?)(?:\?|$|,|:|\bwithin\b|\bhow\b)", rest)
            entity = (m2.group(1) if m2 else rest).strip().rstrip("?").strip()
            if entity:
                return entity
        # Fallback: capitalized tokens
        tokens = q.replace("?", "").split()
        caps = [t for t in tokens if t[0].isupper() and len(t) > 2]
        if caps:
            return " ".join(caps[:3])
        return None

    @staticmethod
    def extract_all_entities(question: str) -> list:
        """Extract all entity names from a question (for multi-entity templates).

        Handles "How far is X from Y?" and "Which is closer to Z: X or Y?"
        Returns a list of entity name strings.
        """
        q = QueryParserService._normalize_question_words(
            question.strip().rstrip("?")
        )

        # Pattern 1: "How far is X from Y?"
        m = re.match(r"how far is\s+(.+?)\s+from\s+(.+)$", q, re.I)
        if m:
            return [m.group(1).strip(), m.group(2).strip()]

        # Pattern 2: "Which direction is X from Y?"
        m = re.match(r"which direction is\s+(.+?)\s+from\s+(.+)$", q, re.I)
        if m:
            return [m.group(1).strip(), m.group(2).strip()]

        # Pattern 3: "Which/What is closer to Z: X or Y?" (or "X, Y or Z")
        m = re.match(
            r"(?:which|what)\s+is closer to\s+(.+?):\s*(.+?)\s+or\s+(.+)$",
            q, re.I,
        )
        if m:
            return QueryParserService._compare_candidates(
                m.group(2), m.group(3), m.group(1),
            )

        # Pattern 4: "Which/What X is closer to Y: A or B?"
        m = re.match(
            r"(?:which|what)\s+.+?\s+is closer to\s+(.+?):\s*(.+?)\s+or\s+(.+)$",
            q, re.I,
        )
        if m:
            return QueryParserService._compare_candidates(
                m.group(2), m.group(3), m.group(1),
            )

        # Pattern 5: "Which X is nearest/closest to Y?"
        m = re.match(r"which\s+(.+?)\s+is\s+(?:the\s+)?(?:nearest|closest)\s+to\s+(.+)$", q, re.I)
        if m:
            # The anchor (Y) is the only named place; X is the OBJECT concept.
            return [m.group(2).strip()]

        # Fallback: extract capitalized token sequences, skipping question words
        # and cardinal directions (which are spatial modifiers, not place names).
        _skip = QueryParserService._QUESTION_WORDS | QueryParserService._CARDINAL_DIRECTIONS
        tokens = q.split()
        entities = []
        current = []
        for t in tokens:
            stripped = t.strip(",.!?;:")
            if stripped and stripped[0].isupper() and len(stripped) > 2 \
                    and stripped not in _skip:
                current.append(stripped)
            else:
                if current:
                    entities.append(" ".join(current))
                    current = []
        if current:
            entities.append(" ".join(current))
        return entities

    @staticmethod
    def _compare_candidates(group2: str, group3: str, anchor: str) -> list:
        """Build [candidates..., anchor] for 'X: A or B' phrasings.

        Splits comma lists in the first candidate group so
        "X: A, B or C" yields three candidates.
        """
        candidates = [
            c.strip() for c in re.split(r",|\s+or\s+", group2) if c.strip()
        ]
        candidates.append(group3.strip())
        candidates.append(anchor.strip())
        return candidates

    # ── Role assignment ─────────────────────────────────────────────────────

    def _assign_roles(self, question: str, template: str,
                      concepts: list) -> list:
        """Assign functional roles to extracted concepts."""
        if self.role_assigner is not None and self.role_encoder is not None:
            return self._ml_role_assignment(question, concepts)
        # Fallback: deterministic per-template mapping
        return self._deterministic_role_assignment(template, concepts)

    def _ml_role_assignment(self, question: str, concepts: list) -> list:
        roles = []
        for concept in concepts:
            combined = f"{question} [CONCEPT:{concept['type']}]"
            X = self.vectorizer.transform([combined])
            role_idx = self.role_assigner.predict(X)[0]
            role = self.role_encoder.inverse_transform([role_idx])[0]
            role_conf = float(max(self.role_assigner.predict_proba(X)[0]))
            roles.append({
                "concept_type": concept["type"],
                "concept_text": concept["text"],
                "role": role,
                "role_confidence": role_conf,
            })
        roles.sort(key=lambda r: ROLE_ORDER.get(r["role"], 99))
        return roles

    @staticmethod
    def _deterministic_role_assignment(template: str, concepts: list) -> list:
        """Fallback: use the template→concept→role mapping from training."""
        from .query_parser_service import _TEMPLATE_ROLE_MAP_FALLBACK
        role_map = _TEMPLATE_ROLE_MAP_FALLBACK.get(template, {})
        roles = []
        for concept in concepts:
            role = role_map.get(concept["type"], "SUPPORT")
            roles.append({
                "concept_type": concept["type"],
                "concept_text": concept["text"],
                "role": role,
                "role_confidence": 1.0,
            })
        roles.sort(key=lambda r: ROLE_ORDER.get(r["role"], 99))
        return roles

    # ── DAG composition ─────────────────────────────────────────────────────

    def _compose_dag(self, template: str, concepts: list,
                     roles: list) -> list:
        """Compose a DAG from the template skeleton + concepts + roles.

        The template provides the structure (which roles exist, in what order).
        The concepts provide the content (what fills each role).
        """
        spec = self.template_specs.get(template, {"nodes": []})
        dag = []
        role_to_concept = {r["role"]: r for r in roles
                           if r["role"] in ROLE_ORDER and r["role"] != "MEASURE"}

        for node_spec in spec["nodes"]:
            node = dict(node_spec)
            role = node["role"]
            # Bind concept to this node
            bound = False
            if role in role_to_concept:
                rc = role_to_concept[role]
                node["concept_type"] = rc["concept_type"]
                node["concept_text"] = rc["concept_text"]
                node["resolved_value"] = None
                bound = True
            if not bound:
                # Try to find a concept matching the node's expected type
                for c in concepts:
                    if c["type"] == node_spec.get("concept_type"):
                        node["concept_type"] = c["type"]
                        node["concept_text"] = c["text"]
                        node["resolved_value"] = None
                        break
                else:
                    node["concept_type"] = node_spec.get("concept_type")
                    node["concept_text"] = None
                    node["resolved_value"] = None
            dag.append(node)
        return dag

    # ── Validation ──────────────────────────────────────────────────────────

    @staticmethod
    def _validate_dag(dag: list) -> dict:
        """Validate G2: role ordering must be monotonic non-decreasing."""
        violations = []
        roles = [node["role"] for node in dag]
        for i in range(len(roles) - 1):
            if ROLE_ORDER.get(roles[i], 99) > ROLE_ORDER.get(roles[i + 1], 99):
                violations.append(
                    f"G2: role {roles[i]} → {roles[i+1]} violates precedence"
                )
        return {
            "valid": len(violations) == 0,
            "violations": violations,
            "constraints_checked": ["G2"],
            "constraints_satisfied_by_construction": ["G1", "G3", "G4", "G5"],
        }

    # ── LLM refinement ─────────────────────────────────────────────────────

    @staticmethod
    def _llm_fallback_threshold() -> float:
        """Confidence below which the LLM may refine a parse."""
        try:
            return float(
                getattr(settings, "MAPQA_LLM_FALLBACK_CONFIDENCE", "") or 0.5
            )
        except (TypeError, ValueError):
            return 0.5

    def _llm_refine(self, question: str, template: str, concepts: list):
        """Ask the LLM to re-classify a low-confidence parse.

        Returns (template, concepts) on success, None on any failure — the
        heuristic parse is always retained as the fallback. The LLM's template
        is validated against the trained classifier's label set, and concept
        types against CONCEPT_TYPES, so garbage output cannot corrupt the DAG.
        """
        try:
            from core.services.llm_service import LLMService
            llm = LLMService.get_instance()
            if not llm.is_available():
                return None

            # sklearn's classes_ are a numpy array — normalize to plain str so
            # `not in` works, and check `is not None` (NOT truthiness, which
            # raises "ambiguous truth value" on multi-element arrays).
            classes = getattr(self.label_encoder, "classes_", None)
            valid_templates = [str(t) for t in classes] if classes is not None else []
            if not valid_templates:
                valid_templates = [template]  # no label set → at least keep current

            heuristic_summary = ", ".join(
                f"{c.get('type')}: {c.get('text') or '(none)'}" for c in concepts
            ) or "(none)"

            data = llm.chat_json([
                {
                    "role": "system",
                    "content": (
                        "You classify geospatial questions for the MapQA "
                        "parser. Respond with STRICT JSON only: "
                        '{"template": "<exact template name>", '
                        '"concepts": [{"type": "LOCATION|OBJECT|FIELD|EVENT|'
                        'NETWORK|AMOUNT|PROPORTION", "text": "<span or null>"}]}. '
                        "Use only the exact template names given. Do not add "
                        "prose or markdown."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Question: {question}\n"
                        f"Heuristic template: {template}\n"
                        f"Heuristic concepts: {heuristic_summary}\n"
                        f"Valid template names: {', '.join(valid_templates)}\n"
                        "Re-classify the template and extract concepts."
                    ),
                },
            ], temperature=0.0, max_tokens=300)
            if not data or not isinstance(data, dict):
                return None

            new_template = data.get("template")
            if new_template not in valid_templates:
                logger.info(
                    "LLM refine rejected: template %r not in label set", new_template
                )
                return None

            new_concepts = []
            for c in data.get("concepts") or []:
                if not isinstance(c, dict):
                    continue
                ctype = c.get("type")
                ctext = (c.get("text") or "").strip() or None
                if ctype in CONCEPT_TYPES:
                    new_concepts.append({
                        "type": ctype,
                        "text": ctext,
                        "confidence": 1.0,
                        "resolved_value": None,
                    })
            if not new_concepts:
                return None

            logger.info(
                "LLM parser refinement applied: %s → %s (%d concepts)",
                template, new_template, len(new_concepts),
            )
            return new_template, new_concepts
        except Exception as exc:  # noqa: BLE001 — refinement must never break parse
            logger.warning("LLM parser refinement failed; keeping heuristic parse: %s", exc)
            return None

    # ── Loading helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _load(path: Path):
        with open(path, "rb") as f:
            return pickle.load(f)

    @staticmethod
    def _load_optional(path: Path):
        if not path.exists():
            logger.warning("Optional artifact not found: %s", path)
            return None
        with open(path, "rb") as f:
            return pickle.load(f)

    @staticmethod
    def _load_json(path: Path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)


# Fallback template→concept→role mapping (matches train_mapqa_parser.TEMPLATE_ROLE_MAP)
_TEMPLATE_ROLE_MAP_FALLBACK = {
    "FILTER-AGGREGATE-MEASURE (#1)": {
        "OBJECT": "SUB_COND", "AMOUNT": "COND",
        "LOCATION": "SUPPORT", "FIELD": "MEASURE",
    },
    "GEOCODE-BATCH-COMPARE (#4)": {
        "OBJECT": "SUB_COND", "LOCATION": "SUPPORT",
        "AMOUNT": "MEASURE",
    },
    "PLACE-ATTRIBUTE-QUERY (#8)": {
        "OBJECT": "SUB_COND", "FIELD": "SUPPORT",
        "LOCATION": "MEASURE",
    },
    "LOCATION-BEARING-CLASSIFY (#5)": {
        "LOCATION": "SUB_COND", "AMOUNT": "MEASURE",
    },
    "OBJECT-FIELD-MEASURE (#2)": {
        "LOCATION": "SUB_COND", "AMOUNT": "MEASURE",
    },
}
