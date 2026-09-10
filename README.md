# WorldKG Pipeline and Geo-Spatial Agents

The WorldKG pipeline transforms noisy and unstructured open source data
from OSM and Wikidata into clean, structured knowledge.
The artifacts include vector embeddings, Wikidata-aligned entities, and predicted
spatial links.


### Processing a Country

Before you can query a country, the pipeline needs to process it. The
sidebar walks you through the flow:

1. **Select a country**: Search or pick a country from the sidebar list.
   Each country shows a badge indicating whether it has already been
   preprocessed (green "Preprocessed") or not (grey "Not Preprocessed").

2. **Choose a snapshot year**: The year selector scopes the pipeline run
   to a specific OSM snapshot date. Years that have already been processed
   show a checkmark; selecting one again re-runs the pipeline for that
   snapshot.

3. **Run the pipeline**: Click the pipeline button in the sidebar. The
   progress panel appears below it, showing each step as it runs
   (embedding, enrichment, alignment, link prediction) with a progress
   bar and running stats (entity count, aligned count, spatial link
   count).

4. **Explore the results**: When the pipeline finishes, four tabs
   appear in the sidebar panel:

   | Tab | What it shows |
   |---|---|
   | **Query** | The three query modes (below) for asking questions about the processed country |
   | **Metrics** | Step-by-step breakdown of the pipeline run: status, duration, and summary per step |
   | **USLP** | Predicted spatial links: accepted/rejected counts, acceptance rate, score distribution, and map toggles |
   | **Spatial** | Subgraph coverage: subgraph cards with availability badges and node/way counts |

   Results also appear as overlays on the map. Search hits, query
   results, and link geometries render directly on the Leaflet surface.


### Query Modes

The Query tab supports three modes that can be tested independently:

### 1. Structured (JSON)

Enter OSM tags as JSON (e.g. `{"amenity": "cafe"}`) or a name in any
language (e.g. `{"name": "파리바게뜨"}`).<br>
The romanizer auto-detects the script for cross-script name matching. 
FastText provides semantic type matching.

![Query by OSM Tag](frontend-v3/src/assets/Query_By_OSM_Tag.png)

### 2. Natural language

Type a name in any language or script (e.g. "paris bagueete",
"파리바게뜨", "café", "원탕").<br>
The romanizer handles cross-script matching; FastText adds semantic type
matching.

![Query by Natural Language](frontend-v3/src/assets/Query_By_Natural_Language.png)

### 3. Kuhn's Template

Type a full geospatial question (e.g. "Which bars are within 50m of
Hollywood Blvd?"). The parser classifies it into one of 9 templates with
confidence scores,<br>
and the parsed concepts are displayed for review. This mode uses its own
parser and executor, and can be tested independently.

![Kuhn's Template](frontend-v3/src/assets/Kuhns_Template.png)

<br>

<hr style="border: 2px solid #333;">

<br>

<!-- TODO add MapQA type to the GUI as key to the template number that is shown -->
The parser is trained on the public MapQA
dataset and on self-supervised question–answer pairs generated from our
own OSM data, so the templates learn the phrasings and toponyms of the
countries the pipeline has actually processed. When a user submits a
question through Kuhn's template mode, the result is summarized by an
open-source AI agent (QwenAgent) that grounds its answer in the template
output, the underlying entity data, and deterministic relational context
(predicted spatial links, community structure, class distribution).
Answers stream in progressively over SSE — parsed query → results →
entity context → the summary appears token-by-token — so the query runs
directly from the browser to the backend with no agent in the loop.

![GeoFlow](frontend-v3/src/assets/GeoFlow.png)

*Figure 2: Overview of Spatial-Agent: (A) Spatial information theory
analysis extracts core concepts and assigns functional roles; (B) Concept
transformation drafting composes templates from the library; (C) GeoFlow
Graph construction produces an ordered and constrained graph; (D) Graph
factorization maps to executable tools for execution and answer
generation. (Source: [Spatial-Agent paper](papers/WernerKuhn/Spatial-Agent:%20Agentic%20Geo-spatial%20Reasoning%20with%20Scientific%20Core%20Concepts.pdf))*




### Types of Questions the Templates Handle Well

The templating system succeeds with questions that map to the artifacts
the pipeline produces: entity types (from WorldKG ontology classes),
spatial relationships (from USLP link prediction), semantic similarity
(from GeoVectors embeddings), and cross-script names (from the
romanizer). The table below shows all 10 macro-templates from the
Spatial-Agent paper, the example questions we use, and the coverage
each template has in the parser's training data.

**Ireland: 2025_12_31**

```
┌──────────────────────────────┬──────────────────────────────────────────────────────────────────┬──────────────────────────────────┬──────────┐
│ Question type                │ English                                                          │ Template Type                    │ Coverage │
├──────────────────────────────┼──────────────────────────────────────────────────────────────────┼──────────────────────────────────┼──────────┤
│ Find things near a place     │ "Which cafes are within 2km of a school?"                        │ FILTER-AGGREGATE-MEASURE         │ 21%      │
├──────────────────────────────┼──────────────────────────────────────────────────────────────────┼──────────────────────────────────┼──────────┤
│ Find the nearest X           │ "What is the nearest cafe to Shandon Bells?"                     │ GEOCODE-BATCH-COMPARE            │ 25%      │
├──────────────────────────────┼──────────────────────────────────────────────────────────────────┼──────────────────────────────────┼──────────┤
│ Compare distances            │ "Which is closer to Dublin: Tully Mill or the Cliffs of Moher?"  │ GEOCODE-BATCH-COMPARE            │ 25%      │
├──────────────────────────────┼──────────────────────────────────────────────────────────────────┼──────────────────────────────────┼──────────┤
│ What's around here           │ "What amenities are around Tully Mill?"                          │ PLACE-ATTRIBUTE-QUERY            │ 19%      │
├──────────────────────────────┼──────────────────────────────────────────────────────────────────┼──────────────────────────────────┼──────────┤
│ Direction from a place       │ "What is west of Tullygally Tavern?"                             │ LOCATION-BEARING-CLASSIFY        │ 17%      │
├──────────────────────────────┼──────────────────────────────────────────────────────────────────┼──────────────────────────────────┼──────────┤
│ How far is X from Y          │ "How far is Betelnut Cafe from Dublin?"                          │ OBJECT-FIELD-MEASURE             │ 17%      │
├──────────────────────────────┼──────────────────────────────────────────────────────────────────┼──────────────────────────────────┼──────────┤
│ Optimal visiting order       │ "What's the best order to visit these 5 cafes?"                  │ ROUTE-OPTIMIZE                   │ 0%       │
├──────────────────────────────┼──────────────────────────────────────────────────────────────────┼──────────────────────────────────┼──────────┤
│ Navigation maneuvers         │ "What turns do I take to get to the pub?"                        │ ROUTE-STEP-EXTRACT               │ 0%       │
├──────────────────────────────┼──────────────────────────────────────────────────────────────────┼──────────────────────────────────┼──────────┤
│ Compare routes               │ "Which route to Dublin is faster: M1 or M7?"                     │ MULTI-ROUTE-COMPARE              │ 0%       │
├──────────────────────────────┼──────────────────────────────────────────────────────────────────┼──────────────────────────────────┼──────────┤
│ Multi-leg journey            │ "How long is the bus and train trip to Cork?"                    │ MULTI-SEGMENT-AGGREGATE          │ 0%       │
├──────────────────────────────┼──────────────────────────────────────────────────────────────────┼──────────────────────────────────┼──────────┤
│ Latest departure time        │ "What's the latest I can leave to arrive by 5pm?"                │ TIME-WINDOW-REVERSE              │ 0%       │
└──────────────────────────────┴──────────────────────────────────────────────────────────────────┴──────────────────────────────────┴──────────┘
```


The 5 templates with coverage are trained and operational. The remaining
5 templates from the paper (routing and navigation patterns) have no
training data yet and are future work.


### System Summary

The System Summary modal (accessible from the header) is the operational
dashboard for verifying pipeline prerequisites. It shows the overall
system state across five tabs:

- **Overview**: planet PBF size and availability
- **Embeddings**: GV-Tags / GV-NLE scan status
- **Storage**: database and partition status
- **Paths**: country and subgraph file paths
- **History**: pipeline run history

This is where you confirm that planet initialization completed and all
prerequisites are in place before running a country pipeline.

<!-- TODO: Add screenshot: System summary modal on the overview tab -->


### Predicted Links (USLP)

The USLP tab shows the output of the pipeline's spatial link prediction
stage. A predicted link is a spatial relationship between two OSM
entities that the pipeline has inferred from their geometry, proximity,
and semantic similarity. For example, a cafe that is "near" a train
station, or a school that is "within" a residential area.

Each predicted link is scored and either accepted or rejected based on a
confidence threshold. The USLP tab displays:

- **Summary metrics**: accepted count, rejected count, acceptance rate,
  and total entity count
- **Score distribution**: a histogram showing how links are spread
  across confidence scores
- **Map toggles**: buttons to render accepted links (green) and rejected
  links (red) as geometries on the map, so you can visually inspect what
  the pipeline predicted and where

---

## Open-Source Technology Choices

The platform is built entirely on open-source components. The table below
shows the choices made for this project versus the closed-source or
commercial alternatives that are common in ML and geospatial systems.

```
┌──────────────────────┬──────────────────────────────────────┬──────────────────────────────────────────────┐
│ Component            │ This project (open-source)           │ Common closed-source / commercial alternative│
├──────────────────────┼──────────────────────────────────────┼──────────────────────────────────────────────┤
│ Text embeddings      │ FastText (cc.en.300.bin, 300D)       │ OpenAI text-embedding-3-small/large          │
│                      │ trained on Common Crawl, runs        │ API-only, per-token pricing, data leaves     │
│                      │ locally, no API costs                │ your infrastructure                          │
├──────────────────────┼──────────────────────────────────────┼──────────────────────────────────────────────┤
│ Vector database      │ pgvector (PostgreSQL extension)      │ Pinecone, Weaviate Cloud                     │
│                      │ same DB as entity data, no sync;     │ managed services, vendor lock-in,            │
│                      │ search is exact + USLP re-rank       │ separate infrastructure to maintain          │
│                      │ (HNSW-ready; fused-400D ANN opt-in)  │                                             │
├──────────────────────┼──────────────────────────────────────┼──────────────────────────────────────────────┤
│ LLM / agent          │ Ollama via NVIDIA Container Toolkit  │ OpenAI GPT-4, Anthropic Claude               │
│                      │ Qwen 14B, Goose MCP server           │ API-only, per-call pricing, rate limits,     │
│                      │ in Vite, calls Django REST directly  │ data sent to third-party servers             │
├──────────────────────┼──────────────────────────────────────┼──────────────────────────────────────────────┤
│ Language /           │ Custom romanizer (phonetic script    │ Google Translate API, DeepL                  │
│ romanization         │ detection, Hangul↔Latin, diacritic   │ API-only, per-character pricing,             │
│                      │ stripping, deterministic, no model)  │ non-deterministic output                     │
├──────────────────────┼──────────────────────────────────────┼──────────────────────────────────────────────┤
│ Spatial database     │ PostgreSQL + PostGIS                 │ Oracle Spatial, Esri ArcGIS                  │
│                      │ full spatial SQL, GiST indexes,      │ licensed, expensive, vendor-specific         │
│                      │ partitioning, free                   │ query languages                              │
├──────────────────────┼──────────────────────────────────────┼──────────────────────────────────────────────┤
│ Backend framework    │ Django + Django REST Framework       │ Proprietary / cloud-native services          │
│                      │ Python, mature, large ecosystem      │ vendor lock-in, limited extensibility        │
├──────────────────────┼──────────────────────────────────────┼──────────────────────────────────────────────┤
│ Frontend / map       │ Vue 3 + Leaflet                      │ Google Maps API, Mapbox                      │
│                      │ reactive SPA, open map tiles,        │ per-load pricing, API key requirements,      │
│                      │ no map usage fees                    │ usage limits                                 │
├──────────────────────┼──────────────────────────────────────┼──────────────────────────────────────────────┤
│ OSM data processing  │ osmium-tool                          │ Proprietary geo-processing pipelines         │
│                      │ fast PBF extraction & filtering,     │ licensed, limited customization,             │
│                      │ full control over the pipeline       │ black-box processing                         │
├──────────────────────┼──────────────────────────────────────┼──────────────────────────────────────────────┤
│ Task orchestration   │ Celery + Redis                       │ AWS Step Functions, Google Cloud Tasks       │
│                      │ local, no cloud vendor dependency    │ cloud-locked, per-transition pricing         │
└──────────────────────┴──────────────────────────────────────┴──────────────────────────────────────────────┘
```

The trade-off is operational: open-source components require you to host,
monitor, and scale them yourself. The benefit is full control over data
residency, no per-call or per-token costs, no rate limits, and no vendor
lock-in. All of which matter for a system that processes millions of
OSM entities and serves interactive queries.

---

## Quickstart

```bash
docker compose -f docker-compose.yml -f compose.override.yml up -d
cd frontend-v3 && npm install && npm run dev
```

---

## What's Next?
- Baseline tests: Ovid and Drift Tests
- Add support for more languages
- CityFM Support
- Named-entity anchor resolution (unique business names → OSM coordinates)
