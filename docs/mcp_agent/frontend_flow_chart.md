# Frontend Flow Chart: Component Tree and State Diffusions

> Current architecture after the two-card redesign (ISSUE-46: URL-gated
> leaf surfaces) and the doppelganger intake (ISSUE-48: live seed
> resolution on the map). Covers the component tree, the stores, the
> data-flow pipeline end to end, and the functional roles in the
> diffusions. The pre-redesign version of this doc (Home.vue tabs,
> pipelineStore + WebSocket, PipelineProgressPanelV3, OsmEntitiesPanel,
> SystemSummaryModal, MapLabelsControls) described components that no
> longer exist; it was rewritten 2026-10-17 when the dead code was
> removed.

## 1. Component Tree

Three layers max (view, unit, leaf). Props flow down, events flow up.
Store access via `setup()` in Options API components only (rule 1.3).

```
App.vue (shell)
  |     router-view + QueryConfirmationModal (global HITL modal)
  |
  +-- router (createWebHistory, URL-gated surfaces, ISSUE-46)
  |     /                          -> HomeV2.vue (two cards: Tools | Doppelganger)
  |     /tools/geo-semantic/:cc?   -> GeoSemanticSurface.vue
  |     /agents/doppelganger/:cc?  -> DoppelgangerSurface.vue
  |
  +-- SurfaceShell.vue (shared chrome for BOTH leaf surfaces)
        owns: system-status gate (PlanetInitPanel), country resolution
        (:cc -> country from the map-data payload), SnapshotCalendar,
        the map overlay state, and the sidebar slot
        |
        +-- WorldKGMap.vue (layer 2: the shared map)
        |     props: selectedIds, searchResults, queryGraph,
        |            templateVisualization, seedOverlay
        |     emits: countries-loaded, country-toggled
        |     stores: entityInfoStore (marker click -> info popover),
        |            seedStore (candidate-marker click IS the
        |            confirmation, ISSUE-48), overlayStore (MCP agent
        |            overlays), mapLabelsStore (text labels)
        |     |
        |     +-- EntityInfoPanel.vue (the focused-entity popover)
        |           store: entityInfoStore (read) + seedStore
        |           (Use-as-seed button for candidates, Seed badge for
        |           the confirmed entity, Wikidata link)
        |
        +-- sidebar slot (per surface):
              SnapshotCalendar.vue (date picker; props-first)
              CountryList.vue (picker when :cc is bare)
              CountryRequestPanel.vue (unsupported/unprocessed country)
              |
              +-- ResearchPanel.vue (DOPPELGANGER surface, ISSUE-48)
              |     props: countryName, countryIso, snapshotDate,
              |            subdivisionQid, recipe='doppelganger_report',
              |            setSeedOverlay (slot setter)
              |     stores: seedStore (setup) + useChat (AI SDK v7)
              |     flows: interview chat -> data-seed parts ->
              |             seedStore -> setSeedOverlay -> the map;
              |             finalize (confirmed seed) -> SSE run
              |
              +-- SemanticSearchPanel.vue (GEO-SEMANTIC surface)
                    props: countryName, countryCode, snapshotDate
                    emits: search-results, query-graph,
                           template-visualization (-> slot setters)
                    |
                    +-- SubdivisionSelector.vue (leaf)
```

The slot setters are the panel-to-map data bridge: `SurfaceShell` owns
the map's overlay props and exposes `setSearchResults`,
`setQueryGraph`, `setTemplateVisualization`, and `setSeedOverlay` to
the slot. Data flows panel -> shell state -> map props; gestures flow
store -> panel (the map writes stores directly).

## 2. Stores (Pinia Options stores unless noted)

| Store | State slice | Writers | Readers |
|---|---|---|---|
| `seedStore.js` (ISSUE-48) | The doppelganger seed: `asked`, `confirmed`, `confirmedSource` ('resolution'/'explicit'), `locked`, `candidates`, `suggestions`, `hoveredKey`; getters `hasSeed`, `isMultiHit` | ResearchPanel (from `data-seed` parts), WorldKGMap (candidate-marker click), EntityInfoPanel (Use-as-seed) | ResearchPanel (chip menu, finalize payload), WorldKGMap (seed overlay layers), EntityInfoPanel (button/badge/hover) |
| `entityInfoStore.js` | The focused entity + source ('map'/'table') | WorldKGMap marker clicks, SemanticSearchPanel table hovers | EntityInfoPanel, WorldKGMap (popover position) |
| `overlayStore.js` (setup store) | MCP agent overlays (`renderToolOverlay`) | The MCP bridge (client-functions) | WorldKGMap (`buildAgentOverlayLayers`) |
| `mapLabelsStore.js` | Map-label display settings + label data | MapLabelsPanel | WorldKGMap (`renderMapLabels`) |
| `queryProposalStore.js` | The HITL query proposal (push/approve state) | The MCP bridge | QueryConfirmationModal |

The pipeline store and its WebSocket machinery were removed with the
two-card redesign (2026-10-17): the pipeline panels left the app with
the redesign and nothing in it consumed the run state.

## 3. Data Flow Summary (end to end)

### 3.1 The doppelganger interview + seed flow (ISSUE-48)

```
  Backend                     Frontend state              Presentation
  -------                     --------------              ------------
  POST /nca/research/chat/    useChat messages            chat bubbles
    (AI SDK UI-message-         (parts array)               ([seed] marker
     stream: start ->                                        stripped for
     data-seed? -> text-*                                    display)
     -> finish)
  data-seed part              lastSeedPart watcher ->     seed chip /
    {found, asked, name?,      seedStore.applySeedPart     candidate menu /
      candidates?|             (locked seed never          not-found
      suggestions?}            clobbered)                  suggestions
  seedStore mutation          seedSignature watcher ->    amber candidate
    (content change only;      pushSeedOverlay ->          markers + green
     hover NEVER fires it)     setSeedOverlay ->           confirmed pin
                               WorldKGMap prop ->
                               renderSeedOverlay
  explicit pick (map click /  seedStore.selectCandidate   chip turns green,
    chip / info-box button)    (locks; candidates          info box: Seed
                               retained for switching)     badge; FIRST pick
                               announceExplicitSeed ->     announces once:
                               one "Seed: <name> (kind)"   "Seed: X (kind)"
                               user message                bubble
  POST /nca/research/         finalizedBrief +            brief box
    finalize/ (messages +       briefSource                 ("Seed:" line
    recipe + confirmed seed)    (chatStatus watcher         from the
                                 auto-rebuilds)             confirmed seed)
  GET /nca/research/stream/   questions[], tools[],       Execution trace +
    (plan/question/tool/       summary (summary_delta)     summary block
     summary_delta/done)
  GET /nca/research/          seedExamples (component-    greeting bubble +
    seed-examples/?country     local, single consumer)     input placeholder
    _code=<iso>                                            use a real entity
```

### 3.2 The geo-semantic search flow (/tools)

| Backend API | Component state | Computed | Template |
|---|---|---|---|
| `/nca/semantic-triplet-search/` results[], scores{...} | `SemanticSearchPanel` data: `.results` (raw), `.map(normalizeResult)` at the fetch boundary | `displayResults`: `.slice(0, topKClamped)` | results table, 10 columns |
| (emit) search-results / query-graph / template-visualization | `SurfaceShell` slot setters -> shell state | - | `WorldKGMap` props -> deck layers (`buildSearchResultLayer`, `buildQueryGraphLayers`, `buildTemplateDeckLayers`) |

### 3.3 Shared hydration

| Backend API | Owner | Consumers |
|---|---|---|
| `/api/system/status/` (ready, snapshot dates, processed countries) | `SurfaceShell.checkSystem` (+ the map's own fetch, whichever lands first) | the init gate, SnapshotCalendar, country eligibility |
| `/recipes/regions-map-data/` countries[] | `WorldKGMap.loadCountries` | the map's country layer, `SurfaceShell` country resolution (via `countries-loaded`) |

## 4. Core Concepts (functional roles in the diffusion)

| Concept | Functional role |
|---|---|
| Source State | API responses stored once, never derived. Component `data` for single-consumer server data, stores for shared state. Everything else is computed. |
| Computed Transform | Takes source state, produces derived value. Pure, reactive, never mutates. Lives in component `computed{}` (props-derived) or store getters (store-derived). |
| NormalizeResult | Shape unifier: raw API result (two shapes, `geom:{lat,lon}` vs `lat,lon`) to canonical `{osm_type, osm_id, name, tags, wkg_class, geom, scores, dist}`. Applied at the fetch boundary, before storage. |
| Props-First | Anti-duplication rule: data the parent already holds flows down as props. SnapshotCalendar receives dates as props, never calls the store. |
| Three States | Completeness contract: every data unit renders loading (skeleton), error (message+retry), empty (guidance). |
| Config-Driven | Data-only contract for leaf components. No handlers in config. Clicks emit up. |
| Slot Setters | The panel-to-map bridge (ISSUE-46): `SurfaceShell` owns the map's overlay props and exposes setters to the slot; panels push data, the shell renders it. No component-to-component prop drilling. |
| Single Seed Owner | One concern, one store (ISSUE-48): the seed lives in `seedStore` and nowhere else. The transcript is the log, the store is the state; finalize reads the store, never re-derives from chat text. |
| Store-Driven Confirmation | The map click IS the confirmation: the candidate-marker click writes `seedStore` directly (the `entityInfoStore` pattern). No upward event, no model interpretation of which entity was meant. |
| Lock | An explicit pick sets `locked`: later `data-seed` parts update the candidate display but never clobber the user's choice. Explicit beats automatic. |
| Handshake Announcement | The FIRST explicit pick is announced to the KE once as a `Seed: <name> (kind)` user message (the existing router-handshake format); switches stay silent (the finalize's confirmed seed is authoritative). |
| Content-Signature Watch | Overlay/announcement side effects key on `seedSignature` (asked/confirmed/candidates content), never on the store deeply: hover state must not re-fit the map (the 2026-10-17 click-storm fix). |
| Marker Shape Adapter | The flat seed shape (`{lat, lon}`) adapts to the marker shape (`{geom: {lat, lon}}` + `seed_kind`) at the panel boundary (`pushSeedOverlay`), so the deck layers and the info panel read one shape. |
