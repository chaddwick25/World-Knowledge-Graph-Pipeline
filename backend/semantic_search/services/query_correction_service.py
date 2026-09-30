"""
QueryCorrectionService — misspelled/mistyped concept correction (Kuhn's Template).

Data plane — the same family as EntityGeocoder / FactorResolutionService.
Scoped to Kuhn's Template (the `execute-query/` MapQA path): the
structured-JSON and natural-name query modes never touch the parser or the
executor, so this layer cannot affect them.

Fixes the failure mode where the parser's open-vocabulary OBJECT extraction
emits a misspelled amenity phrase ("resturant within 50m of X"). That phrase:
  1. misses the parser's closed-vocab amenity matcher,
  2. fails every exact tier in the executor (canonical alias map →
     ontology map → DB existence check),
  3. then degrades the FastText semantic fallback into a search keyed on a
     garbage embedding.

Correction runs against the snapshot's REAL OSM amenity tag vocabulary
(`DISTINCT tags->>'amenity'`), scored in Postgres with two extensions:
  - pg_trgm      similarity()      — typo-tolerant trigram overlap
  - fuzzystrmatch levenshtein()    — bounded edit distance (short words,
                                     transpositions that defeat trigrams)
Both must be enabled on the vectors DB (migrations 0017 + 0019). If either
extension is unavailable the service degrades to a no-op (returns None),
mirroring EntityGeocoder's graceful trigram skip.

Cache: the vocabulary is bounded by the snapshot's distinct tag values and
changes only on pipeline backfill, so it is cached per (snapshot, country)
with a short TTL (same pattern as snapshot_utils.SNAPSHOT_ID_CACHE).
"""

import logging
import time

from worldkg_nca.models import OsmEntity

logger = logging.getLogger(__name__)

# pg_trgm similarity floor — the geocoder uses 0.4 for NAME matching;
# amenity tags are short canonical words, so require a tighter overlap.
MIN_SIMILARITY = 0.5
# fuzzystrmatch: accept at most this many character edits.
MAX_LEVENSHTEIN = 3
# Combined score floor (max of trigram similarity and normalized
# edit-distance ratio). Rejects junk ("zzqxw") and multi-word
# open-vocabulary phrases ("italian food") that are not near any tag.
MIN_SCORE = 0.55

VOCAB_CACHE_TTL_SECONDS = 300


class QueryCorrectionService:
    """Correct misspelled amenity phrases to canonical OSM tag values."""

    _vocab_cache = {}

    # ── Public API ────────────────────────────────────────────────────────

    @classmethod
    def correct_amenity(cls, phrase, country_code=None, snapshot_id=None):
        """Return ``(canonical_tag, score, method)`` or ``None``.

        ``method`` is one of ``"exact"``, ``"pg_trgm"``, ``"fuzzystrmatch"``.
        ``None`` means the phrase is not a near-miss of any real amenity tag
        in the snapshot — callers keep their existing fallback behavior
        (ontology class / FastText semantic).
        """
        if not phrase:
            return None
        norm = phrase.lower().strip()
        if not norm:
            return None

        vocab = cls.amenity_vocabulary(snapshot_id, country_code)
        if not vocab:
            return None

        # Exact pass — the phrase is already a canonical tag (fast path,
        # no SQL scoring needed).
        if norm in vocab:
            return (norm, 1.0, "exact")

        # SQL scoring tier: pg_trgm similarity + fuzzystrmatch levenshtein
        # over the candidate vocabulary, in one pass.
        try:
            best = cls._score_candidates(vocab, norm)
        except Exception as exc:  # noqa: BLE001 — extension unavailable → no-op
            logger.warning(
                "Amenity correction unavailable (%s); keeping phrase %r",
                exc, phrase,
            )
            return None
        if best is None:
            return None
        candidate, sim, lev = best
        lev_ratio = 1.0 - lev / max(len(candidate), len(norm))
        score = max(sim, lev_ratio)
        if score < MIN_SCORE:
            return None
        # The tier that ADMITTED the candidate: the trigram floor decides
        # (both functions run in one pass; levenshtein is the safety net
        # for short words that defeat trigrams).
        method = "pg_trgm" if sim >= MIN_SIMILARITY else "fuzzystrmatch"
        return (candidate, round(score, 4), method)

    @classmethod
    def amenity_vocabulary(cls, snapshot_id, country_code=None):
        """Distinct real OSM amenity tag values in a snapshot (cached)."""
        if not snapshot_id:
            return []
        key = (snapshot_id, (country_code or "").upper())
        cached = cls._vocab_cache.get(key)
        now = time.monotonic()
        if cached and now - cached[0] < VOCAB_CACHE_TTL_SECONDS:
            return cached[1]

        qs = (
            OsmEntity.objects.using("vectors")
            .filter(snapshot_id=snapshot_id, tags__has_key="amenity")
        )
        if country_code:
            qs = qs.filter(country_code=country_code.upper())
        # DISTINCT in SQL — the live snapshot has millions of amenity rows;
        # the distinct value set is only a few hundred.
        vocab = sorted(
            {t for t in qs.values_list("tags__amenity", flat=True).distinct() if t}
        )
        cls._vocab_cache[key] = (now, vocab)
        return vocab

    @classmethod
    def clear_cache(cls):
        """Drop the vocabulary cache (tests, backfill)."""
        cls._vocab_cache = {}

    # ── Internals ─────────────────────────────────────────────────────────

    @staticmethod
    def _score_candidates(vocab, phrase):
        """Score candidates with pg_trgm similarity() + fuzzystrmatch
        levenshtein() in one SQL pass.

        Returns ``(candidate, similarity, levenshtein)`` for the best match
        above the per-tier thresholds, or ``None``.
        """
        from django.db import connections

        values_sql = ",".join(["(%s)"] * len(vocab))
        sql = (
            "SELECT candidate, sim, lev FROM ("
            "  SELECT v.candidate,"
            "         similarity(v.candidate, %s) AS sim,"
            "         levenshtein(v.candidate, %s) AS lev"
            "  FROM (VALUES " + values_sql + ") AS v(candidate)"
            ") s"
            " WHERE sim >= %s OR lev <= %s"
            " ORDER BY sim DESC, lev ASC"
            " LIMIT 1"
        )
        params = [phrase, phrase] + list(vocab) + [MIN_SIMILARITY, MAX_LEVENSHTEIN]
        with connections["vectors"].cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        if not row:
            return None
        return row[0], float(row[1]), int(row[2])
