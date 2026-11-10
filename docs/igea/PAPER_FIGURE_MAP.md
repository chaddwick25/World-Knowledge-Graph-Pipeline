# Paper figure map

Which paper figure each stage of our implementation corresponds to, and where we
diverge from it. This is the traceability layer: it lets a reader check our code
against the source rather than trusting a paraphrase.

**Papers**

| Key | Paper | Location |
|---|---|---|
| `[IGEA]` | Dsouza, Yu, Windoffer, Demidova, *Iterative Geographic Entity Alignment with Cross-Attention*, 2023 | `docs/papers/WorldKG/` |
| `[USLP]` | *Spatial Link Prediction with Spatial and Semantic Embeddings*, Springer 2023, `10.1007/978-3-031-47240-4_10` | reference repo `gkmn21/SSLPandUSLP` |
| `[GEOVECTORS]` | *GeoVectors: A Linked Open Corpus of OpenStreetMap*, 2021 | `docs/papers/WorldKG/` |

**On images.** Figures are referenced by number, not reproduced. The USLP chapter
is a Springer book chapter and its licence has not been verified; embedding its
figures in this public repository would be redistribution rather than citation.
Check the licence before adding any image asset. The redrawn diagrams in the
subsystem docs carry the same information and can be annotated where we diverge,
which a screenshot cannot.

---

## [IGEA] stage map

| Our stage | Code | Paper reference | Divergence |
|---|---|---|---|
| Seed collection | `_get_seed_alignment` | "already linked entities are used as supervision" | none, but our seeds come from the OSM tag rather than a prior alignment |
| NCA class alignment | `_run_nca` | Algorithm 1, input `n` | none |
| Candidate blocking | `_candidate_pairs` | Algorithm 1, class + distance blocking | none |
| Iterative bootstrap | `run` loop | Algorithm 1, `itr` iterations | none |
| Threshold selection | gate logic | Fig. 5, "Alignment Threshold Tuning" | **we use two gates**, one per scorer; the paper tunes only the model path |
| Score combination | `_score_pair` | cross-attention or cosine fallback | none |

### Fig. 5 in detail

The paper's threshold sweep, quoted directly:

> Fig. 5 depicts the F1-scores obtained after the third iteration for threshold
> values ranging between 0.50 and 0.90 with a gap of 0.1. Overall, the model
> performs well for all threshold values. Comparing the performance of different
> `tha` values, the highest F1-score is achieved with a `tha = 0.60` for both KGs
> across all datasets. Therefore, in the experiments in other parts of this paper,
> we set `tha` to 0.6.

Two things this tells us:

1. The sweep range is 0.50 to 0.90, which is meaningful only for a probability.
   Our cosine path sits in `[-1, 1]` centred near 0, so the same sweep would
   measure nothing.
2. The tuning is against **F1 on ground truth**. We have not run that experiment,
   which is why our cosine constants are engineering choices rather than
   measured values. See `plans/issues/ISSUE-igea-fig5-calibration.md`.

---

## [USLP] stage map

Reference implementation: `USLP/USLP_main.py` in `gkmn21/SSLPandUSLP`.

| Our stage | Code | Paper reference | Divergence |
|---|---|---|---|
| Relation selection | `SPATIAL_LITERAL_TAGS`, `TAG_TO_RELATION` | `geohash_precision_dictionary` | **22 relations vs 14**. We add `addrCity`, `addrVillage`, `addrTown`, `addrNeighbour`, `isInState`, `isInDistrict` |
| Precision choice | `RELATION_GEOHASH_PRECISION` | same dictionary | none |
| Spatial score | `_geo_score` | `space1_geohash_score` | **we add a fixed radius prefilter**; the paper relies on cluster normalisation alone |
| Semantic score | name similarity | `space2_score`, cosine(literal, labelNameEn) | none |
| Structural score | topo component | `space3_score`, type + relation embeddings | none |
| Score combination | `_total_score` | `np.sum(scores_list)` | **weighted** by `w_geo`, `w_name`, `w_topo`; the paper sums unweighted |
| Threshold | `ACCEPTANCE_THRESHOLD = 0.7` | 0.7 | same number, **different meaning** because the combination differs |

### Why the divergences matter

**`addrCity` is absent from the paper's dictionary**, yet it produced 362 of the
488 historical JM links (74 percent). Our relation extension is what generated
most of the output, so our results are not directly comparable to the paper's
published figures.

**The radius prefilter is an addition.** A candidate just outside 39 km at
precision 4 is discarded before scoring, where the paper would score it and let
the cluster normalisation decide. That is a source of false negatives the paper
does not have.

**A weighted sum cannot exceed an unweighted sum**, so our `normalized_score` is
not on the paper's scale and the shared 0.7 threshold gates differently.

---

## [GEOVECTORS] relevance

`[GEOVECTORS]` explains why the semantic side of the bridge is what it is. The
GV-Tags encoding is a plain average of FastText vectors:

```
GV-Tags(o) = 1/(2|o.T|) * sum over <k,v> in o.T of [ ft(k) + ft(v) ]
```

No centring, no L2 normalisation. Summing many word vectors collapses toward the
global mean direction, so cosine similarity between two such vectors is dominated
by the shared component rather than by entity-specific meaning. This is why the
cosine distributions differ per country (tag density varies) and why an absolute
threshold cannot serve all of them.

---

## Adding figures later

If the USLP chapter turns out to be CC-BY, or permission is obtained:

```
  docs/igea/figures/          images, with a LICENCE note recording the
                              source, figure number and licence
  this file                   add a column linking figure number to file
```

Until then, the redrawn diagrams in
`docs/igea/igea_iterative_alignment.md` and
`docs/igea/uslp_spatial_link_prediction.md` are the visual layer, and they carry
the divergence annotations that images would lack.
