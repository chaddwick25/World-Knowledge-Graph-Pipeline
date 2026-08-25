<template>
  <div class="d-flex flex-column gap-2">
    <form class="d-flex flex-column gap-2" @submit.prevent="performSearch">
      <!-- Query mode toggle (hidden in agent mode — template only) -->
      <div v-if="!agentMode" class="d-flex flex-column gap-1">
        <label class="form-label small text-secondary mb-0">Query mode</label>
        <div class="d-flex gap-1">
          <button
            type="button"
            class="btn btn-sm rounded-pill"
            :class="isTagsMode ? 'btn-primary' : 'btn-outline-secondary'"
            @click="queryMode = 'tags'"
          >
            Structured (JSON)
          </button>
          <button
            type="button"
            class="btn btn-sm rounded-pill"
            :class="isNaturalMode ? 'btn-primary' : 'btn-outline-secondary'"
            @click="queryMode = 'natural'"
          >
            Natural language
          </button>
          <button
            type="button"
            class="btn btn-sm rounded-pill"
            :class="isTemplateMode ? 'btn-primary' : 'btn-outline-secondary'"
            @click="queryMode = 'template'"
          >
            Kuhn's Template
          </button>
        </div>
      </div>

      <!-- Subdivision selector (hidden in agent mode) -->
      <SubdivisionSelector
        v-if="!agentMode"
        :country-name="countryName"
        @subdivision-selected="subdivisionQid = $event"
      />

      <!-- Tags input (Structured JSON mode — human only) -->
      <div v-if="isTagsMode" class="d-flex flex-column gap-1">
        <label class="form-label small text-secondary mb-0">OSM Tag</label>
        <textarea
          v-model="queryTagsInput"
          class="form-control form-control-sm font-monospace"
          rows="2"
          placeholder='{"amenity": "cafe"}  or  {"name": "파리바게뜨"}'
        ></textarea>
      </div>

      <!-- Natural language name search input (human only) -->
      <div v-if="isNaturalMode" class="d-flex flex-column gap-1">
        <label class="form-label small text-secondary mb-0">Name search (any language)</label>
        <textarea
          v-model="naturalQuery"
          class="form-control form-control-sm font-monospace"
          rows="2"
          placeholder="paris bagueete  /  파리바게뜨  /  café  /  원탕"
        ></textarea>
      </div>

      <!-- Natural language template input (MapQA parser) -->
      <div v-if="isTemplateMode" class="d-flex flex-column gap-1">
        <label class="form-label small text-secondary mb-0">
          {{ agentMode ? 'Ask a geospatial question' : 'Geospatial question' }}
        </label>
        <textarea
          v-model="effectiveTemplateQuery"
          class="form-control form-control-sm font-monospace"
          rows="2"
          placeholder="Which bars are within 50m of Hollywood Blvd?"
          @input="agentMode && store.setTemplateQuery($event.target.value)"
        ></textarea>
      </div>

      <!-- Filters (hidden in NL Template mode and agent mode) -->
      <div v-if="!isTemplateMode && !agentMode" class="row g-2">
        <div class="col-6">
          <label class="form-label small text-secondary mb-0">Lat</label>
          <input v-model="lat" type="number" step="any" class="form-control form-control-sm" placeholder="Optional" />
        </div>
        <div class="col-6">
          <label class="form-label small text-secondary mb-0">Lon</label>
          <input v-model="lon" type="number" step="any" class="form-control form-control-sm" placeholder="Optional" />
        </div>
        <div class="col-6">
          <label class="form-label small text-secondary mb-0">Class</label>
          <select v-model="rdfType" class="form-select form-select-sm">
            <option :value="null">Any class</option>
            <option v-for="opt in classOptions" :key="opt.value" :value="opt.value">
              {{ opt.text }}
            </option>
          </select>
        </div>
        <div class="col-6">
          <label class="form-label small text-secondary mb-0">Top K</label>
          <input v-model="topK" type="number" min="1" max="100" class="form-control form-control-sm" />
        </div>
      </div>

      <button
        type="submit"
        class="btn btn-primary btn-sm"
        :disabled="isLoading || !isValid"
      >
        <span v-if="isLoading" class="spinner-border spinner-border-sm me-1" role="status" aria-hidden="true"></span>
        {{ isLoading ? 'Searching…' : (agentMode ? 'Run as agent' : 'Search') }}
      </button>
    </form>

    <!-- Error -->
    <div v-if="displayError" class="alert alert-danger small py-1 px-2 mb-0">
      {{ displayError }}
    </div>

    <!-- Streaming phase indicator (agent mode only) -->
    <div v-if="agentMode && store.streamingPhase && store.streamingPhase !== 'done'" class="d-flex align-items-center gap-2 small text-secondary">
      <span class="spinner-border spinner-border-sm" role="status" aria-hidden="true"></span>
      <span>{{ store.streamingPhase }}</span>
    </div>

    <!-- Parsed query (MapQA parser — template mode) -->
    <div v-if="displayParsedQuery" class="card card-body p-2">
      <div class="d-flex align-items-center gap-2 mb-1">
        <span class="small fw-semibold" style="color: var(--bs-primary-text-emphasis);">{{ displayParsedQuery.template }}</span>
        <span
          class="badge rounded-pill"
          :class="confidenceBadgeClass"
        >
          {{ (displayParsedQuery.confidence * 100).toFixed(0) }}% confident
        </span>
      </div>
      <div class="d-flex flex-wrap gap-1">
        <span
          v-for="(concept, idx) in displayParsedQuery.concepts"
          :key="idx"
          class="d-inline-flex align-items-center gap-1 small"
        >
          <span class="badge text-bg-info">{{ concept.type }}</span>
          <span class="text-secondary">{{ concept.text || '—' }}</span>
        </span>
      </div>
    </div>

    <!-- Answer summary (MapQA executor) -->
    <div v-if="displayAnswer" class="alert alert-success small py-1 px-2 mb-0">
      <strong>Answer:</strong> {{ displayAnswer }}
      <div v-if="displayTrace.length > 0" class="mt-1 small text-secondary">
        <details>
          <summary class="cursor-pointer">Execution trace ({{ displayTrace.length }} steps)</summary>
          <ol class="mt-1 ms-3">
            <li v-for="(step, idx) in displayTrace" :key="idx">
              <strong>{{ step.step }}</strong>
              <span v-if="step.input"> — in: {{ formatTraceValue(step.input) }}</span>
              <span v-if="step.output_count !== undefined"> — count: {{ step.output_count }}</span>
              <span v-if="step.output"> — out: {{ formatTraceValue(step.output) }}</span>
              <span v-if="step.output_km !== undefined"> — {{ step.output_km }} km</span>
              <span v-if="step.output_degrees !== undefined"> — {{ step.output_degrees }}°</span>
              <span v-if="step.error" class="text-danger"> — ERROR: {{ step.error }}</span>
            </li>
          </ol>
        </details>
      </div>
    </div>

    <!-- Live answer (agent streaming mode — token stream) -->
    <div v-if="agentMode && store.liveAnswer && store.streamingPhase !== 'done'" class="card card-body p-2 small">
      <strong class="text-secondary">Streaming answer:</strong>
      <p class="mb-0 mt-1">{{ store.liveAnswer }}</p>
    </div>

    <!-- Enrichment display (agent mode only — LLM research loop) -->
    <div v-if="agentMode && store.hasEnrichment" class="card card-body p-2">
      <h6 class="card-title small fw-semibold text-secondary mb-1">Research</h6>
      <div v-if="store.enrichment.actions?.length" class="d-flex flex-wrap gap-1 mb-1">
        <span
          v-for="action in store.enrichment.actions"
          :key="action"
          class="badge text-bg-primary"
        >{{ action }}</span>
      </div>
      <div v-if="store.enrichment.action_outputs" class="small text-secondary mb-1">
        <div v-for="(output, idx) in store.enrichment.action_outputs" :key="idx">
          {{ output.tool || `action ${idx + 1}` }}: {{ Array.isArray(output.output) ? `${output.output.length} entities` : output.output }}
        </div>
      </div>
      <div v-if="store.enrichment.enriched_answer" class="small">
        <strong class="text-success">Enriched answer:</strong> {{ store.enrichment.enriched_answer }}
      </div>
    </div>

    <!-- Results table (hidden in agent mode — map markers are primary) -->
    <div v-if="!agentMode && displayResults.length > 0" class="d-flex flex-column gap-1">
      <h6 class="small fw-semibold mb-0">Results ({{ displayResults.length }})</h6>
      <div class="table-responsive" style="max-height: 480px; overflow-y: auto;">
        <table class="table table-sm table-borderless mb-0" style="font-size: 0.72rem;">
          <thead class="table-dark">
            <tr>
              <th>OSM ID</th>
              <th>Tags</th>
              <th>Class</th>
              <th>Coords</th>
              <th>Score</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in displayResults" :key="`${item.osm_type}-${item.osm_id}`">
              <td>
                <a
                  :href="`https://www.openstreetmap.org/${item.osm_type}/${item.osm_id}`"
                  target="_blank"
                  class="text-decoration-none"
                  style="color: var(--bs-info-text-emphasis);"
                >
                  {{ item.osm_type }}/{{ item.osm_id }}
                </a>
              </td>
              <td><small>{{ formatTags(item.tags) }}</small></td>
              <td>{{ item.wkg_class || '—' }}</td>
              <td>
                <template v-if="item.geom">
                  {{ item.geom.lat.toFixed(3) }}, {{ item.geom.lon.toFixed(3) }}
                </template>
                <span v-else class="text-secondary">N/A</span>
              </td>
              <td class="text-end">
                <strong>{{ item.scores?.final_score?.toFixed(3) || '—' }}</strong>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <div v-else-if="!agentMode && searched" class="small text-secondary">
      No results found for this query.
    </div>
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
 *   agentMode    - Optional (default false). When true, the panel runs in
 *                  agent mode: template-only (no mode toggle, filters, or
 *                  subdivision), streaming via EventSource, enrichment
 *                  display, and store-backed state (useAgentQueryStore).
 *                  When false, full 3-mode controls, sync, component-local
 *                  state (current behavior).
 *
 * Emits:
 *   search-results  - Array of result objects (for map markers)
 */

import axios from 'axios'
import SubdivisionSelector from './SubdivisionSelector.vue'
import { useAgentQueryStore } from '../stores/agentQueryStore'
import { useTemplateQueryStream } from '../composables/useTemplateQueryStream'

export default {
  name: 'SemanticSearchPanel',
  components: { SubdivisionSelector },
  props: {
    countryName: {
      type: String,
      required: true,
    },
    snapshotDate: {
      type: String,
      default: null,
    },
    agentMode: {
      type: Boolean,
      default: false,
    },
  },
  emits: ['search-results'],
  setup() {
    const store = useAgentQueryStore()
    const stream = useTemplateQueryStream()
    return { store, stream }
  },
  data() {
    return {
      // ── Human mode state (component-local) ──
      queryMode: 'tags',
      queryTagsInput: '{"amenity": "cafe"}',
      naturalQuery: '',
      templateQuery: '',
      lat: '',
      lon: '',
      rdfType: null,
      topK: 20,
      loading: false,
      error: null,
      results: [],
      searched: false,
      subdivisionQid: null,
      // MapQA parser state (sync template mode)
      parsedQuery: null,
      executeAnswer: null,
      executeTrace: [],
      classOptions: [
        { value: 'wkgs:Cafe', text: 'Cafe' },
        { value: 'wkgs:Restaurant', text: 'Restaurant' },
        { value: 'wkgs:Hotel', text: 'Hotel' },
        { value: 'wkgs:Hospital', text: 'Hospital' },
        { value: 'wkgs:School', text: 'School' },
        { value: 'wkgs:Shop', text: 'Shop' },
        { value: 'wkgs:Amenity', text: 'Amenity (general)' },
      ],
    }
  },
  computed: {
    isTagsMode() {
      return this.agentMode ? false : this.queryMode === 'tags'
    },
    isNaturalMode() {
      return this.agentMode ? false : this.queryMode === 'natural'
    },
    isTemplateMode() {
      return this.agentMode ? true : this.queryMode === 'template'
    },
    isValid() {
      if (this.isTagsMode) {
        try {
          JSON.parse(this.queryTagsInput)
          return true
        } catch {
          return false
        }
      }
      if (this.isTemplateMode) {
        const q = this.agentMode ? this.store.templateQuery : this.templateQuery
        return q.trim().length > 0
      }
      return this.naturalQuery.trim().length > 0
    },
    isLoading() {
      return this.agentMode ? this.store.loading : this.loading
    },
    displayError() {
      return this.agentMode ? this.store.error : this.error
    },
    displayParsedQuery() {
      return this.agentMode ? this.store.parsedQuery : this.parsedQuery
    },
    displayAnswer() {
      return this.agentMode ? this.store.executeAnswer : this.executeAnswer
    },
    displayTrace() {
      return this.agentMode ? [] : this.executeTrace
    },
    displayResults() {
      return this.agentMode ? this.store.results : this.results
    },
    confidenceBadgeClass() {
      const c = this.displayParsedQuery?.confidence || 0
      if (c >= 0.8) return 'text-bg-success'
      if (c >= 0.6) return 'text-bg-warning'
      return 'text-bg-danger'
    },
    /** In agent mode, the textarea binds to the store's templateQuery. */
    effectiveTemplateQuery: {
      get() {
        return this.agentMode ? this.store.templateQuery : this.templateQuery
      },
      set(val) {
        if (this.agentMode) {
          this.store.setTemplateQuery(val)
        } else {
          this.templateQuery = val
        }
      },
    },
  },
  watch: {
    countryName() {
      this.reset()
    },
    // In agent mode, emit search-results when the store's results change
    // (streaming 'done' event populates store.results).
    'store.results'(newResults) {
      if (this.agentMode) {
        this.$emit('search-results', newResults)
      }
    },
  },
  unmounted() {
    if (this.agentMode) {
      this.stream.cleanup()
    }
  },
  methods: {
    reset() {
      if (this.agentMode) {
        this.store.reset()
        this.stream.closeStream()
        return
      }
      this.queryMode = 'tags'
      this.queryTagsInput = '{"amenity": "cafe"}'
      this.naturalQuery = ''
      this.templateQuery = ''
      this.lat = ''
      this.lon = ''
      this.rdfType = null
      this.topK = 20
      this.loading = false
      this.error = null
      this.results = []
      this.searched = false
      this.subdivisionQid = null
      this.parsedQuery = null
      this.executeAnswer = null
      this.executeTrace = []
    },

    async performSearch() {
      if (this.agentMode) {
        await this.executeStream()
      } else {
        await this.executeSync()
      }
    },

    /** Agent mode: streaming via EventSource composable. */
    async executeStream() {
      const query = this.store.templateQuery?.trim()
      if (!query) {
        this.store.setError('Please enter a geospatial question')
        return
      }
      this.stream.startStream({
        query,
        countryCode: this.countryName,
        snapshotDate: this.snapshotDate,
      })
    },

    /** Human mode: synchronous POST (current behavior, unchanged). */
    async executeSync() {
      this.loading = true
      this.error = null
      this.results = []
      this.searched = false
      this.parsedQuery = null
      this.executeAnswer = null
      this.executeTrace = []

      try {
        if (this.isTemplateMode) {
          // NL Template mode: MapQA parser + executor pipeline
          if (!this.templateQuery.trim()) {
            throw new Error('Please enter a geospatial question')
          }
          const payload = {
            query: this.templateQuery.trim(),
          }
          if (this.countryName) payload.country_code = this.countryName
          if (this.snapshotDate) payload.snapshot_date = this.snapshotDate

          const response = await axios.post('/nca/execute-query/', payload)
          const data = response.data

          // Display parsed query (template + concepts)
          if (data.parsed) {
            this.parsedQuery = data.parsed
          }

          // Display executor results
          if (data.result) {
            this.executeAnswer = data.result.answer || null
            this.executeTrace = data.result.trace || []
            // Results from the executor (entities with lat/lon)
            if (data.result.results && Array.isArray(data.result.results)) {
              this.results = data.result.results.map(r => ({
                osm_type: r.osm_type,
                osm_id: r.osm_id,
                tags: r.tags,
                wkg_class: r.wkg_class,
                geom: (r.lat != null && r.lon != null)
                  ? { lat: r.lat, lon: r.lon }
                  : null,
                scores: { final_score: null },
                distance_m: r.distance_m,
              }))
            }
            if (data.result.error) {
              this.error = data.result.error
            }
          }

          this.searched = true
          this.$emit('search-results', this.results)
        } else {
          // Structured (JSON) and Natural language modes both use triplet search
          const payload = {
            country_code: this.countryName,
            top_k: parseInt(this.topK) || 20,
          }

          if (this.isTagsMode) {
            let queryTags = {}
            try {
              queryTags = JSON.parse(this.queryTagsInput)
            } catch {
              throw new Error('Invalid JSON in query tags')
            }
            payload.query_tags = queryTags
          } else if (this.isNaturalMode) {
            // Natural language name search: romanizer + FastText
            if (!this.naturalQuery.trim()) {
              throw new Error('Please enter a name to search')
            }
            payload.natural_query = this.naturalQuery.trim()
          }

          if (this.lat) payload.lat = parseFloat(this.lat)
          if (this.lon) payload.lon = parseFloat(this.lon)
          if (this.rdfType) payload.rdf_type = this.rdfType
          if (this.subdivisionQid) payload.subdivision_qid = this.subdivisionQid

          if (this.snapshotDate) {
            payload.snapshot_date = this.snapshotDate
          }

          const response = await axios.post('/nca/semantic-triplet-search/', payload)
          this.results = response.data.results || []
          this.searched = true
          this.$emit('search-results', this.results)
        }
      } catch (err) {
        console.error('Semantic search failed:', err)
        this.error = err.response?.data?.error || err.message || 'Search failed'
      } finally {
        this.loading = false
      }
    },

    formatTags(tags) {
      if (!tags) return ''
      const entries = Object.entries(tags).slice(0, 3)
      const str = entries.map(([k, v]) => `${k}=${v}`).join(', ')
      return entries.length < Object.keys(tags).length ? str + '…' : str
    },

    formatTraceValue(val) {
      if (val == null) return ''
      if (typeof val === 'string') return val
      if (typeof val === 'number') return String(val)
      try {
        return JSON.stringify(val)
      } catch {
        return String(val)
      }
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

/* The font-monospace utility uses Bootstrap's --bs-font-monospace; ensure
   the textarea inherits a readable mono stack in dark mode. */
.font-monospace {
  font-family: var(--bs-font-monospace, ui-monospace, SFMono-Regular, Menlo,
    Monaco, Consolas, 'Liberation Mono', 'Courier New', monospace) !important;
}
</style>
