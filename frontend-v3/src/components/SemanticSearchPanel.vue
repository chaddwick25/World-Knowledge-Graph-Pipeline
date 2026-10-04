<template>
  <div class="d-flex flex-column gap-2">
    <!-- Query mode radio group — always visible (the mode switcher; only
         the sub-mode content below swaps). Kept OUT of the v-else block
         so the Researcher mode can switch back (2026-10-01). -->
    <div class="d-flex flex-column gap-1">
      <div class="btn-group btn-group-sm" role="group" aria-label="Query mode">
        <input
          type="radio"
          class="btn-check"
          name="query-mode"
          id="query-mode-template"
          autocomplete="off"
          value="template"
          v-model="queryMode"
        >
        <label class="btn btn-outline-secondary" for="query-mode-template">AI query</label>

        <input
          type="radio"
          class="btn-check"
          name="query-mode"
          id="query-mode-tags"
          autocomplete="off"
          value="tags"
          v-model="queryMode"
        >
        <label class="btn btn-outline-secondary" for="query-mode-tags">OSM Tag Query</label>

        <input
          type="radio"
          class="btn-check"
          name="query-mode"
          id="query-mode-research"
          autocomplete="off"
          value="researcher"
          v-model="queryMode"
        >
        <label class="btn btn-outline-secondary" for="query-mode-research">Researcher</label>
      </div>
    </div>

    <!-- Subdivision selector — always visible (2026-10-01): scopes the
         sample questions, the structured/template searches, AND the
         Researcher sub-mode (its QID rides the research endpoints). -->
    <SubdivisionSelector
      :country-name="countryName"
      @subdivision-selected="onSubdivisionSelected"
    />

    <!-- Researcher sub-mode (2026-10-01): the general research loop
         (KE interview → brief → decompose → execute → assemble) folded
         in from the removed standalone Research tab. -->
    <ResearchPanel
      v-if="isResearcherMode"
      :country-name="countryName"
      :snapshot-date="snapshotDate"
      :subdivision-qid="subdivisionQid"
    />
    <template v-else>
    <form class="d-flex flex-column gap-2" @submit.prevent="performSearch">

      <!-- Tag key/value (OSM Tag Query mode) -->
      <div v-if="isTagsMode" class="row g-2">
        <div class="col-6">
          <label class="form-label small text-secondary mb-0">Tag key</label>
          <select v-model="tagKey" class="form-select form-select-sm">
            <option v-for="opt in tagKeyOptions" :key="opt.value" :value="opt.value">
              {{ opt.text }}
            </option>
          </select>
        </div>
        <div class="col-6">
          <label class="form-label small text-secondary mb-0">Tag value</label>
          <input
            v-model="tagValue"
            type="text"
            class="form-control form-control-sm"
            placeholder="e.g. cafe (empty = has key)"
          />
        </div>
      </div>

      <!-- Natural language template input (MapQA parser) -->
      <div v-if="isTemplateMode" class="d-flex flex-column gap-1">
        <label class="form-label small text-secondary mb-0">Geospatial question</label>
        <textarea
          v-model="templateQuery"
          class="form-control form-control-sm font-monospace"
          rows="2"
          placeholder="Which bars are within 50m of Hollywood Blvd?"
        ></textarea>
      </div>

      <!-- Filters (hidden in NL Template mode) -->
      <div v-if="!isTemplateMode" class="row g-2">
        <div class="col-4">
          <label class="form-label small text-secondary mb-0">Lat</label>
          <input v-model="lat" type="number" step="any" class="form-control form-control-sm" placeholder="Optional" />
        </div>
        <div class="col-4">
          <label class="form-label small text-secondary mb-0">Lon</label>
          <input v-model="lon" type="number" step="any" class="form-control form-control-sm" placeholder="Optional" />
        </div>
        <div class="col-4">
          <label class="form-label small text-secondary mb-0">Class</label>
          <select v-model="rdfType" class="form-select form-select-sm">
            <option :value="null">Any class</option>
            <option v-for="opt in classOptions" :key="opt.value" :value="opt.value">
              {{ opt.text }}
            </option>
          </select>
        </div>
      </div>

      <!-- Top K results limit + Search button (inline) -->
      <div class="d-flex align-items-center gap-2">
        <label class="form-label small text-secondary mb-0 text-nowrap">Top K</label>
        <input
          v-model.number="topK"
          type="number"
          min="1"
          max="100"
          class="form-control form-control-sm"
          style="max-width: 90px;"
        />
        <button
          type="submit"
          class="btn btn-primary btn-sm flex-grow-1"
          :disabled="isLoading || !isValid"
        >
          <span v-if="isLoading" class="spinner-border spinner-border-sm me-1" role="status" aria-hidden="true"></span>
          {{ isLoading ? 'Searching…' : 'Search' }}
        </button>
      </div>
    </form>

    <!-- Sample questions (OSM RAG mode learning aid — leaf component;
         shown right below the search form so they're handy before
         searching; the chevron collapses the list) -->
    <SampleQuestions
      v-if="isTemplateMode && filteredSampleQuestions.length"
      :questions="filteredSampleQuestions"
      :scope="subdivisionName || countryName"
    />

    <!-- Error -->
    <div v-if="displayError" class="alert alert-danger small py-1 px-2 mb-0">
      {{ displayError }}
    </div>

    <!-- Data answer (deterministic factor-join — arrives first, stays pinned) -->
    <div v-if="executeAnswer" class="alert alert-info small py-1 px-2 mb-1">
      <div class="text-secondary small mb-1">
        Retrieval time: <span class="text-danger">{{ dataAnswerElapsed != null ? dataAnswerElapsed.toFixed(1) : '—' }}s</span>
      </div>
      <strong>Deterministic:  </strong> {{ executeAnswer }}
    </div>

    <!-- AI answer (enrichment stream — completes when ready, held back
         until the data answer has had its solo moment) -->
    <div v-if="enrichingContext && !aiAnswerAt" class="small text-secondary">
      Enriching with AI…
    </div>
    <div v-if="aiAnswerAt && enrichedAnswer" class="alert alert-success small py-1 px-2 mb-0">
      <div class="text-secondary small mb-1">
        Retrieval time: <span class="text-danger">{{ aiAnswerElapsed != null ? aiAnswerElapsed.toFixed(1) : '—' }}s</span>
      </div>
      <strong>AI Summary: </strong> {{ enrichedAnswer }}
    </div>

    <!-- Execution trace — decision flow (each step shows what it decided).
         Rendered by the ExecutionTraceFlow leaf component (monolith split). -->
    <ExecutionTraceFlow
      :nodes="traceFlow"
      :parsed-query="displayParsedQuery"
      :confidence-badge-class="confidenceBadgeClass"
    />

    <!-- Results table — leaf component (props in, events out). -->
    <SearchResultsTable
      v-model:show-nameless="showNameless"
      :rows="displayResults"
      :total="cappedResults.length"
      :has-nameless="hasNameless"
      :score-columns="activeScoreColumns"
      @name-hover="onResultNameHover"
      @name-leave="onResultNameLeave"
    />

    <div v-if="!displayResults.length && searched" class="small text-secondary">
      No results found for this query.
    </div>
    </template>
  </div>
</template>

<script>
/**
 * SemanticSearchPanel
 *
 * Embedded panel for querying OSM entities via semantic triplet search.
 * Refactored from SemanticSearchModal.vue — no modal shell, designed
 * to fit inside the sidebar tab panel.
 *
 * Props:
 *   countryName  - Required. Country to search within.
 *   snapshotDate - Optional. Snapshot date string (e.g. "2025_12_31").
 *                  Forwarded to the backend so searches can be scoped to a
 *                  specific pipeline run once the data layer supports it.
 *
 * Emits:
 *   search-results  - Array of result objects (for map markers)
 */

import axios from 'axios'
import ExecutionTraceFlow from './ExecutionTraceFlow.vue'
import SubdivisionSelector from './SubdivisionSelector.vue'
import ResearchPanel from './ResearchPanel.vue'
import SampleQuestions from './SampleQuestions.vue'
import SearchResultsTable from './SearchResultsTable.vue'
import { useEntityInfoStore } from '../stores/entityInfoStore'
import { decorateStep } from '../mapViz/traceNodeDecorators'
import { normalizeResult, buildQueryGraph, buildTemplateVisualization } from '../mapViz/queryGraphBuild'
import {
  openTemplateStream,
  clearAiAnswerTimer,
} from '../services/templateStream'
import sampleQuestionsCsv from '../assets/sample_questions.csv?raw'

/**
 * Minimal CSV parse (RFC4180 subset) — handles double-quoted fields
 * (sample questions may contain commas and apostrophes).
 */
function parseCsv(text) {
  const rows = []
  let row = []
  let field = ''
  let inQuotes = false
  for (let i = 0; i < text.length; i++) {
    const ch = text[i]
    if (inQuotes) {
      if (ch === '"') {
        if (text[i + 1] === '"') { field += '"'; i++ } else inQuotes = false
      } else field += ch
    } else if (ch === '"') {
      inQuotes = true
    } else if (ch === ',') {
      row.push(field); field = ''
    } else if (ch === '\n' || ch === '\r') {
      if (ch === '\r' && text[i + 1] === '\n') i++
      row.push(field); field = ''
      if (row.length && row.some((c) => c !== '')) rows.push(row)
      row = []
    } else {
      field += ch
    }
  }
  if (field !== '' || row.length) { row.push(field); rows.push(row) }
  return rows
}

/** Parsed sample questions from the bundled CSV: {country_code, question, template}. */
const SAMPLE_QUESTIONS = parseCsv(sampleQuestionsCsv)
  .slice(1) // drop the header row
  .filter((r) => r.length >= 2 && r[1])
  .map((r) => ({ country_code: (r[0] || '').toUpperCase(), question: r[1], template: r[2] || '' }))

export default {
  name: 'SemanticSearchPanel',
  components: { ExecutionTraceFlow, SubdivisionSelector, ResearchPanel, SampleQuestions, SearchResultsTable },
  setup() {
    const entityInfoStore = useEntityInfoStore()
    return { entityInfoStore }
  },
  props: {
    countryName: {
      type: String,
      required: true,
    },
    countryCode: {
      type: String,
      default: '',
    },
    snapshotDate: {
      type: String,
      default: null,
    },
  },
  emits: ['search-results', 'query-graph', 'template-visualization'],
  data() {
    return {
      // ── Query state (component-local) ──
      queryMode: 'template',
      // OSM Tag Query mode: key dropdown + free-text value → query_tags.
      tagKey: 'name',
      tagValue: 'cafe',
      templateQuery: '',
      lat: '',
      lon: '',
      rdfType: null,
      topK: 10,
      loading: false,
      error: null,
      results: [],
      searched: false,
      // Unnamed-entity toggle (2026-10-01): off by default — the
      // nameless filter hides noise; the funnel icon beside Results
      // shows them when the AI answer cites entities the table hides.
      showNameless: false,
      subdivisionQid: null,
      // Selected subdivision's display name (rides the same emit) — used
      // by the "Sample Questions - <scope>" label.
      subdivisionName: null,
      // Sample questions served by the backend (country or subdivision
      // scope); empty until the fetch resolves — the bundled CSV is the
      // fallback only when the API is unreachable.
      sampleQuestions: [],
      // MapQA parser state (sync template mode)
      parsedQuery: null,
      executeAnswer: null,
      executeTrace: [],
      // SSE streaming state (template mode — direct path, no agent)
      eventSource: null,
      enrichedAnswer: '',
      enrichingContext: null,
      // Data-answer min-display window: the raw answer must stay visible
      // alone for a beat before the AI region appears.
      minDataAnswerMs: 900,
      dataAnswerAt: null,
      aiAnswerAt: null,
      aiAnswerTimer: null,
      // Answer timing labels (seconds from query start)
      queryStartAt: null,
      dataAnswerElapsed: null,
      aiAnswerElapsed: null,
      classOptions: [
        { value: 'wkgs:Cafe', text: 'Cafe' },
        { value: 'wkgs:Restaurant', text: 'Restaurant' },
        { value: 'wkgs:Hotel', text: 'Hotel' },
        { value: 'wkgs:Hospital', text: 'Hospital' },
        { value: 'wkgs:School', text: 'School' },
        { value: 'wkgs:Shop', text: 'Shop' },
        { value: 'wkgs:Amenity', text: 'Amenity (general)' },
      ],
      // OSM tag keys for the OSM Tag Query dropdown. `name` is the
      // default (fuzzy name search) — the backend routes it to the fuzzy
      // name search instead of exact tag matching.
      tagKeyOptions: [
        { value: 'name', text: 'name (fuzzy search)' },
        { value: 'amenity', text: 'amenity' },
        { value: 'shop', text: 'shop' },
        { value: 'cuisine', text: 'cuisine' },
        { value: 'tourism', text: 'tourism' },
        { value: 'leisure', text: 'leisure' },
        { value: 'office', text: 'office' },
        { value: 'healthcare', text: 'healthcare' },
        { value: 'craft', text: 'craft' },
        { value: 'building', text: 'building' },
        { value: 'natural', text: 'natural' },
        { value: 'place', text: 'place' },
        { value: 'historic', text: 'historic' },
        { value: 'man_made', text: 'man_made' },
        { value: 'highway', text: 'highway' },
        { value: 'railway', text: 'railway' },
        { value: 'waterway', text: 'waterway' },
        { value: 'power', text: 'power' },
        { value: 'landuse', text: 'landuse' },
        { value: 'aeroway', text: 'aeroway' },
        { value: 'emergency', text: 'emergency' },
        { value: 'military', text: 'military' },
        { value: 'sport', text: 'sport' },
        { value: 'barrier', text: 'barrier' },
        { value: 'telecom', text: 'telecom' },
      ],
    }
  },
  computed: {
    isTagsMode() {
      return this.queryMode === 'tags'
    },
    isTemplateMode() {
      return this.queryMode === 'template'
    },
    /** Researcher sub-mode (2026-10-01): the general research loop that
     *  was folded in from the removed standalone Research tab. */
    isResearcherMode() {
      return this.queryMode === 'researcher'
    },
    /** Sample questions served by the backend (country or subdivision
     *  scope — the service generates subdivision questions from the
     *  entities inside the subdivision). Falls back to the bundled CSV
     *  only when the API fetch fails. */
    filteredSampleQuestions() {
      if (this.sampleQuestions.length) return this.sampleQuestions.slice(0, 8)
      // Fallback: bundled CSV (backend unreachable) — country-filtered.
      const iso = (this.countryCode || '').toUpperCase()
      const country = SAMPLE_QUESTIONS.filter((q) => q.country_code === iso)
      const generic = SAMPLE_QUESTIONS.filter((q) => !q.country_code)
      return (country.length ? country : generic).slice(0, 8)
    },
    isValid() {
      if (this.isTagsMode) {
        // Key always comes from the dropdown; value may stay empty (the
        // backend treats it as a has-key filter).
        return !!this.tagKey
      }
      return this.templateQuery.trim().length > 0
    },
    isLoading() {
      return this.loading
    },
    displayError() {
      return this.error
    },
    displayParsedQuery() {
      return this.parsedQuery
    },
    /** Trace steps rendered as a decision-flow (Layout B): each node shows
     *  the step, its inputs, and what it decided. Decorators live in
     *  mapViz/traceNodeDecorators.js (dispatch table); resultsByName feeds
     *  the compare_closer candidate name join. */
    traceFlow() {
      const resultsByName = new Map()
      for (const r of this.displayResults) {
        const key = String(r.name || '').toLowerCase()
        if (key) resultsByName.set(key, r)
      }
      return (this.executeTrace || []).map((s) => decorateStep(s, { resultsByName }))
    },
    /** Top K clamped to the backend-supported 1–100 range. */
    topKClamped() {
      return Math.min(100, Math.max(1, parseInt(this.topK, 10) || 10))
    },
    /** All normalized results capped at Top K — before the nameless
     *  filter, so the header can show "Results (1 / 4)" when some rows
     *  are hidden. */
    cappedResults() {
      return this.results
        .map(normalizeResult)
        .slice(0, this.topKClamped)
    },
    /** Whether any capped result lacks a name — the funnel toggle is
     *  only meaningful then, and it stays visible in both states so the
     *  filter can be turned back on (2026-10-01). */
    hasNameless() {
      return this.cappedResults.some((r) => !(r.name || '').trim())
    },
    /** Normalized + top-k-capped results (table rows and map entities).
     *  Nameless entities are filtered out by default (2026-09-28):
     *  unnamed noise (traffic islands, generic multipolygons) ranked high
     *  with no name to show. Presentation-only — the re-rank and backend
     *  are untouched. The funnel toggle (showNameless, 2026-10-01) opts
     *  into showing them — the AI answer may cite bars the table hides. */
    displayResults() {
      if (this.showNameless) return this.cappedResults
      return this.cappedResults.filter((r) => (r.name || '').trim().length > 0)
    },
    /** Score columns that have at least one non-zero value in the current
     *  result set. Columns that are always 0 for a given search mode
     *  (e.g. Geo without lat/lon, Tag without specific tags) are hidden. */
    activeScoreColumns() {
      const cols = [
        { key: 'name_score',       label: 'Name'  },
        { key: 'geo_score',        label: 'Geo'   },
        { key: 'class_score',      label: 'Class' },
        { key: 'tag_match_score',  label: 'Tag'   },
      ]
      return cols.filter((c) =>
        this.displayResults.some((r) => {
          const v = r.scores?.[c.key]
          return v != null && v !== 0 && !Number.isNaN(v)
        })
      )
    },
    /** Confidence badge color: green >= 80%, yellow >= 60%, red < 60%. */
    confidenceBadgeClass() {
      const c = this.displayParsedQuery?.confidence || 0
      if (c >= 0.8) return 'text-bg-success'
      if (c >= 0.6) return 'text-bg-warning'
      return 'text-bg-danger'
    },
  },
  watch: {
    countryName() {
      this.reset()
    },
    countryCode() {
      this.fetchSampleQuestions()
    },
    // Subdivision selection changes the question scope — refetch so the
    // sample questions reflect operations within the subdivision.
    subdivisionQid() {
      this.fetchSampleQuestions()
    },
    // Live control: re-slice the emitted graph without re-querying.
    topK() {
      this.publishResults()
    },
  },
  mounted() {
    this.fetchSampleQuestions()
  },
  // Lifecycle balance — close any open SSE stream (rules §1.2).
  unmounted() {
    this.eventSource?.close()
    this.eventSource = null
  },
  methods: {
    reset() {
      this.queryMode = 'tags'
      this.tagKey = 'name'
      this.tagValue = 'cafe'
      this.templateQuery = ''
      this.lat = ''
      this.lon = ''
      this.rdfType = null
      this.topK = 10
      this.loading = false
      this.error = null
      this.results = []
      this.searched = false
      this.subdivisionQid = null
      this.subdivisionName = null
      this.parsedQuery = null
      this.executeAnswer = null
      this.executeTrace = []
      this.eventSource?.close()
      this.eventSource = null
      this.enrichedAnswer = ''
      this.enrichingContext = null
      this.$emit('template-visualization', null)
    },

    async performSearch() {
      await this.executeSync()
    },

    onSubdivisionSelected(qid, name) {
      this.subdivisionQid = qid
      this.subdivisionName = name
    },

    /** Fetch the sample questions from the backend (country scope, or the
     *  subdivision scope when one is selected — the service generates
     *  subdivision questions from the entities inside the subdivision).
     *  On failure the bundled CSV remains the fallback. */
    async fetchSampleQuestions() {
      try {
        const params = new URLSearchParams({ country_code: this.countryCode || '' })
        if (this.subdivisionQid) params.set('subdivision_qid', this.subdivisionQid)
        if (this.snapshotDate) params.set('snapshot_date', this.snapshotDate)
        const { data } = await axios.get(`/nca/sample-questions/?${params}`)
        this.sampleQuestions = (data.questions || []).map((q) => ({
          question: q.question,
          template: q.template || '',
          source: q.source || '',
        }))
      } catch (err) {
        // Backend unreachable → the bundled CSV fallback in
        // filteredSampleQuestions covers the panel.
        this.sampleQuestions = []
      }
    },

    /** Hovering a result name pops the same info card as a marker click
     *  (both write entityInfoStore). The name link already underlines on
     *  hover via .results-name-link. */
    onResultNameHover(item) {
      this.entityInfoStore.setEntity(item, 'table')
    },
    onResultNameLeave(item) {
      this.entityInfoStore.clearEntity(`${item.osm_type}:${item.osm_id}`)
    },

    /** Execute the query — SSE stream (template) or POST (triplet search). */
    async executeSync() {
      this.loading = true
      this.error = null
      this.results = []
      this.searched = false
      this.parsedQuery = null
      this.executeAnswer = null
      this.executeTrace = []
      this.enrichedAnswer = ''
      this.enrichingContext = null
      clearAiAnswerTimer(this)
      this.dataAnswerAt = null
      this.aiAnswerAt = null
      this.dataAnswerElapsed = null
      this.aiAnswerElapsed = null
      this.eventSource?.close()
      this.eventSource = null

      if (this.isTemplateMode) {
        // NL Template mode: MapQA parser + executor over SSE (direct path).
        if (!this.templateQuery.trim()) {
          this.error = 'Please enter a geospatial question'
          this.loading = false
          return
        }
        console.log('[SSE] executeSync template mode @', Date.now())
        openTemplateStream(this)
        return  // loading is cleared by the stream's done/error events
      }

      try {
        // OSM Tag Query mode uses triplet search
        const payload = {
          country_code: this.countryName,
          top_k: this.topKClamped,
        }

        if (this.isTagsMode) {
          // Key from dropdown + free-text value → query_tags; empty value
          // means has-key matching on the backend.
          payload.query_tags = { [this.tagKey]: this.tagValue.trim() }
        }

        if (this.lat) payload.lat = parseFloat(this.lat)
        if (this.lon) payload.lon = parseFloat(this.lon)
        if (this.rdfType) payload.rdf_type = this.rdfType
        if (this.subdivisionQid) payload.subdivision_qid = this.subdivisionQid

        if (this.snapshotDate) {
          payload.snapshot_date = this.snapshotDate
        }

        const response = await axios.post('/nca/semantic-triplet-search/', payload)
        this.results = (response.data.results || []).map(normalizeResult)
        this.searched = true
        this.publishResults()
      } catch (err) {
        console.error('Semantic search failed:', err)
        this.error = err.response?.data?.error || err.message || 'Search failed'
      } finally {
        this.loading = false
      }
    },

    /** Emit normalized, top-k-capped entities + the anchor/entity graph. */
    publishResults() {
      const entities = this.displayResults
      const graph = buildQueryGraph({
        entities,
        trace: this.executeTrace,
        topK: this.topKClamped,
      })
      // Map markers (+ backward-compat circle fallback) — only entities with coords.
      this.$emit('search-results', entities.filter((e) => e.geom))
      this.$emit('query-graph', graph)
      this.$emit(
        'template-visualization',
        buildTemplateVisualization({
          isTemplateMode: this.isTemplateMode,
          parsedQuery: this.parsedQuery,
          templateQuery: this.templateQuery,
          trace: this.executeTrace,
          entities,
          graph,
        })
      )
    },

  },
}
</script>

<style scoped>
/* Minimal residual CSS — Bootstrap utilities cover the rest.
   Only keep the cursor-pointer helper for <summary> (Bootstrap doesn't
   provide a utility for this). */
.cursor-pointer {
  cursor: pointer;
}

/* Result-table styles moved to SearchResultsTable.vue (scoped CSS does
   not cross into child components). */

/* Form fields + selectors: standardized 12px with comfortable 32px
   touch targets (Stitch design pass 2026-09-21). */
.form-control-sm,
.form-select-sm {
  font-size: 0.75rem;
  min-height: 32px;
}

/* The font-monospace utility uses Bootstrap's --bs-font-monospace; ensure
   the textarea inherits a readable mono stack in dark mode. */
.font-monospace {
  font-family: var(--bs-font-monospace, ui-monospace, SFMono-Regular, Menlo,
    Monaco, Consolas, 'Liberation Mono', 'Courier New', monospace) !important;
}
</style>
