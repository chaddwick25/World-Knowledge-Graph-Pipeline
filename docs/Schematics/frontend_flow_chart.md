# Frontend Flow Chart: Component Tree and State Diffusions

> New design for the homepage sidebar refactor. Covers the component tree,
> state transformations (diffusions), and the data-flow pipeline end to end.
> Driven by `TICKET_SNAPSHOT_CALENDAR_AND_STATE`,
> `TICKET_UI_QUERY_MODE_RADIO_BUTTONS`, `TICKET_SEARCH_RESULTS_SCORE_COLUMNS`,
> and the steps-list visibility fix.

## 1. Component Tree

Three layers max (view, unit, leaf). Props flow down, events flow up. Store
access via `setup()` in Options API components only.

```
Home.vue (layer 1: view)
  |
  +-- WorldKGMap.vue (layer 2)
  |     props: selectedIds, searchResults, queryGraph, augmentedLinks, ...
  |     emits: countries-loaded, country-toggled
  |     store: none
  |
  +-- PlanetInitPanel.vue (layer 2)
  |     props: suggestedPlanetFilePath, systemError, checking
  |     emits: refresh
  |     store: none
  |
  +-- PipelineProgressPanelV3.vue (layer 2)          [steps-list gate changed]
  |     props: countryName, sessionId
  |     emits: pipeline-done
  |     store: usePipelineStore() via setup()
  |
  +-- SnapshotCalendar.vue (layer 2)                [new]
  |     props: snapshotDates, completedDates, usedDates, selectedDate, disabled, jobs
  |     emits: select-date, reset
  |     store: none (props-first)
  |     |
  |     +-- JobButton.vue (layer 3: leaf)            [new]
  |           props: date, status, entities, aligned, isActive
  |           emits: click
  |           store: none
  |
  +-- Tabbed Panel (rendered inline, not a component)
  |     |
  |     +-- SemanticSearchPanel.vue (layer 2)        [radio + score columns changed]
  |     |     props: countryName, snapshotDate
  |     |     emits: search-results, query-graph
  |     |     store: none (axios direct)
  |     |     |
  |     |     +-- SubdivisionSelector.vue (layer 3: leaf)
  |     |           props: countryName
  |     |           emits: subdivision-selected
  |     |           store: none (axios direct)
  |     |
  |     +-- OsmEntitiesPanel.vue (layer 2, merged 2026-09-28)
  |           sub-mode toggle: USLP (AugmentedDataPanel) | OSM Entity (MapLabelsPanel)
  |
  +-- SystemSummaryModal.vue (layer 1, unchanged)
        props: open
        emits: close
        store: none
```

### Layer 1: Home.vue (view)

Owns the source state for the page. Mounts `checkSystem()` to hydrate
readiness and snapshot dates in one request.

**Source state (data):**

| Field | Type | Source |
|---|---|---|
| `selectedCountryIds` | `[]` to `[id]` | user selection |
| `selectedSnapshotDate` | `"2025_12_31"` | calendar or default |
| `snapshotDates` | `["2025_12_31", ...]` | `/system/status/` |
| `isSystemReady` | `null\|true\|false` | `/system/status/` |
| `isPipelineRunning` | `bool` | pipeline lifecycle |
| `pipelineSessionId` | `str\|null` | `store.startPipeline()` |
| `pipelineDoneStatus` | `'completed'\|'failed'\|...` | `onPipelineDone()` |
| `countryStatus` | `{is_processed, ...}` | `/country-search-status/` |
| `activeTab` | `'query'\|'osm-entities'\|'research'` | user selection |
| `searchResults` | `[...]` | `SemanticSearchPanel` emit |
| `augmentedLinks` | `{...}\|null` | `OsmEntitiesPanel` emit (USLP sub-mode) |
| `queryGraph` | `{...}\|null` | `SemanticSearchPanel` emit |

**Store access:** `usePipelineStore()` via `setup()`.
- Reads: `store.runs`, `store.snapshotJobs`, `store.usedSnapshotDates` (renamed from `runsByYear`)
- Calls: `store.fetchSnapshotJobs()`, `store.fetchSnapshotJobResults()`, `store.startPipeline()`

**Watchers:**
1. `selectedCountryIds` (deep) to `fetchCountryStatus()` + `store.fetchSnapshotJobs()` + `maybeFetchDurableResults()`
2. `selectedSnapshotDate` to `maybeFetchDurableResults()`

**Computed transforms:**

| Computed | Derivation |
|---|---|
| `singleCountry` | `selectedCountries.length === 1 ? selectedCountries[0] : null` |
| `hasSelection` | `selectedCountryIds.length > 0` |
| `filteredAvailable` | `allCountries` filtered by `is_geovectors_supported` + search query |
| `canSearch` | `(countryStatus?.is_processed \|\| pipelineDoneStatus === 'completed') && singleCountry` |
| `selectedYear` | `selectedSnapshotDate.split('_')[0]` (removed in calendar rewrite) |
| `yearDisabled` | `isPipelineRunning` (removed in calendar rewrite) |

**Method transforms:**

| Method | Transform |
|---|---|
| `applySnapshotDates(dates, defaultDate)` | raw `dates[]` to `snapshotDates[]` + set `selectedSnapshotDate` default. `snapshotYears` derivation removed. |
| `maybeFetchDurableResults()` | if `usedSnapshotDates.has(date)`, call `store.fetchSnapshotJobResults()`, set `pipelineDoneStatus = 'completed'` |
| `handleRunPipeline()` | call `store.startPipeline(name, date, opts)`, set `pipelineSessionId` |
| `onPipelineDone(event)` | call `fetchCountryStatus()` + `store.fetchSnapshotJobs()` to refresh |

### Layer 2: WorldKGMap.vue (unchanged)

Receives all data via props. No store access.

| Prop | Type |
|---|---|
| `selectedIds` | `[id]` |
| `searchResults` | `[...]` |
| `queryGraph` | `{...}\|null` |
| `augmentedLinks` | `{...}\|null` |
| `showAcceptedLinks` | `bool` |
| `showRejectedLinks` | `bool` |
| `visibleRelations` | `{...}\|null` |

**Emits:** `countries-loaded`, `country-toggled`

**Computed:** map markers from `searchResults.filter(.geom).map(toMarker)`

**Renders:** Leaflet map + GeoJSON overlays.

### Layer 2: PlanetInitPanel.vue (unchanged)

| Prop | Type |
|---|---|
| `suggestedPlanetFilePath` | `str` |
| `systemError` | `str` |
| `checking` | `bool` |

**Emits:** `refresh`

### Layer 2: PipelineProgressPanelV3.vue (steps-list gate changed)

| Prop | Type |
|---|---|
| `countryName` | `str` (required) |
| `sessionId` | `str\|null` |

**Emits:** `pipeline-done`

**Store access:** `usePipelineStore()` via `setup()`.
- Reads: `store.runs[countryName]`
- Calls: `store.startPipeline()`, `store.connectWebSocket()`

**Computed (internal):**

| Computed | Derivation |
|---|---|
| `run` | `store.runs[countryName] \|\| null` |
| `status` | `run?.status \|\| 'idle'` |
| `steps` | `run?.steps \|\| []` |
| `completedCount` | `store.completedCount(run)` |
| `totalCount` | `steps.length` |
| `progressPct` | `store.progressPct(run)` |
| `errorMessage` | `run?.error \|\| null` |
| `logs` | `run?.logs \|\| []` |
| `isDurable` | `run?.durable === true` |
| `summary` | `run?.summary \|\| null` |
| `snapshotDate` | `run?.snapshotDate \|\| null` |
| `isRunning` | `status === 'running' \|\| status === 'in_progress'` |
| `isComplete` | `status === 'completed'` |
| `isFailed` | `status === 'failed'` |
| `isSkipped` | `status === 'skipped'` |
| `isIdle` | `status === 'idle'` |
| `badgeLabel` | Running / Complete / Failed / Skipped / Idle |
| `badgeVariant` | info / success / danger / warning / secondary |
| `currentStepName` | `steps.find(s => s.status === 'in_progress')?.name` |
| `pipelineTitle` | `'Preprocessing'` if temporal, else `'WorldKG Pipeline'` |

**Steps-list gate (changed):**

```
v-if="steps.length > 0 && !(isDurable && isComplete)"
```

Hides the steps list for durable completed runs. The summary block
(entities, aligned, spatial links) replaces it as the "result present" view.
Visible for running, failed, and durable-failed runs.

```
                    run.status
                        |
          +-------------+-------------+
          |             |             |
      running      completed      failed
          |             |             |
          |        isDurable?         |
          |        /         \        |
          |      yes          no     |
          |       |           |      |
          |   HIDE steps   SHOW steps |
          |   SHOW summary  SHOW steps|
          |       |           |      |
          +-------+-----------+------+
                  |
            template renders:
              running  -> steps list (open) + progress bar
              failed   -> steps list (open) + error
              durable  -> summary block (entities/aligned/links)
              + done
              live     -> steps list (open) + progress bar
              + done
```

### Layer 2: SnapshotCalendar.vue (new)

Replaces the year-button selector (Home.vue lines 152 to 182). Props-first,
no store access (rule 01-frontend-patterns, section 1.4b).

| Prop | Type | Source |
|---|---|---|
| `snapshotDates` | `["2025_12_31", ...]` | `/system/status/` |
| `completedDates` | `["2025_12_31", ...]` | `/system/status/` (global) |
| `usedDates` | `Set<"YYYY_MM_DD">` | `store.usedSnapshotDates` |
| `selectedDate` | `"2025_12_31"` | Home.vue data |
| `disabled` | `bool` | `isPipelineRunning` |
| `jobs` | `[{snapshot_date, status, ...}]` | `store.snapshotJobs` |

**Emits:** `select-date` (date: `"YYYY_MM_DD"`), `reset` ()

**Store:** none (props-first).

**Computed transforms:**

| Computed | Derivation |
|---|---|
| `availableDates` | union of `snapshotDates`, `completedDates`, `jobDates` from `jobs`, as a Set, sorted descending |
| `isDateUsed(date)` | `usedDates.has(date)` |
| `isDateActive(date)` | `date === selectedDate` |
| `isDateDisabled(date)` | `disabled \|\| !availableDates.has(date)` |
| `jobButtons` | `jobs.filter(completed\|failed).toSorted(desc by date).map(to button config)` |
| `hasJobs` | `jobButtons.length > 0` |
| `canReset` | `usedDates.size > 0 && !disabled` |

`jobButtons` transform (immutable chain):

```js
jobs
  .filter(j => j.status === 'completed' || j.status === 'failed')
  .toSorted((a, b) => b.snapshot_date.localeCompare(a.snapshot_date))
  .map(j => ({
    date: j.snapshot_date,
    status: j.status,
    entities: j.total_entities,
    aligned: j.total_aligned,
    isActive: j.snapshot_date === selectedDate,
  }))
```

**Three states (rule 01-frontend-patterns, section 1.10c):**
- Loading: skeleton calendar while parent fetches `snapshotJobs`
- Error: "Failed to load snapshot dates" + retry
- Empty: "No processed jobs yet. Run the pipeline to see results."

**Renders:** `<input type="date">` (native, themed via CSS custom properties) or Bootstrap date picker, plus the processed-jobs button list (config-driven, rows = `jobButtons`), plus Reset button.

### Layer 3: JobButton.vue (new, leaf)

Dumb component: props in, event out, no store, no fetching (rule 01-frontend-patterns, section 1.6).

| Prop | Type |
|---|---|
| `date` | `"2025_12_31"` |
| `status` | `"completed" \| "failed"` |
| `entities` | `int` |
| `aligned` | `int` |
| `isActive` | `bool` |

**Emits:** `click`

**Computed:**

| Computed | Derivation |
|---|---|
| `badgeClass` | `status === 'completed' ? 'text-bg-success' : 'text-bg-danger'` |
| `entityLabel` | `entities.toLocaleString() + ' entities'` |

### Layer 2: SemanticSearchPanel.vue (radio + score columns changed)

| Prop | Type |
|---|---|
| `countryName` | `str` |
| `snapshotDate` | `"2025_12_31"` |

**Emits:** `search-results` (results[]), `query-graph` (graph)

**Store:** none. Fetches directly via axios (single-consumer, rule 01-frontend-patterns, section 3.2).

**Source state (data):**

| Field | Type |
|---|---|
| `queryMode` | `'tags' \| 'natural' \| 'template'` |
| `queryTagsInput` | `str` |
| `naturalQuery` | `str` |
| `templateQuery` | `str` |
| `lat`, `lon`, `rdfType`, `topK` | filters |
| `results` | `[]` |
| `loading`, `error`, `searched` | lifecycle |
| `executeTrace`, `aiAnswerElapsed`, `queryStartAt` | trace state |
| `displayParsedQuery`, `sseConnected` | parser state |

**Computed transforms:**

| Computed | Derivation |
|---|---|
| `isTagsMode` | `queryMode === 'tags'` |
| `isNaturalMode` | `queryMode === 'natural'` |
| `isTemplateMode` | `queryMode === 'template'` |
| `topKClamped` | `Math.min(100, Math.max(1, topK \|\| 20))` |
| `displayResults` | `results.map(normalizeResult).slice(0, topKClamped)` |
| `confidenceBadgeClass` | `>= 0.8` success, `>= 0.6` warning, else danger |

**Radio group (was 3 pill buttons):**

```html
<input type="radio" name="query-mode" v-model="queryMode" value="tags|natural|template">
```

Labels: Structured / Natural language / Structured Search (was "AI query", before that "Kuhn's Template").

**normalizeResult(r), the core shape normalizer:**

| Input shape | Output field |
|---|---|
| `r.geom?.lat ?? r.lat` | `geom: { lat: Number, lon: Number } \| null` |
| `r.osm_type, r.osm_id` | passthrough |
| `r.name \|\| ''` | `name` |
| `r.tags \|\| {}` | `tags` |
| `r.wkg_class \|\| null` | `wkg_class` |
| `r.scores \|\| { final_score: null }` | `scores` (passthrough) |
| `r.distance_m ?? null` | `distance_m` |

Applied at the fetch boundary, before storage.

```
  raw API result (two shapes)
      |
      +-- shape A: r.geom = { lat: 18.4, lon: -76.8 }
      |
      +-- shape B: r.lat = 18.4, r.lon = -76.8  (no geom object)
      |
      v
  normalizeResult(r)
      |
      +-- lat = r.geom?.lat ?? r.lat
      +-- lon = r.geom?.lon ?? r.lon
      +-- geom = (lat && lon) ? { lat: Number, lon: Number } : null
      |
      v
  canonical output:
    { osm_type, osm_id, name, tags, wkg_class,
      geom: { lat: Number, lon: Number } | null,
      scores: { ... } | { final_score: null },
      distance_m }
```

**Results table (was 5 columns, now 10):**

| Old columns | New columns |
|---|---|
| OSM ID, Tags, Class, Coords, Score | Name, OSM ID, Tags, Class, Coords, Name Score, Geo Score, Class Score, Tag Match, Final (bold) |

Score columns read from `item.scores.{name_score, geo_score, class_score, tag_match_score, final_score}`. The API already returns all scores, no backend change. Score columns can hide behind a toggle if width is tight.

**Method transforms:**

| Method | Transform |
|---|---|
| `performSearch()` | `axios.post('/nca/semantic-triplet-search/')` to `results.map(normalizeResult)` to `publishResults()` (emit search-results + query-graph) |
| `executeTemplateQuery()` | SSE stream to `/nca/execute-query/`, accumulate trace steps + AI answer, `result.results.map(normalizeResult)`, `aiAnswerElapsed = (Date.now() - queryStartAt) / 1000` |

### Layer 3: SubdivisionSelector.vue (unchanged, leaf)

| Prop | Type |
|---|---|
| `countryName` | `str` |

**Emits:** `subdivision-selected` (QID or null)

**Store:** none. Fetches `/api/nca/subdivisions/` directly (single-consumer, leaf fetches its own reference data).

**Computed:** `subdivisions` from `axios.get('/nca/subdivisions/').map(s => ({ wikidata_id, name }))`

### Layer 2: OsmEntitiesPanel.vue (merged 2026-09-28)

Merged the USLP and Map Labels tabs into one OSM Entities tab. A sub-mode
toggle renders AugmentedDataPanel (USLP — link summary, emits
`links-toggle`) or MapLabelsPanel (OSM Entity — class/entity labels via
`mapLabelsStore`). The `mode-change` emit gates `MapLabelsControls` (the
map-top-right sliders) in Home. (`PipelineMetricsPanel` and
`SpatialMetricsPanel` were removed 2026-09-28 — the Metrics tab and the
never-built Spatial metrics panel.)

### Layer 1: SystemSummaryModal.vue (unchanged)

Props: `open`. Emits: `close`. Store: none. Teleport to body. Tabs: Overview / Data / Storage / Paths / History.

## 2. Store: pipelineStore.js (Pinia Options Store)

### State (source)

**Granularity shift (year to full date):**

```
  OLD (runsByYear)                    NEW (usedSnapshotDates)
  -----------------                   ----------------------
  Set<"2025">                         Set<"2025_12_31">
  Set<"2024">                         Set<"2024_12_31">
                                       Set<"2023_12_31">

  fetchSnapshotJobs():                fetchSnapshotJobs():
    jobs                              jobs
      .filter(completed|running)         .filter(completed|running)
      .map(j =>                         .map(j =>
        j.snapshot_date                   j.snapshot_date        <-- full date
          .split('_')[0]                  )                       <-- no split
      )                                )

  startPipeline():                    startPipeline():
    runsByYear.add("2025")             usedSnapshotDates.add("2025_12_31")

  maybeFetchDurableResults():         maybeFetchDurableResults():
    runsByYear.has("2025")?             usedSnapshotDates.has("2025_12_31")?
```

| Field | Shape | Notes |
|---|---|---|
| `runs` | `{ [countryName]: { sessionId, status, steps[], logs[], snapshotDate, durable, summary, pipelineType, phases, error, startedAt, completedAt } }` | one run per country |
| `usedSnapshotDates` | `{ [countryName]: Set<"YYYY_MM_DD"> }` | renamed from `runsByYear`. Rebuilt from `snapshotJobs` in `fetchSnapshotJobs()`. Old: `j.snapshot_date.split('_')[0]` to `"2025"`. New: `j.snapshot_date` to `"2025_12_31"`. |
| `snapshotJobs` | `{ [countryName]: [{ snapshot_date, status, total_entities, total_aligned, total_spatial_links, pipeline_run_id, started_at, completed_at }] }` | raw job rows |
| `wsConnections` | `{ [sessionId]: WebSocket }` | active WS connections |
| `loading` | `bool` | fetch in flight |

### Getters (derivation from store state)

| Getter | Derivation |
|---|---|
| `activeRun(country)` | `runs[country]` if status is running/in_progress, else null |
| `latestRun(country)` | `runs[country] \|\| null` |
| `completedCount(run)` | `run.steps.filter(s => s.status === 'completed').length` |
| `progressPct(run)` | `run.status === 'completed' ? 100 : round(completed / total * 100)` |

### Actions (server-data fetching + state mutations)

**fetchRunState(country):**

1. `GET /app-state/{country}/` + `GET /worldkg-pipeline/state/{country}/` in parallel
2. `buildStepsFromState(pipelineData, pipelineSteps)`: raw backend state to step array
   - Input: `{ completed_stages[], current_stage, is_complete, pipeline_steps[] }`
   - Output: `[{ name, label, status, message, pct }]`
   - Transform per step: `completed.has(name)` to `'completed'` (pct=100), `name === current` to `'in_progress'`, `isComplete && !completed` to `'skipped'`, else `'pending'`
   - Immutable: `.map()` returns new array
3. `runs[country] = { sessionId, status, steps, error, ... }`
4. If running: `connectWebSocket(sessionId, country)`

**fetchSnapshotJobs(country, iso):**

1. `GET /snapshot-jobs/{iso}/`
2. `snapshotJobs[country] = data.jobs`
3. `usedSnapshotDates = new Set(jobs.filter(completed | running).map(j => j.snapshot_date))` (full date, not year)
4. Diffusion: raw job rows to used-date cache

**fetchSnapshotJobResults(country, iso, date):**

1. `GET /snapshot-jobs/{iso}/{date}/results/`
2. Diffusion: TaskResult rows to step array + durable run
   - Input: `[{ task_name, status, result, date_done, date_created }]`
   - Output: `[{ name, label, status, message, pct, dateDone, durationMs }]`
   - Transform: `task_name.replace(/^step_\d+_/, '')` to stepName, `SUCCESS` to `'completed'`, `FAILURE` to `'failed'`, `REVOKED` to `'skipped'`, else `'pending'`. `durationMs = new Date(date_done) - new Date(date_created)`. Message parsed from result JSON (igea_accepted, subgraph, snapshot_pbf_path).
   - Immutable: `.map()` returns new array
3. `runs[country] = { sessionId, status, steps, error, snapshotDate, durable: true, summary: { totalEntities, totalAligned, totalSpatialLinks } }`

**startPipeline(country, date, opts):**

1. `GET /app-state/{country}/` to get `pipelineSteps`
2. `_initRun(country, 'running', 'worldkg', pipelineSteps)`
3. `usedSnapshotDates.add(date)` (full date, was year)
4. `POST /worldkg-pipeline-v2/start/ { country_name, snapshot_date, force }`
5. `runs[country].sessionId = data.pipeline_run_id`
6. `connectWebSocket(sessionId, country)`

**WebSocket message handlers (`_handleWsMessage`):**

| Message type | Handler | Diffusion |
|---|---|---|
| `step_update` | `_applyStepUpdate(run, msg)` | WS msg to step mutation: find step by name, set status/message/pct, auto-complete prior pending steps (idx > 0), set `run.status = 'running'` |
| `pipeline_complete` | `_applyPipelineComplete(run, msg)` | completion msg to terminal state: `'completed'` sets all in_progress/pending to `'completed'` (pct=100), `'skipped'` sets in_progress to `'skipped'` + error, else `'failed'` + error. Set `completedAt`, `disconnectWebSocket(sessionId)`. |
| `log` | `run.logs.push({ message, time })` | cap 200 entries |

**WebSocket lifecycle:**

```
  startPipeline()
      |
      v
  POST /worldkg-pipeline-v2/start/
      |
      v
  sessionId = data.pipeline_run_id
      |
      v
  connectWebSocket(sessionId, country)
      |
      v
  ws.onmessage --> _handleWsMessage(msg, country)
      |
      +-- 'step_update'      --> _applyStepUpdate(run, msg)
      |                            step.status = msg.status
      |                            auto-complete prior pending
      |                            run.status = 'running'
      |
      +-- 'pipeline_complete' --> _applyPipelineComplete(run, msg)
      |                            run.status = 'completed'|'failed'|'skipped'
      |                            run.completedAt = now
      |                            disconnectWebSocket(sessionId)
      |
      +-- 'log'              --> run.logs.push({message, time})
                                 cap 200

  ws.onclose --> if run still active, reconnect in 5s
                 else: wsConnections[sessionId] = null
```

## 3. Data Flow Summary (end to end)

```
  Backend API          Store (source)         Component (computed)      Template
  -----------          ----------------       ---------------------     --------
                                            
  /system/status/      Home.vue.data          SnapshotCalendar          date picker
    snapshot_dates  -->  snapshotDates    -->   availableDates       --> job buttons
    completed_dates     (applySnapshot-       isDateUsed
                         Dates)                isDateActive
                                               jobButtons
                                               (filter+sort+map)
                                            
  /snapshot-jobs/{iso}/ store.snapshotJobs --> passed as prop       --> .jobs prop
    jobs[]              store.usedSnapshot-    (props-first)
                        Dates (full dates)
                                            
  /snapshot-jobs/{iso}/ store.runs[country] --> PipelineProgressV3   --> steps list
    {date}/results/       .steps (from           computed:              v-if="!(isDurable
    task_results[]          TaskResult)           run, status,            && isComplete)"
    status                .durable = true         isDurable,            HIDDEN for
    total_entities        .summary               isComplete            durable completed
                                                                     --> summary block
                                            
  /worldkg-pipeline-   store.runs[country] --> PipelineProgressV3   --> steps (open)
    v2/start/              .status='running'      computed:              progress bar
    pipeline_run_id        .sessionId             isRunning,            badge: Running
                          connectWebSocket()      currentStepName
                                            
  WebSocket            store._handleWsMsg  --> computed re-evaluates  --> steps (live)
    step_update          _applyStepUpdate       status watcher          step icons
    pipeline_complete    _applyPipelineComplete  emits pipeline-done   badge: Complete
    log                  run.logs.push          to onPipelineDone      logs panel
                                                 to fetchCountryStatus
                                                 to fetchSnapshotJobs
                                            
  /nca/semantic-      SemanticSearchPanel --> displayResults        --> results table
    triplet-search/      .results (raw)         .map(normalizeResult)   10 columns
    results[]            .map(normalizeResult)   .slice(0, topKClamped) (Name + 4 scores
    scores{...}           geom unification                              + existing 5)
                           scores passthrough
                           name extraction
```

Detailed table form:

| Backend API | Store (source state) | Component (computed) | Template (presentation) |
|---|---|---|---|
| `/system/status/` snapshot_dates, completed_dates | `Home.vue` data: `snapshotDates` (via `applySnapshotDates`) | `availableDates` (union), `isDateUsed`, `isDateActive`, `jobButtons` (filter+sort+map) | SnapshotCalendar date picker, job button list |
| `/snapshot-jobs/{iso}/` jobs[] | `store.snapshotJobs`, `store.usedSnapshotDates` (rebuilt: full dates) | passed as prop (props-first, no recompute in child) | SnapshotCalendar `.jobs` prop |
| `/snapshot-jobs/{iso}/{date}/results/` task_results[], status, total_entities, total_aligned | `store.runs[country]`: `.steps` (built from TaskResult rows), `.durable = true`, `.summary` | PipelineProgressPanelV3 computed: `run`, `status`, `isDurable`, `isComplete` | steps list `v-if="!(isDurable && isComplete)"`, hidden for durable completed, summary block replaces it |
| `/worldkg-pipeline-v2/start/` pipeline_run_id | `store.runs[country]`: `.status = 'running'`, `.sessionId`, `connectWebSocket()` | PipelineProgressPanelV3 computed: `isRunning`, `currentStepName` | steps list (open), progress bar, badge: Running |
| WebSocket `/ws/pipeline/{sid}/` step_update, pipeline_complete, log | `store._handleWsMessage`: `_applyStepUpdate`, `_applyPipelineComplete`, `run.logs.push` | computed re-evaluates, status watcher emits `pipeline-done` to `Home.onPipelineDone` to `fetchCountryStatus()` + `store.fetchSnapshotJobs()` | steps list (live), step icons, badge: Complete, logs panel |
| `/nca/semantic-triplet-search/` results[], scores{...} | `SemanticSearchPanel` data: `.results` (raw), `.map(normalizeResult)`: geom unification, scores passthrough, name extraction | `displayResults`: `.map(normalizeResult).slice(0, topKClamped)` | results table, 10 columns (Name + 4 scores + existing 5) |

## 4. Core Concepts (functional roles in the diffusion)

| Concept | Functional role |
|---|---|
| Source State | API responses stored once, never derived. `Home.vue` data + `pipelineStore.state`. Everything else is computed. |
| Computed Transform | Takes source state, produces derived value. Pure, reactive, never mutates. The filter+map+sort pipeline. Lives in component `computed{}` (props-derived) or store getters (store-derived). |
| NormalizeResult | Shape unifier: raw API result (two shapes, `geom:{lat,lon}` vs `lat,lon`) to canonical `{osm_type, osm_id, name, tags, wkg_class, geom, scores, dist}`. Applied at the fetch boundary, before storage. |
| buildStepsFromState | State reconstructor: backend pipeline state (`completed_stages`, `current`) to step array with per-step status. Immutable `.map()`, never mutates the input state. |
| usedSnapshotDates | Date cache: SnapshotJob rows to Set of full dates with completed/running jobs. Granularity shift: year string to full `YYYY_MM_DD`. |
| Durable flag | Provenance marker: distinguishes DB-backed results (`durable: true`, no WS) from live results (`durable: false`, WS-driven). Drives the steps-list gate. |
| Props-First | Anti-duplication rule: data the parent already holds flows down as props. SnapshotCalendar receives `usedDates` + `jobs` as props, never calls the store. |
| Three States | Completeness contract: every data unit renders loading (skeleton), error (message+retry), empty (guidance). SnapshotCalendar must implement all three. |
| Steps-List Gate | Visibility diffusion: `isDurable && isComplete` hides steps list. Summary block (entities/aligned/links) replaces it as the "result present" view. Running, failed, durable-failed keep steps list visible. |
| Config-Driven | Data-only contract: `jobButtons` config = `{date, status, entities, ...}`. No handlers in config. Clicks emit up. JobButton is a dumb leaf. |
