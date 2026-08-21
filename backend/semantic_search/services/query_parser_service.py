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
                "shop", "amenity", "pub", "bank", "pharmacy")):
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
            best = None
            for amenity in self.amenity_vocab:
                if amenity.lower() in object_part:
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
    def _extract_entity_name(question: str, template: str) -> str:
        """Extract proper-noun entity names from the question.

        For templates with two entities (compare-closer, distance, direction),
        returns the first entity. The executor handles multi-entity extraction
        separately.
        """
        # Remove signal phrases and extract the remainder
        q = question.strip()
        # Common patterns: "X near Y", "X within 50m of Y", "how far is X from Y"
        # Try to find the anchor entity after prepositions
        for prep in ("of", "from", "to", "near", "around", "by", "beside"):
            pattern = rf"\b{prep}\s+(.+?)(?:\?|$|,|\bwithin\b|\bhow\b)"
            m = re.search(pattern, q, re.I)
            if m:
                entity = m.group(1).strip().rstrip("?").strip()
                if entity:
                    return entity
        # Fallback: capitalized tokens
        tokens = question.replace("?", "").split()
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
        q = question.strip().rstrip("?")

        # Pattern 1: "How far is X from Y?"
        m = re.match(r"how far is\s+(.+?)\s+from\s+(.+)$", q, re.I)
        if m:
            return [m.group(1).strip(), m.group(2).strip()]

        # Pattern 2: "Which direction is X from Y?"
        m = re.match(r"which direction is\s+(.+?)\s+from\s+(.+)$", q, re.I)
        if m:
            return [m.group(1).strip(), m.group(2).strip()]

        # Pattern 3: "Which is closer to Z: X or Y?"
        m = re.match(r"which is closer to\s+(.+?):\s*(.+?)\s+or\s+(.+)$", q, re.I)
        if m:
            return [m.group(2).strip(), m.group(3).strip(), m.group(1).strip()]

        # Pattern 4: "Which X is closer to Y: A or B?"
        m = re.match(r"which .+? is closer to\s+(.+?):\s*(.+?)\s+or\s+(.+)$", q, re.I)
        if m:
            return [m.group(2).strip(), m.group(3).strip(), m.group(1).strip()]

        # Fallback: extract capitalized token sequences
        tokens = q.split()
        entities = []
        current = []
        for t in tokens:
            stripped = t.strip(",.!?;:")
            if stripped and stripped[0].isupper() and len(stripped) > 2:
                current.append(stripped)
            else:
                if current:
                    entities.append(" ".join(current))
                    current = []
        if current:
            entities.append(" ".join(current))
        return entities

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
