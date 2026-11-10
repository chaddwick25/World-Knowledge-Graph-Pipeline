# IGEA: iterative geographic entity alignment

How two identity systems that share no key get reconciled, and what that
reconciliation costs.

**Reference:** Dsouza, Yu, Windoffer, Demidova, *Iterative Geographic Entity
Alignment with Cross-Attention* (2023).
**Implementation:** `backend/igea/services/iterative_alignment_service.py`,
driven by `backend/pipeline/tasks/country_pipeline_steps/step_3_run_igea.py`.
**Flow companion:** `docs/igea/PAPER_FIGURE_MAP.md` maps each stage to the
paper figure it implements.

---

## 1. The two primitives

The whole pipeline stands on two data sources that describe the world in
completely different ways.

```
  OSM                                    Wikidata
  the geographic primitive               the semantic primitive
  -----------------------------------    -----------------------------------
  identity   osm_type : osm_id           identity   Q-number
  carries    geometry, tags, names       carries    labels, classes, claims
  answers    WHERE, and WHAT SHAPE       answers    WHAT IT IS
  edited by  mappers, incrementally      edited by  a curated community
  scale      1,113,366 entities (JM)     1,761 items in the JM bbox
```

**They share no key.** OSM does not know what a Q-number is; Wikidata does not
know what an osm_id is. The only thing connecting them is a `wikidata=Q123` tag
that a mapper typed onto an OSM object.

```
        +-----------------------------+
        |  OSM entity                 |
        |  osm_type:osm_id            |
        |  geom, tags, name           |
        +-----------------------------+
                    |
                    |  wikidata = Q123        <- the entire bridge
                    |
        +-----------------------------+
        |  Wikidata item              |
        |  Q123                       |
        |  label, class, claims       |
        +-----------------------------+
```

## 2. The bridge, and how little of it exists

Measured on JM at snapshot `2025_12_31`:

```
  OSM entities                                   1,113,366
  carrying a wikidata=* tag                            738    (0.07%)
  Wikidata items harvested for the same bbox         1,761

  the bridge covers roughly one OSM entity in 1,500
```

That number is the central constraint of the pipeline. Every stage either
**uses** the bridge, **extends** it, or **ignores** it, and the sparsity is why
extending it matters.

```
  STAGE            ROLE WITH RESPECT TO THE BRIDGE
  ---------------  ---------------------------------------------
  Step 1 ingest    populate the bridge from source tags
  Step 2/3 harvest load the far shore (Wikidata items in the bbox)
  Step 3 IGEA      EXTEND the bridge for unlinked entities
  Step 4 USLP      does not touch the bridge at all
  Step 5c spectral works on the OSM side only
```

## 3. Two reconciliations, not one

The most useful distinction in this subsystem: there are two different kinds of
reference in OSM data, and they need two different resolutions.

```
  LITERAL reference (unresolved)        RESOLVED reference
  ------------------------------        ------------------------------
  addr:city = "Kingston"                wikidata = Q123
  points at a NAME                      points at an IDENTITY
  ambiguous, many Kingstons             unambiguous
  resolved within OSM                   resolved across systems
  by USLP                               by IGEA
```

Which gives two reconciliation problems that only look similar:

```
  +---------------------------------------------------------------+
  |  RECONCILIATION 1:  OSM entity  <->  Wikidata item            |
  |                                                               |
  |  bridge      wikidata=* tags, sparse                          |
  |  extended by IGEA                                             |
  |  needs       seeds, candidates, a score scale                 |
  |  failure     produces nothing if any input is empty           |
  +---------------------------------------------------------------+

  +---------------------------------------------------------------+
  |  RECONCILIATION 2:  OSM entity  <->  OSM entity via a literal |
  |                                                               |
  |  bridge      addr:* and is_in tags, dense                     |
  |  resolved by USLP                                             |
  |  needs       OSM geometry and names only, NO Wikidata         |
  |  failure     produces nothing if the candidate pool is empty  |
  +---------------------------------------------------------------+
```

**This is why USLP produced 488 links with zero IGEA seeds while IGEA produced
zero.** They solve different problems with different inputs. Treating them as one
system, or diagnosing one with the other's symptoms, is the mistake that makes
both look broken at once.

## 4. Seeds versus candidates

IGEA consumes two inputs that are easy to confuse and impossible to substitute.

```
                SEEDS                            CANDIDATES
       supervision: what is already known   search space: what might match
       the known bridge                     the far shore

    +--------------------------+          +--------------------------+
    | OSM snapshot PBF         |          | Wikidata SPARQL endpoint |
    | wikidata=Q123 tags       |          | bbox query, one per tile |
    +--------------------------+          +--------------------------+
                |                                      |
                | Step 1 ingest                        | Step 2/3 harvest
                | vector_storage_service               | wikidata_service
                v                                      v
    +--------------------------+          +--------------------------+
    | OsmEntity.wikidata_uri   |          | PrecomputedLinkCandidate |
    | one row per linked OSM   |          | one row per Wikidata     |
    | entity                   |          | item in the bbox         |
    +--------------------------+          +--------------------------+
                |                                      |
                | _get_seed_alignment                  | _candidate_pairs
                |                                      |
                v                                      v
    +--------------------------+          +--------------------------+
    | seed_map {osm_id -> QID} |          | candidate pairs          |
    | reported: final_seed_size|          | reported: blocking: N    |
    +--------------------------+          +--------------------------+
                |                                      |
                +------------------+-------------------+
                                   |
                                   v
                       +-----------------------+
                       | IGEA scoring          |
                       | score_pair(head,cand) |
                       | gate(score) -> accept |
                       +-----------------------+
```

Their failures have different signatures, which is the practical reason to keep
them apart:

```
  SEED failure looks like                CANDIDATE failure looks like
  -----------------------                ----------------------------
  final_seed_size absurdly low           blocking: N pairs small or zero
  "only 4 seed pairs (< 100)"            "SPARQL request timed out at
  scoring runs without supervision         offset=14000"
  the model never trains                 the pool is partial
```

## 5. The flow, as contracts

Each junction states what the implementation assumes. Reading it this way, a
defect is a junction where an assumption silently defaulted rather than raised.

```
  INPUT                STAGE                OUTPUT
  -----                -----                ------

  OSM PBF tags   -->   Step 1 ingest   -->  OsmEntity rows
  + geometry           vector_storage        tags, geom, wkg_class,
                                              embedding, wikidata_uri
                       CONTRACT: every source tag is preserved on the row

  Wikidata bbox  -->   Step 2/3 harvest -->  PrecomputedLinkCandidate
                       wikidata_service      wikidata_uri, label, class
                       CONTRACT: every item in the bbox is returned and
                                 pagination completes

  OsmEntity      -->   Step 3 IGEA      -->  accepted links
                       iterative_            wikidata_uri written,
                       alignment_service     rejected rows recorded
                       CONTRACT: seeds are the already-linked entities;
                                 the working set is the whole country
                                 partition, not a tag-defined subset;
                                 the gate matches the scorer's scale
```

### Why each contract exists

**Tags preserved on the row.** The OSM primitive carries the bridge. If ingest
drops `wikidata=*`, the bridge is empty and IGEA has no supervision at all. The
column existed and was read; nothing wrote it.

**Bbox completeness.** The candidates are the far shore. A truncated harvest
shrinks the search space silently, so IGEA scores against a partial pool and
reports success.

**Working set is the partition.** Country membership is already encoded by the
`country_code` partition key. A tag cannot add that information, only subtract
entities from it. Filtering on `addr:country` selected 1.4% of the available OSM
primitive and starved the reconciliation.

**Gate matches the scorer.** The paper's `tha = 0.6` is a cross-attention
probability. Cosine similarity is a different scale entirely, and the cosine
distribution also differs per country, so no single absolute value serves all of
them.

## 6. Where the contracts live

```
  SUBSYSTEM         FILE                                     CONTRACT
  ---------         ----                                     --------

  ingest            geovectors_encoder/services/             tag preservation
                    vector_storage_service.py

  harvest           worldkg_nca/services/                    bbox completeness
                    wikidata_service.py

  alignment         igea/services/                          seed and working-set
                    iterative_alignment_service.py           selection, gating

  link prediction   igea/services/                          pool availability
                    spatial_link_prediction.py

  canvas            pipeline/canvas.py                      stage ordering
```

The last one is worth noting because it is where the two ordering defects lived:
the canvas is built from state that a later stage generates, so a first run on
fresh input sees an empty world. See section 9.

## 7. The scoring path

Two scorers, two scales, and a gate that must follow whichever is active.

```
                      _score_pair(head, candidate)
                                  |
                   cross_attention_model is not None?
                     +------------+------------+
                    yes                        no
                     |                          |
                     v                          v
             sigmoid(logits)              cosine(a, b)
             PROBABILITY                  SIMILARITY
             range [0, 1]                 range [-1, 1]
             calibrated by training       raw geometry, uncalibrated
                     |                          |
             gate: absolute 0.6          gate: relative to the
             (IGEA paper Fig. 5)         country's own distribution
```

The relative gate:

```
  if spread < COSINE_MIN_SPREAD:      accept nothing
                                      the metric carries no signal
  else:                               gate = max(COSINE_FLOOR,
                                                  mean + COSINE_K_SIGMA * std)
```

Measured distributions that motivated it:

```
  country   min      max     mean    spread    what the gate did
  -------   -----    -----   -----   ------    -----------------
  IE       -0.084   0.554   0.233   0.638     accepted the upper tail
  BZ        0.000   0.429   0.187   0.429     accepted its own tail
  NI        0.500   0.502   0.501   0.002     accepted nothing, correctly
```

A fixed 0.45 accepted IE's tail and rejected all of BZ. A fixed 0.6 rejected
everything, because it is a probability threshold applied to a similarity score.

## 8. Measured behaviour

JM at `2025_12_31`, after the flow above was made to hold end to end:

```
  processing 31,048 entities      (was 96)
  seed alignment = 417            (was 4)

  iter 1   spread -0.200..0.483   gate 0.257   ->  901 accepted
  iter 2   spread -0.200..0.396   gate 0.250   ->  315 accepted
  iter 3   spread -0.200..0.422   gate 0.250   ->  114 accepted
                                          total  1,330
```

Seeds come from source, written in the same COPY as the embedding:

```
  seeds 2,054   distinct QIDs 988   802 one-to-one (81%)
  node 1,751    way 246             relation 57
```

## 9. Open items

**Precision is unvalidated.** 2.13 OSM entities per Wikidata target, with a tail
reaching 83. The one-to-one majority is clean; the tail is not. This is what the
paper's Fig. 5 threshold tuning measures, and it can now be run because real
seeds exist.

**Cross-attention never trains.** Seeds are excluded from candidates because they
are already linked, so training has no positive examples by construction. The
`MIN_SEEDS_FOR_MODEL` guard is therefore unreachable in practice: the "no positive
examples" guard always fires first and falls back to cosine.

**Ordering defects are a separate class.** Both the subgraph chord and the USLP
pool consume state a later stage produces, so a first run on fresh input sees an
empty world. Documented in `AGENTS.md` under pipeline state-ordering defects.

---

## Appendix: parameter reference

| Parameter | Default | Meaning |
|---|---|---|
| `max_iterations` | 3 | Bootstrapping rounds |
| `threshold` | 0.6 | Gate for the cross-attention **probability** path |
| `COSINE_FLOOR` | 0.25 | Absolute minimum for the cosine path |
| `COSINE_K_SIGMA` | 1.5 | Cosine gate is `mean + k * std` |
| `COSINE_MIN_SPREAD` | 0.05 | Below this the metric cannot discriminate |
| `MIN_SEEDS_FOR_MODEL` | 100 | Below this, skip training and use cosine |
| `MAX_DISTANCE_M` | 2500.0 | Max Haversine distance for candidate pairs |
| `enable_nca` | True | Run NCA class alignment inside the loop |
| `enable_cross_attention_training` | True | Train the BiLSTM per country |

IGEA does not read `hyperparams.yaml`; defaults are hardcoded. USLP is a separate
subsystem and is not gated on IGEA output.
