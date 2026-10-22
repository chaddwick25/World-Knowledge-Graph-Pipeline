# BETA_RETROSPECTIVE - The Exploration Era (draft)

> Drafted from the BETA_ROADMAP_PLAN §3 spine (planning session
> 2026-10-12, doc opened 2026-10-15). The spine is the record of what
> the project felt like at beta; it expands in M4, it is not rewritten.
> Dated addenda at the end accumulate insights worth keeping between
> now and the M4 close-out.

## Chapter 1: The machine as the design brief

One workstation, bought at the right time (128 GB RAM before the price
climb, RTX 4070 Ti SUPER 16 GB). Every architectural choice was
constraint-driven: a 70 GB planet PBF streams through a SAX-style
handler on a bounded worker; the spectral solver's routing is written in
VRAM terms (~2240 bytes/node, the 5M-node `SUBDIVISION_NODE_THRESHOLD`,
`_check_gpu_vram` CPU fallback instead of OOM); the same GPU is shared
between pipeline compute and the local LLM server, which is why the
worker-restart logic exists (`empty_cache()` cannot reclaim the CUDA
context). The RAM is what makes the in-memory structures honest:
BallTree over a country's entities, LOBPCG eigenbases, DeepWalk corpora.
Planet-scale compute on the machine you have, not the cluster the
papers assume.

## Chapter 2: Apps first, canvas second

Each paper got its own Django app to learn it in isolation:
`geovectors_encoder` (GeoVectors), `igea` (entity alignment),
`worldkg_nca` (WorldKG), `semantic_search` (MapQA/Kuhn/spectral),
`osmsnapshot` (snapshot identity). Once understood, Celery canvas became
the control plane (chain + chord, `@pipeline_step` separating
logging/WS/run-tracking from the services' data plane). That separation
is what made the September consolidation safe: `extraction` +
`orchestration` collapsed into `core`, `analysis` into `osmsnapshot`,
planet-init folded into one `init_planet` command. Scaffolding was
removed only after the layer beneath was understood.

## Chapter 3: The papers as oracle

The wall: clean, embedded, link-predicted data that was too general to
interpret. The instinct for generality was geometric algebra. The
trail: geospatial agents (Spatial-Agent), then its Kuhn citations, then
Core Concepts of Spatial Information. The aha: interpretation is a
vocabulary, not a technique. Kuhn's five concepts (Location,
Neighborhood, Field, Object, Network) became literal slot types in the
parser. Then the MapQA luck: its dataset is OSM, so question templates
map directly onto the data already pipelined. Method: when a
generality appears, find the paper that already solved the general
case and preserve it natively. `docs/research_to_runtime/` exists
so the mapping stays auditable. Every architectural decision has a
citation.

## Chapter 4: Consolidation and exposure

Going public (Tailscale Funnel, nginx) forced the hardening pass:
per-surface allowlists, admin local-only, login guard removed, the
spoofed-Host trust flip caught by the 3B review-model bake-off and
fixed. Meanwhile the control plane kept simplifying: three
generations of orchestration (Celery canvas, `init_planet`, LangGraph
per DEC-006) in three months, each earned by understanding the layer
below.

## Chapter 5: The doppelganger as proof-of-signal

The closing chapter: `similarEntities` k-NN + `DOPPELGANGER_REPORT`,
inductive by construction (queries the embedding space directly, no
per-country training), carried by commercial POIs (restaurants are
the densest consistent coverage OSM has across countries). It
composes the whole stack: FastText semantics, spatial embeddings,
distance spread, agent grounding. The first feature that travels.
Shipped in M1.

## Lesson carried forward: demos in MVP slices

The WorldKG intro video ran too long and the demo wandered across
features, so the signal was diluted. The correction shapes every
milestone's shipping surface: each beta exposes one public URL that
demonstrates one feature. No composite demos, no feature tours. The
M1 URL demos the doppelganger flow alone.

## Closing line (to verify at M4)

The exploration era ended the day the doppelganger worked. Everything
after is platform work.

---

## Addendum 2026-10-15: the platform found its grammar

Insights worth keeping, captured between the spine and the M4
close-out. These came out of one session covering indexes, the MCP
sidecar, GCP enrichment options, and the frontend redesign.

### Enrichment attaches to existing nodes; new entities are nodes

The factor tables turned out to be the enrichment plane all along:
flat, per-entity, snapshot-keyed, batch-written, runtime-joined on
`osm_id`. Any external dataset that attaches attributes to existing
entities (Overture places, a Places-API pilot, vision/NLP tags) is a
new table in that shape, not a schema change, not a graph change. The
graph is derived from `gv_nle_embedding` + geometry, so enrichment is
always read-side and write-beside.

The boundary is equally clear in the other direction: a dataset that
adds entities (Statistics Canada buildings, NAR addresses) is
structural. New rows are new nodes; the k-NN rebuilds, eigenbases
shift, factor tables recompute, and a foreign ID namespace has to be
answered (the slug gate). Two paths exist (OSM import, or a second
overlay source) and the choice is a milestone, not an issue. This
distinction now sorts the whole data-augmentation roadmap: enrichment
issues can ship in any beta, entity-source work gets its own.

### The UI is a router over two surfaces

Reducing the frontend exposed the information architecture that was
buried under tabs: **Tools** are the query primitives (geo-semantic
today, temporal in beta.2), **Agents** are the composed workflows
(doppelganger today, a temporal agent once M2/M3 lands). Home is two
columns, not a feature grid.

Two refinements of the MVP-slices lesson:

- **URL-gated surfaces.** The tab bar became vue-router paths
  (`/tools`, `/agents`, country-scoped leaf routes). One public path
  demos one feature; a demo video is a walk through URLs, and a shared
  link reproduces the exact state. The gate is the video script and
  the feature boundary at once.
- **Disabled cards communicate the roadmap better than hidden
  features.** Temporal shows up on both section indexes, greyed, with
  the beta.2 note. The page tells the "frontend grows with the
  roadmap" story by itself.

Metrics and ops (system summary, pipeline progress beyond the surface
the user is on) move out of the feature app into the georecord
admin app wrapped around Django admin. Separation of concerns:
the feature app demos, the admin app operates.

### The task loop got a shape

This session also established how work moves now: reasoning in
session, an `ISSUE-<slug>.md` draft as the reviewable spec, the
maintainer runs `glab issue create`, branch `<iid>-<slug>`, agent
implements and emits the handoff block, maintainer merges. The next
step is agents that run this loop themselves (cartographer
profile, write-issue skill, a test-runner later). The workflow is
what made the October consolidation survivable at speed; writing it
down is what makes it repeatable.

---

## Addendum 2026-10-17: consolidation shadows

Insights from the pre-end-to-end hardening session (data-dir
reorganization research, init reliability). Where the 10-15 addendum
recorded the platform finding its grammar, this one records what
running the grammar exposed.

### Scaffolding survives in both directions

Chapter 2's rule (scaffolding removed only after the layer beneath was
understood) has a second half the beta era earned: scaffolding also
survives after the layer beneath is removed. `ASSET_BUNDLES_DIR`
outlived the AssetBundle model; `pbf_cache` was built and wired into
the extraction path's read side but never populated (its only writer
had zero callers); `validate_pickle_generation` gated every country on
a placeholder that always returned False. None of it failed loudly:
every phantom path degraded to a silent fallback (empty dict, skipped
country, original source), so the system ran green over dead code.
The lesson sharpens: understanding earns the right to delete, but only
loud absence tells you what to delete. The init-reliability ticket is
the systemic correction: FAILED status plus per-step state, so a
partial init is visible instead of sealed behind a COMPLETED lock.

### The oracle changes hands

Three generations of orchestration (Chapter 4) left debris the fourth
inherited: path derivations written against different `BASE_DIR` eras
(the phantom `data/data` nesting, a CWD-relative writer, a reader
resolving against the wrong root), and three storage roots kept
implicit across `.env` + compose + settings (the `osm_polygon_file`
mount typo duplicated an entire poly tree before anyone looked).
During the exploration era the papers were the oracle: when a
generality appeared, find the paper that already solved the general
case. Platform work inverts this: the oracle is the system's own
incident history, read the same way - trace the symptom to the
assumption, cite the era that wrote it. The hardening session that
produced a collision map and two sequenced tickets is the 10-15 task
loop applied to archaeology instead of features.

### The closing line, tested

"The exploration era ended the day the doppelganger worked. Everything
after is platform work." It held, with one refinement this session
suggests: platform work should not feel like exploration, and this
session did - digging through one's own debris, surprised by what
turned up. The refinement: what changed is where the unknowns live.
They moved out of the papers and into the system's own history; the
retrospective is how they get read.

---

## The owner's voice: vibe coding the WorldKG app (2026-10-18)

The spine above is the record of what the project felt like at beta.
This section is the owner's first-person telling, drafted 2026-10-18
in the what-went-wrong voice the spine lacks. The spine's closing line
speaks of the "exploration era"; the owner's telling frames the same
moment as the "vibe-coded era". Both stand; M4 reconciles them.

Cost is always a factor when building any machine learning system or
pipeline, and that includes time and money. When I attempted to create
an open-source ML app, I paid both costs and learned even more
expensive lessons along the way. This is a retrospective on vibe coding
the WorldKG app.

### The cost of entry

Solving real-world problems with AI requires hardware, and hardware is
expensive. I learned this in the initial stages of the project: when I
first tested the pipeline, it was clear that I needed RAM, space, and
power. After securing 128GB of RAM, a 1200 watt power supply, and a
2TB SSD, my system finally had the power to process the massive OSM
planetary files. Managing resources effectively across long jobs and
containers is the bread and butter of any homelab/pipeline setup, and
mine was no different.

### Apps first, orchestration second

Processing the planet files needed the right tool as much as the right
hardware. Osmium is a battle-tested C++ tool with Python bindings, and
its streaming API is what made a 70GB planet file processable on
consumer hardware. I created a Django app for each paper in the WorldKG
that represented a stage in the pipeline. I did this for my own
understanding, with custom orchestration to mimic the learnings from
the paper. This leads to my next harsh lesson: get iterative feedback
and have an outlet to share ideas. Looking back, this was the best time
to get feedback, to avoid the AI slop trap of perceived progress. I
strayed from the fundamentals, including communication, because of the
intoxicating power of perceived progress when using AI coding agents.
The knowledge gained from reading the papers and understanding the
math was great for improving my understanding, but it created paralysis
by analysis when trying to decide what to implement. In the future I
would fix this by forcing myself to create an MVP focused on solving a
problem for the audience, starting with UX design first before doing
any research or writing a single line of code.

Celery Canvas was the perfect orchestration tool for the pipeline at
the time when orchestration was the missing piece. It worked with
Python 3.8, was battle-tested, and was so abstract that it aligned with
everything available to me: I could use the Postgres DB, and I could
even have Postgres do the messaging, though I chose the Redis broker
that's implemented now. Because the pipeline is a chain of
transformations, a general orchestration layer that doesn't care what
any single stage does made a lot of sense.

### The wall: data too general to interpret

I crossed another milestone by finishing the pipeline, but the data was
not interpretable because it was too general. The way to interpret the
data turned out to be a vocabulary, not a technique: Kuhn's core
concepts with their functional roles pretty much unify the geosemantic
question space, and when you align that with MapQA, the first five
question templates become foundational. That alignment is what made the
data speak.

I trained a parser on those templates using the MapQA paper's dataset
and called the feature "Structured Questions". It runs as an MCP tool
and as an endpoint in the web app. The training details, including the
supervised-learning trick I borrowed from Aurélien Géron's book, live
in the MapQA blog post.

### Scale breaks

I found the limit of my two GPUs when I started running larger
countries like Australia. The data could not fit on the 16GB card, and
I could not split the calculations across both GPUs. The fix was batch
compute, and the approach came straight from the Maps papers I had
already read.

### Consolidation

The orchestration kept simplifying after that. The September
consolidation collapsed the apps into a unified core and folded planet
initialization into a single init command run by the Docker entrypoint.
Things got a lot cleaner. I started to feel like the project was
finally coming together.

### The doppelganger

So I re-designed the demo around a user-focused product. By making the
product the starting point instead of the pipeline, I built something
that uses the best of both worlds: the Doppelganger Agent and toolset.
I chose the word "doppelganger" because it aligns with recommender
systems. The data is interpretable through geo-semantic querying, and
the OSM dataset has a lot of commercial data, so the doppelganger
projected the right signal without me having to explain much to the
user. It aligns with how the pipeline runs at the country level, which
makes it inductive and means it will work for all countries.

### What's left

The vibe-coded era proved itself the day the doppelganger worked. The
pipeline is stable. The agent built it. Now I finish it by hand:
cleanup, fine touches, and the rest of Kuhn's question types. Each
tool gives the agent new capabilities. I add the tool, then upgrade
the agent to handle it. Better separation of concerns, and a pipeline
that keeps composing.
