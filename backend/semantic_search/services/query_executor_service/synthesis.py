"""SynthesisMixin — extracted from query_executor_service.py (monolith split, Phase 5)."""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def _qes():
    """Lazy accessor — avoids the service.py <-> mixin import cycle."""
    from semantic_search.services.query_executor_service.service import QueryExecutorService
    return QueryExecutorService


class SynthesisMixin:
    """Mixin providing executor methods to QueryExecutorService."""

    @staticmethod
    def _synthesize_answer(template: str, concepts: list,
                           results: list, trace: list,
                           skip_llm: bool = False) -> str:
        """Generate a grounded natural-language answer.

        LLM-first, template fallback: when the platform LLM (Ollama, see
        core/services/llm_service.py) is available, it composes a grounded
        answer from the results + trace ([SPATIAL_AGENT:§F.2] a = L_gen(q, Σ_M,
        F)). Any failure — model down, timeout, empty LLM output — falls back
        to the deterministic per-template formatter below, so answers never
        break because the local model is unavailable.

        ``skip_llm=True`` bypasses the LLM pass — used when the enrichment
        synthesis (QueryEnrichmentService) will run anyway, so the answer
        is not rewritten twice (one fewer LLM call per request).
        """
        if not skip_llm:
            llm_answer = _qes()._llm_synthesize_answer(
                template, concepts, results, trace,
            )
            if llm_answer:
                return llm_answer

        if not results:
            return "No results found."
        if isinstance(results, dict) and "error" in results:
            return results["error"]
        if isinstance(results, list) and len(results) == 1 and isinstance(results[0], dict) and "error" in results[0]:
            return results[0]["error"]

        count = len(results) if isinstance(results, list) else 1

        if template == "FILTER-AGGREGATE-MEASURE (#1)":
            radius = _qes()._get_concept(concepts, "AMOUNT")
            radius_text = radius["text"] if radius else "the specified radius"
            return f"Found {count} entities within {radius_text}."

        if template == "GEOCODE-BATCH-COMPARE (#4)":
            if results and isinstance(results, list):
                top = results[0]
                dist = top.get("distance_m")
                if dist is not None:
                    return f"Nearest: {top.get('name', 'unknown')} ({dist:.0f}m away)."
                return f"Nearest: {top.get('name', 'unknown')}."

        if template == "PLACE-ATTRIBUTE-QUERY (#8)":
            if results and isinstance(results, list):
                count = len(results)
                names = [r.get("name", "N/A") for r in results[:3]]
                if count == 1:
                    return f"Found: {names[0]}."
                return f"Found {count} places: {', '.join(names)}" + \
                       ("..." if count > 3 else ".")

        if template == "LOCATION-BEARING-CLASSIFY (#5)":
            if results and isinstance(results, list):
                r = results[0]
                if "anchor_name" in r:
                    # 5b Cone search
                    amenity_str = f" {r['requested_amenity']}" if r.get("requested_amenity") else ""
                    name = r.get("name") or (r.get("tags") or {}).get("name") or "unknown"
                    dist = r.get("distance_m")
                    dist_str = f" ({dist:.0f}m away)" if dist is not None else ""
                    return (f"Nearest{amenity_str} {r['direction']} of {r['anchor_name']} is "
                            f"{name}{dist_str}.")
                elif "from" in r and "to" in r and "direction" in r:
                    # 5a Bearing pair
                    return (f"Direction: {r['direction']} "
                            f"({r['bearing_degrees']:.0f}°) "
                            f"from {r['from']} to {r['to']}.")

        if template == "OBJECT-FIELD-MEASURE (#2)":
            if results and isinstance(results, list) and "distance_km" in results[0]:
                r = results[0]
                return (f"Distance: {r['distance_km']:.2f} km "
                        f"({r['distance_m']:.0f} m) "
                        f"from {r['from']} to {r['to']}.")

        if template == "SPECTRAL-ANALYSIS (#11)":
            if isinstance(results, dict) and "algebraic_connectivity" in results:
                return (
                    f"Algebraic connectivity λ₂ = "
                    f"{results['algebraic_connectivity']:.6f}, "
                    f"spectral gap = {results['spectral_gap']:.6f}, "
                    f"signal smoothness = {results.get('signal_smoothness', 0.0):.4f}."
                )

        if template == "TEMPORAL-DRIFT (#12)":
            if isinstance(results, dict) and "spectral_distance" in results:
                return (
                    f"Spectral drift = {results['spectral_distance']:.4f} "
                    f"({results.get('drift_magnitude', 'unknown')}), "
                    f"Δλ₂ = {results['connectivity_delta']:.4f}, "
                    f"Fiedler drift = {results['fiedler_drift']:.4f}."
                )

        if template == "COMMUNITY-DETECT (#13)":
            if isinstance(results, dict) and "community_count" in results:
                q = results.get("modularity")
                if q is not None:
                    return (
                        f"Detected {results['community_count']} communities "
                        f"(modularity Q = {q:.4f})."
                    )
                return f"Detected {results['community_count']} communities."

        if template == "EVENT-DIFFUSION (#14)":
            if isinstance(results, dict) and "affected" in results:
                t_keys = sorted(results["affected"].keys())
                counts = [len(results["affected"][t]) for t in t_keys]
                return (
                    f"Event diffusion from osm_id {results.get('source_osm_id')}: "
                    f"{', '.join(f't={t}→{c} entities' for t, c in zip(t_keys, counts))}."
                )

        return f"Found {count} results."
    @staticmethod
    def _llm_synthesize_answer(template: str, concepts: list,
                               results: list, trace: list) -> Optional[str]:
        """LLM-grounded answer synthesis — fail-soft, returns None on any error.

        Only fires when there is actual result content to ground on (an empty
        or error result adds nothing an LLM can say). The prompt receives a
        compact, fully-serialized context so the LLM cannot hallucinate
        counts or distances beyond what the executor produced.
        """
        if not results or (isinstance(results, dict) and "error" in results):
            return None
        try:
            from core.services.llm_service import LLMService
            llm = LLMService.get_instance()
            if not llm.is_available():
                return None

            concept_lines = []
            for c in concepts:
                if c.get("text"):
                    concept_lines.append(f"{c.get('type')}: {c.get('text')}")
                elif c.get("type"):
                    concept_lines.append(f"{c.get('type')}: (none)")

            result_lines = []
            if isinstance(results, list):
                for r in results[:8]:
                    name = r.get("name") or (r.get("tags") or {}).get("name") or "N/A"
                    bits = [str(name)]
                    if r.get("distance_m") is not None:
                        bits.append(f"{float(r['distance_m']):.0f}m")
                    if r.get("distance_km") is not None:
                        bits.append(f"{float(r['distance_km']):.2f}km")
                    if r.get("score") is not None:
                        bits.append(f"score={r['score']:.3f}")
                    if r.get("wkg_class"):
                        bits.append(str(r["wkg_class"]))
                    result_lines.append(" | ".join(bits))
            elif isinstance(results, dict):
                for k, v in list(results.items())[:8]:
                    result_lines.append(f"{k}: {v}")

            trace_steps = [t.get("step") for t in (trace or []) if t.get("step")]

            messages = [
                {
                    "role": "system",
                    "content": (
                        "You are a geospatial reasoning assistant grounded in "
                        "WorldKG pipeline output. Answer concisely in 1-3 "
                        "sentences using ONLY the provided template, concepts, "
                        "results, and execution trace. Never invent entities, "
                        "distances, counts, or scores. If the results are "
                        "insufficient, say so."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Template: {template}\n"
                        f"Concepts: {', '.join(concept_lines) or '(none)'}\n"
                        f"Results:\n" + ("\n".join(result_lines) or "(none)") +
                        f"\nExecution trace: {', '.join(trace_steps) or '(none)'}\n"
                        "Write the natural-language answer."
                    ),
                },
            ]
            answer = llm.chat(messages, temperature=0.2, max_tokens=200)
            answer = (answer or "").strip()
            return answer or None
        except Exception as exc:  # noqa: BLE001 — answer synthesis must never break execute
            logger.warning("LLM answer synthesis failed; using template formatter: %s", exc)
            return None
