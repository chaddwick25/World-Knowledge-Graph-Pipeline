<template>
  <div class="d-flex flex-column gap-2">
    <form class="d-flex flex-column gap-2" @submit.prevent="performSearch">
      <!-- Query mode radio group (was pill buttons) -->
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
          <label class="btn btn-outline-secondary" for="query-mode-tags">Structured (JSON)</label>

          <input
            type="radio"
            class="btn-check"
            name="query-mode"
            id="query-mode-natural"
            autocomplete="off"
            value="natural"
            v-model="queryMode"
          >
          <label class="btn btn-outline-secondary" for="query-mode-natural">Natural language</label>
        </div>
      </div>

      <!-- Subdivision selector -->
      <SubdivisionSelector
        :country-name="countryName"
        @subdivision-selected="subdivisionQid = $event"
      />

      <!-- Tags input (Structured JSON mode) -->
      <div v-if="isTagsMode" class="d-flex flex-column gap-1">
        <label class="form-label small text-secondary mb-0">OSM Tag</label>
        <textarea
          v-model="queryTagsInput"
          class="form-control form-control-sm font-monospace"
          rows="2"
          placeholder='{"amenity": "cafe"}  or  {"name": "파리바게뜨"}'
        ></textarea>
      </div>

      <!-- Natural language name search input -->
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

    <!-- Error -->
    <div v-if="displayError" class="alert alert-danger small py-1 px-2 mb-0">
      {{ displayError }}
    </div>

    <!-- Data answer (deterministic factor-join — arrives first, stays pinned) -->
    <div v-if="executeAnswer" class="alert alert-info small py-1 px-2 mb-1">
      <div class="text-secondary small mb-1">
        Retrieval time: <span class="text-danger">{{ dataAnswerElapsed != null ? dataAnswerElapsed.toFixed(1) : '—' }}s</span>
      </div>
      <strong>Answer from DB:</strong> {{ executeAnswer }}
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
      <strong>AI answer:</strong> {{ enrichedAnswer }}
    </div>

    <!-- Execution trace — decision flow (each step shows what it decided).
         Colors follow the AI answer palette: green (success) for steps on
         the answer path, red (danger) for warnings/errors. The caret is
         red while closed, gray once fully open. -->
    <div v-if="traceFlow.length > 0" class="mt-1 small text-secondary">
      <details class="trace-details">
        <summary class="cursor-pointer">Execution trace ({{ traceFlow.length  + 1 }} steps)</summary>
        <div class="trace-flow mt-1">
          <!-- WK Template headline: first node in the trace. Template name
               + confidence badge. "WK Template" links to the ACL paper. -->
          <div v-if="displayParsedQuery" class="trace-node">
            <div class="d-flex align-items-baseline gap-2">
              <span class="trace-icon bg-primary-subtle" aria-hidden="true">
                <i class="bi bi-file-earmark-text text-primary"></i>
              </span>
              <a
                href="https://aclanthology.org/2026.acl-long.679.pdf"
                target="_blank"
                rel="noopener"
                class="trace-link fw-semibold"
              >WK Template</a>
              <span class="text-secondary">: {{ displayParsedQuery.template }}</span>
              <span
                class="badge rounded-pill"
                :class="confidenceBadgeClass"
              >
                {{ (displayParsedQuery.confidence * 100).toFixed(0) }}% confident
              </span>
            </div>
          </div>
          <div
            v-for="(node, idx) in traceFlow"
            :key="idx"
            :class="['trace-node', node.status ? `trace-node--${node.status}` : '']"
          >
            <div class="d-flex align-items-baseline gap-2">
              <span
                :class="['trace-icon', node.iconBg, node.live ? 'trace-icon--live' : '']"
                aria-hidden="true"
              >
                <i :class="['bi', node.icon, node.iconColor]"></i>
              </span>
              <strong :class="node.color ? `text-${node.color}` : ''">{{ node.title }}</strong>
              <!-- Status protocol: red = error, amber = warning/degraded -->
              <span
                v-if="node.status === 'error'"
                class="trace-status trace-status--error"
                title="error"
              >
                <i class="bi bi-x-octagon-fill"></i>
              </span>
              <span
                v-else-if="node.status === 'warning'"
                class="trace-status trace-status--warning"
                title="warning"
              >
                <i class="bi bi-exclamation-triangle-fill"></i>
              </span>
              <span v-if="node.error" class="text-danger">— {{ node.error }}</span>
            </div>
            <ul v-if="node.lines.length" class="trace-lines">
              <li v-for="(line, li) in node.lines" :key="li">
                <!-- Segmented line: links (entity, WorldKG class, OSM tag)
                     are green, underlined on hover, open in a new tab. -->
                <template v-if="Array.isArray(line?.segments)">
                  <template v-for="(seg, si) in line.segments" :key="si">
                    <template v-if="si > 0">{{ line.sep || ' · ' }}</template>
                    <a
                      v-if="seg && seg.href"
                      :href="seg.href"
                      target="_blank"
                      rel="noopener"
                      class="trace-link"
                    >{{ seg.text }}</a>
                    <template v-else>{{ seg }}</template>
                  </template>
                </template>
                <template v-else>{{ line }}</template>
              </li>
            </ul>
          </div>
        </div>
      </details>
    </div>

    <!-- Results table -->
    <div v-if="displayResults.length > 0" class="d-flex flex-column gap-1">
      <div class="d-flex align-items-center justify-content-between">
        <h6 class="small fw-semibold mb-0">Results ({{ displayResults.length }})</h6>
        <button
          class="btn btn-link btn-sm text-secondary p-0"
          @click="showScores = !showScores"
        >
          {{ showScores ? 'Hide' : 'Show' }} scores
        </button>
      </div>
      <div class="table-responsive" style="max-height: 480px; overflow-y: auto;">
        <table class="table table-sm table-borderless mb-0" style="font-size: 0.72rem;">
          <thead class="table-dark">
            <tr>
              <th>Name</th>
              <th>Tags</th>
              <th>Class</th>
              <template v-if="showScores">
                <th v-for="col in activeScoreColumns" :key="col.key" class="text-end">{{ col.label }}</th>
              </template>
              <th class="text-end">Final</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in displayResults" :key="`${item.osm_type}-${item.osm_id}`">
              <td>
                <a
                  :href="`https://www.openstreetmap.org/${item.osm_type}/${item.osm_id}`"
                  target="_blank"
                  class="results-name-link"
                  style="color: var(--bs-info-text-emphasis);"
                >
                  {{ item.name || '—' }}
                </a>
              </td>
              <td><small>{{ formatTags(item.tags) }}</small></td>
              <td>{{ (item.wkg_class || '—').replace(/^wkgs:/, '') }}</td>
              <template v-if="showScores">
                <td v-for="col in activeScoreColumns" :key="col.key" class="text-end">{{ item.scores?.[col.key]?.toFixed(3) || '—' }}</td>
              </template>
              <td class="text-end">
                <strong>{{ item.scores?.final_score?.toFixed(3) || '—' }}</strong>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <div v-else-if="searched" class="small text-secondary">
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
 *
 * Emits:
 *   search-results  - Array of result objects (for map markers)
 */

import axios from 'axios'
import SubdivisionSelector from './SubdivisionSelector.vue'

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
  },
  emits: ['search-results', 'query-graph'],
  data() {
    return {
      // ── Query state (component-local) ──
      queryMode: 'template',
      queryTagsInput: '{"amenity": "cafe"}',
      naturalQuery: '',
      templateQuery: 'Which cafes are within 2km of a school?',
      lat: '',
      lon: '',
      rdfType: null,
      topK: 20,
      loading: false,
      error: null,
      results: [],
      searched: false,
      showScores: false,
      subdivisionQid: null,
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
    }
  },
  computed: {
    isTagsMode() {
      return this.queryMode === 'tags'
    },
    isNaturalMode() {
      return this.queryMode === 'natural'
    },
    isTemplateMode() {
      return this.queryMode === 'template'
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
        return this.templateQuery.trim().length > 0
      }
      return this.naturalQuery.trim().length > 0
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
    displayTrace() {
      return this.executeTrace
    },
    /** Trace steps rendered as a decision-flow (Layout B): each node shows
     *  the step, its inputs, and what it decided. */
    traceFlow() {
      return (this.executeTrace || []).map((s) => this._decorateStep(s))
    },
    /** Top K clamped to the backend-supported 1–100 range. */
    topKClamped() {
      return Math.min(100, Math.max(1, parseInt(this.topK, 10) || 20))
    },
    /** Normalized + top-k-capped results (table rows and map entities). */
    displayResults() {
      return this.results.map((r) => this.normalizeResult(r)).slice(0, this.topKClamped)
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
    // Live control: re-slice the emitted graph without re-querying.
    topK() {
      this.publishResults()
    },
  },
  // Lifecycle balance — close any open SSE stream (rules §1.2).
  unmounted() {
    this.eventSource?.close()
    this.eventSource = null
  },
  methods: {
    reset() {
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
      this.eventSource?.close()
      this.eventSource = null
      this.enrichedAnswer = ''
      this.enrichingContext = null
    },

    async performSearch() {
      await this.executeSync()
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
      this.clearAiAnswerTimer()
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
        this.openTemplateStream()
        return  // loading is cleared by the stream's done/error events
      }

      try {
        // Structured (JSON) and Natural language modes both use triplet search
        const payload = {
          country_code: this.countryName,
          top_k: this.topKClamped,
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
        this.results = (response.data.results || []).map((r) => this.normalizeResult(r))
        this.searched = true
        this.publishResults()
      } catch (err) {
        console.error('Semantic search failed:', err)
        this.error = err.response?.data?.error || err.message || 'Search failed'
      } finally {
        this.loading = false
      }
    },

    // ── SSE stream (template mode — direct path) ───────────────────────

    /**
     * Open the streaming executor: GET /api/nca/execute-query/stream/.
     * EventSource is GET-only, so the URL is built from
     * axios.defaults.baseURL (http://localhost:8000/api from main.js) —
     * a relative /nca/... path would hit Vite with no proxy.
     */
    openTemplateStream() {
      const params = new URLSearchParams({ query: this.templateQuery.trim() })
      if (this.countryName) params.set('country_code', this.countryName)
      if (this.snapshotDate) params.set('snapshot_date', this.snapshotDate)

      this.queryStartAt = Date.now()
      const base = axios.defaults.baseURL || ''
      const url = `${base}/nca/execute-query/stream/?${params}`
      // TEMP diagnosis signals — remove after the answer-first timing is confirmed.
      console.log('[SSE] opening stream', url, '@', Date.now())
      const es = new EventSource(url)
      this.eventSource = es

      es.addEventListener('parsed', (e) => {
        this.parsedQuery = JSON.parse(e.data).parsed || null
        console.log('[SSE] parsed @', Date.now())
      })

      es.addEventListener('executed', (e) => {
        // {template, result_count, trace} — render the trace/anchors early.
        const d = JSON.parse(e.data)
        this.executeTrace = d.trace || []
        console.log('[SSE] executed @', Date.now(), 'template:', d.template, 'count:', d.result_count)
      })

      es.addEventListener('answer', (e) => {
        // Deterministic factor-join answer (non-AI) — shown immediately;
        // the LLM enrichment (answer_delta) replaces it when ready.
        const d = JSON.parse(e.data)
        console.log('[SSE] answer @', Date.now(), 'len:', (d.answer || '').length, 'text:', (d.answer || '').slice(0, 60))
        if (d.answer) {
          this.executeAnswer = d.answer
          this.dataAnswerAt = Date.now()
          this.dataAnswerElapsed = (Date.now() - this.queryStartAt) / 1000
          this.aiAnswerAt = null
        }
      })

      es.addEventListener('context', (e) => {
        // Deterministic entity context is in — show an enriching indicator.
        this.enrichingContext = JSON.parse(e.data).context || null
        console.log('[SSE] context @', Date.now())
      })

      es.addEventListener('answer_delta', (e) => {
        const delta = JSON.parse(e.data).delta
        if (delta) this.enrichedAnswer += delta
        this.ensureAiAnswerVisible()
        console.log('[SSE] answer_delta @', Date.now(), 'deltaLen:', (delta || '').length, 'total:', this.enrichedAnswer.length)
      })

      es.addEventListener('done', (e) => {
        const d = JSON.parse(e.data)
        const result = d.result || {}
        console.log('[SSE] done @', Date.now(), 'enriched:', !!result.enrichment, 'answerLen:', (result.answer || '').length)
        // Data answer stays pinned; the AI answer settles to the final
        // enriched text when present.
        if (result.enrichment?.enriched_answer) {
          this.enrichedAnswer = result.enrichment.enriched_answer
        } else if (!this.executeAnswer) {
          this.executeAnswer = result.answer || null
        }
        this.ensureAiAnswerVisible()
        // Final AI elapsed — the moment the enriched answer was complete.
        this.aiAnswerElapsed = (Date.now() - this.queryStartAt) / 1000
        this.executeTrace = result.trace || this.executeTrace
        if (Array.isArray(result.results)) {
          // Full result set — the Top K control caps display/markers.
          this.results = result.results.map((r) => this.normalizeResult(r))
        }
        if (result.error) this.error = result.error
        this.searched = true
        this.loading = false
        this.publishResults()
        this.closeTemplateStream()
      })

      es.addEventListener('error', () => {
        // Fires on connection failure OR when the server closes the stream.
        // After a clean `done` this is a no-op; otherwise surface an error.
        console.log('[SSE] error @', Date.now(), 'readyState:', es.readyState, 'searched:', this.searched)
        this.clearAiAnswerTimer()
        if (es.readyState === EventSource.CLOSED && !this.searched) {
          this.error = this.error || 'Stream error'
          this.loading = false
        }
        this.closeTemplateStream()
      })
    },

    closeTemplateStream() {
      // NOTE: do not clear the AI-reveal timer here — done() closes the
      // stream, but the buffered AI answer must still appear after the
      // data answer's solo window (the timer fires ~900ms later).
      this.eventSource?.close()
      this.eventSource = null
    },

    /** Reveal the AI answer region once the data answer has had its
     * minimum solo display window; tokens buffer in the meantime.
     *
     * This controls ONLY when the AI box becomes visible — it must never
     * touch aiAnswerElapsed. That value is set once, at the `done` event
     * (the true enrichment completion time). Stamping it here made the
     * displayed AI latency equal to dataAnswer + 900ms whenever the
     * enrichment finished inside the solo window — the "always 0.9s
     * behind" artifact. */
    ensureAiAnswerVisible() {
      if (this.aiAnswerAt) {
        console.log('[SSE] ensureAi: already visible @', Date.now())
        return
      }
      const elapsed = this.dataAnswerAt
        ? Date.now() - this.dataAnswerAt
        : Number.POSITIVE_INFINITY
      console.log('[SSE] ensureAi @', Date.now(), 'elapsed:', elapsed, 'min:', this.minDataAnswerMs, 'enrichedLen:', this.enrichedAnswer.length)
      if (elapsed >= this.minDataAnswerMs) {
        this.aiAnswerAt = Date.now()
        return
      }
      this.clearAiAnswerTimer()
      this.aiAnswerTimer = setTimeout(() => {
        this.aiAnswerAt = Date.now()
        console.log('[SSE] ensureAi: timer fired @', Date.now())
      }, this.minDataAnswerMs - elapsed)
    },

    clearAiAnswerTimer() {
      if (this.aiAnswerTimer) {
        clearTimeout(this.aiAnswerTimer)
        this.aiAnswerTimer = null
      }
    },

    // ── Anchor/entity query graph ─────────────────────────────────────

    /** Normalize backend result shapes (executor: top-level lat/lon; triplet: geom). */
    normalizeResult(r) {
      const lat = r.geom?.lat ?? r.lat
      const lon = r.geom?.lon ?? r.lon
      return {
        osm_type: r.osm_type,
        osm_id: r.osm_id,
        name: r.name || r.tags?.name || r.tags?.['name:en'] || '',
        tags: r.tags || {},
        wkg_class: r.wkg_class || null,
        geom: lat != null && lon != null ? { lat: Number(lat), lon: Number(lon) } : null,
        scores: r.scores || { final_score: null },
        distance_m: r.distance_m ?? null,
      }
    },

    /** Anchors = geocoded named locations from the executor trace.
     *  Handles every geocoding step shape: 'geocode' / 'geocode_anchor'
     *  (single output) and 'batch_geocode' (paired inputs/outputs). */
    extractAnchors() {
      const trace = this.executeTrace
      const seen = new Set()
      const anchors = []
      const pushAnchor = (name, out) => {
        if (!out) return
        const lat = out.lat
        const lon = out.lon
        if (lat == null || lon == null) return
        const key = name || 'anchor'
        if (seen.has(key)) return
        seen.add(key)
        anchors.push({ name: key, lat: Number(lat), lon: Number(lon) })
      }
      for (const step of trace || []) {
        if (step?.step === 'geocode' || step?.step === 'geocode_anchor') {
          pushAnchor(step.input, step.output)
        } else if (step?.step === 'batch_geocode') {
          const inputs = Array.isArray(step.inputs) ? step.inputs : []
          const outputs = Array.isArray(step.outputs) ? step.outputs : []
          outputs.forEach((out, i) => pushAnchor(inputs[i] || `anchor ${i + 1}`, out))
        }
      }
      return anchors
    },

    /** Haversine distance in meters (nearest-anchor link assignment). */
    _haversineM(a, b) {
      const R = 6371000
      const toRad = (d) => (d * Math.PI) / 180
      const dLat = toRad(b.lat - a.lat)
      const dLon = toRad(b.lon - a.lon)
      const s =
        Math.sin(dLat / 2) ** 2 +
        Math.cos(toRad(a.lat)) * Math.cos(toRad(b.lat)) * Math.sin(dLon / 2) ** 2
      return 2 * R * Math.asin(Math.sqrt(s))
    },

    /** Index of the anchor nearest to an entity (links entities to their anchor set). */
    nearestAnchorIndex(entity, anchors) {
      let best = 0
      let bestDist = Infinity
      anchors.forEach((a, i) => {
        const d = this._haversineM(entity.geom, a)
        if (d < bestDist) {
          bestDist = d
          best = i
        }
      })
      return best
    },

    /** Distance/bearing lines between paired geocoded anchors
     *  (OBJECT-FIELD-MEASURE, bearing 5a): batch_geocode steps with two
     *  coordinate-bearing outputs; the label comes from the haversine step
     *  when present. */
    extractAnchorLines() {
      const trace = this.executeTrace
      let distLabel = null
      for (const step of trace || []) {
        if (step?.step === 'haversine' && step.output_km != null) {
          distLabel = `${step.output_km} km`
        }
      }
      const lines = []
      for (const step of trace || []) {
        if (step?.step !== 'batch_geocode') continue
        const outputs = Array.isArray(step.outputs) ? step.outputs : []
        const inputs = Array.isArray(step.inputs) ? step.inputs : []
        const pts = outputs
          .filter((o) => o && o.lat != null && o.lon != null)
          .slice(0, 2)
        if (pts.length === 2) {
          lines.push({
            from: { lat: Number(pts[0].lat), lon: Number(pts[0].lon) },
            to: { lat: Number(pts[1].lat), lon: Number(pts[1].lon) },
            label: distLabel || `${inputs[0] || 'A'} → ${inputs[1] || 'B'}`,
          })
        }
      }
      return lines
    },

    /** Build the anchor/entity graph payload consumed by WorldKGMap. */
    buildQueryGraph(entities) {
      const anchors = this.extractAnchors()
      const links = anchors.length
        ? entities
            .filter((e) => e.geom)
            .map((e) => ({
              anchorIdx: this.nearestAnchorIndex(e, anchors),
              entityIdx: entities.indexOf(e),
            }))
        : []
      return {
        anchors,
        entities,
        links,
        anchorLines: this.extractAnchorLines(),
        topK: this.topKClamped,
      }
    },

    /** Emit normalized, top-k-capped entities + the anchor/entity graph. */
    publishResults() {
      const entities = this.displayResults
      // Map markers (+ backward-compat circle fallback) — only entities with coords.
      this.$emit('search-results', entities.filter((e) => e.geom))
      this.$emit('query-graph', this.buildQueryGraph(entities))
    },

    formatTags(tags) {
      if (!tags) return ''
      const entries = Object.entries(tags).slice(0, 3)
      const str = entries.map(([k, v]) => `${k}=${v}`).join(', ')
      return entries.length < Object.keys(tags).length ? str + '…' : str
    },

    /** Meters → human ("1.2 km" / "450 m"); '' when null. */
    _fmtM(m) {
      if (m == null) return ''
      return m >= 1000 ? `${(m / 1000).toFixed(1)} km` : `${Math.round(m)} m`
    },

    /** openstreetmap.org link for an OSM entity; null when no id. */
    _osmUrl(e) {
      if (!e || e.osm_id == null) return null
      return `https://www.openstreetmap.org/${e.osm_type || 'node'}/${e.osm_id}`
    },

    /** OSM Wiki page for a tag value (Tag:key=value); null when empty. */
    _tagSeg(key, value) {
      if (!key || value == null || value === '') return null
      const href = `https://wiki.openstreetmap.org/wiki/Tag:${key}=${encodeURIComponent(value)}`
      return { text: `${key}=${value}`, href }
    },

    /** Link to a tag VALUE only ("pub" → Tag:amenity=pub), for use after a
     *  plain "amenity" label; null when empty. */
    _tagValueSeg(key, value) {
      if (!key || value == null || value === '') return null
      const href = `https://wiki.openstreetmap.org/wiki/Tag:${key}=${encodeURIComponent(value)}`
      return { text: String(value), href }
    },

    /** WorldKG depth-1 classes whose UpperCamelCase name is the OSM key
     *  (matches the ontology's KEY_CLASS_MAP). */
    _WKGS_KEY_CLASSES() {
      return new Set([
        'Amenity', 'Natural', 'Building', 'Highway', 'Railway', 'Leisure',
        'Shop', 'Tourism', 'Historic', 'Waterway', 'Landuse', 'Place',
        'Aeroway', 'Emergency', 'Healthcare', 'Man_made', 'Power',
        'Public_transport',
      ])
    },

    /** Wiki link for a WorldKG class: wkgs:Place → OSM Wiki Key:place
     *  (the OSM key the class is derived from); unknown classes fall back
     *  to the WorldKG class URI. Returns a segment or null. */
    _classSeg(wkgClass) {
      if (!wkgClass || !wkgClass.startsWith('wkgs:')) return null
      const cls = wkgClass.slice(5)
      const key = cls.charAt(0).toLowerCase() + cls.slice(1)
      const href = this._WKGS_KEY_CLASSES().has(cls)
        ? `https://wiki.openstreetmap.org/wiki/Key:${key}`
        : `http://www.worldkg.org/schema/${cls}`
      return { text: wkgClass, href }
    },

    /** Trace line for an OSM entity as segments: the entity NAME, the
     *  WorldKG class, and any place tag each get the green wiki link.
     *  Pass { linked: false } for failed steps (no id/coords) — the
     *  entity renders as a plain diagnostic, never a success link. */
    _entityLine(e, { linked = true } = {}) {
      const segs = []
      const name = e.name || e.tags?.name || 'unnamed'
      const href = linked ? this._osmUrl(e) : null
      segs.push(href ? { text: name, href } : name)
      if (e.osm_id != null) segs.push(`(${e.osm_type || '?'}/${e.osm_id})`)
      if (e.lat != null && e.lon != null) {
        segs.push(`${Number(e.lat).toFixed(4)}, ${Number(e.lon).toFixed(4)}`)
      }
      if (linked && e.wkg_class) {
        const cls = this._classSeg(e.wkg_class)
        if (cls) segs.push(cls)
      }
      if (linked && e.tags?.place) {
        const tag = this._tagSeg('place', e.tags.place)
        if (tag) segs.push(tag)
      }
      return { segments: segs }
    },

    /** Decorate one trace step into a flow node: icon + title + insight lines.
     *  Every step type keeps its real fields — nothing is dropped, the
     *  layout just decides what reads as the one-line insight.
     *
     *  Colors: the heading matches the primary-answer text color ("Found N
     *  entities within …", rendered with the info emphasis color); each
     *  icon is colored by its nature (green = resolution, amber = decision,
     *  blue = ordering/search, cyan = spatial/diffusion, gray = context).
     *  Warnings/errors flip heading + icon to red. */
    _decorateStep(step) {
      const st = step?.step || 'step'
      const node = {
        icon: 'bi-arrow-right',
        title: st.replace(/_/g, ' '),
        lines: [],
        error: null,
        color: 'info-emphasis',
        iconColor: 'text-secondary',
        iconBg: 'bg-secondary-subtle',
        live: false,
        status: null, // 'error' | 'warning' | null — status badge + accent bar
      }

      const isEntity = (v) =>
        v && typeof v === 'object' && v.osm_id != null && v.tags

      switch (st) {
        case 'geocode':
        case 'geocode_anchor': {
          // Anchor resolved → green; entity is a link to OSM. On failure
          // (no usable id/coords) the step shows the error and the entity
          // renders as a plain diagnostic, never a success link.
          node.icon = 'bi-geo-alt'
          node.iconColor = 'text-success'
          node.title = `Geocode${step.input ? `: ${step.input}` : ''}`
          if (step.error) node.error = step.error
          if (isEntity(step.output)) {
            node.lines.push(this._entityLine(step.output, { linked: !step.error }))
          }
          break
        }
        case 'batch_geocode': {
          // Multiple anchors resolved in one step → green links.
          node.icon = 'bi-pin-map'
          node.iconColor = 'text-success'
          node.title = step.inputs?.length
            ? `Geocode: ${step.inputs.join(', ')}`
            : 'Batch geocode'
          if (step.error) node.error = step.error
          if (Array.isArray(step.outputs)) {
            step.outputs.forEach((out, i) => {
              if (isEntity(out)) {
                node.lines.push(this._entityLine(out, { linked: !step.error }))
              } else if (step.inputs?.[i]) {
                node.lines.push(step.inputs[i])
              }
            })
          }
          break
        }
        case 'haversine': {
          // Distance computed between two points → cyan.
          node.icon = 'bi-rulers'
          node.iconColor = 'text-info'
          node.title = 'Distance (haversine)'
          if (step.output_km != null) {
            node.lines.push(
              `${step.output_km} km${step.output_m != null ? ` (${step.output_m} m)` : ''}`
            )
          }
          break
        }
        case 'cone_search': {
          // Directional cone filter → amber compass; amenity links to wiki.
          node.icon = 'bi-compass'
          node.iconColor = 'text-warning'
          node.title = step.direction
            ? `Cone search: ${step.direction}`
            : 'Cone search'
          const amenitySeg = this._tagValueSeg('amenity', step.amenity)
          if (amenitySeg) {
            node.lines.push({ segments: ['amenity', amenitySeg] })
          }
          if (step.radius_m != null) node.lines.push(`radius: ${step.radius_m} m`)
          if (step.output_count != null) node.lines.push(`candidates: ${step.output_count}`)
          break
        }
        case 'compare_closer': {
          // Comparison decision → amber; each candidate's name is a green
          // OSM link (resolved entity id comes from the executor; legacy
          // string candidates fall back to a name join with the results).
          node.icon = 'bi-arrows-angle-contract'
          node.iconColor = 'text-warning'
          node.title = step.anchor
            ? `Which is closer to ${step.anchor}?`
            : 'Compared candidates'
          if (Array.isArray(step.candidates) && step.candidates.length) {
            const byName = new Map()
            for (const r of this.displayResults) {
              const key = String(r.name || '').toLowerCase()
              if (key) byName.set(key, r)
            }
            const rows = step.candidates.map((c) => {
              if (typeof c === 'string') {
                const hit = byName.get(String(c).toLowerCase())
                return { text: c, d: hit?.distance_m ?? null, entity: hit }
              }
              return {
                text: c.query || c.name || String(c.osm_id ?? ''),
                d: c.distance_m ?? null,
                entity: c,
              }
            })
            const minD = Math.min(...rows.map((r) => (r.d == null ? Infinity : r.d)))
            for (const r of rows) {
              const segs = []
              if (r.entity && r.entity.osm_id != null) {
                segs.push({ text: r.text, href: this._osmUrl(r.entity) })
              } else {
                segs.push(r.text)
              }
              if (r.d != null) {
                segs.push(`— ${this._fmtM(r.d)}${r.d === minD ? ' ✓ closest' : ''}`)
              }
              node.lines.push({ segments: segs, sep: ' ' })
            }
          } else if (step.output_count != null) {
            node.title = `Compared ${step.output_count} candidates`
          }
          break
        }
        case 'entity_context': {
          // Supporting context → cyan with a live pulse so the node reads
          // as "context being assembled for the answer".
          node.icon = 'bi-diagram-3'
          node.iconColor = 'text-info'
          node.live = true
          node.title = 'Entity context'
          const parts = []
          if (step.uslp_links != null) parts.push(`uslp links ${step.uslp_links}`)
          if (step.communities != null) parts.push(`communities ${step.communities}`)
          if (step.class_distribution != null) parts.push(`classes ${step.class_distribution}`)
          if (parts.length) node.lines.push(parts.join(' · '))
          if (Array.isArray(step.sources) && step.sources.length) {
            node.lines.push(`sources: ${step.sources.join(', ')}`)
          }
          break
        }
        case 'default_radius':
        case 'radius_guard': {
          // Spatial bound / filter → cyan
          node.icon = 'bi-broadcast'
          node.iconColor = 'text-info'
          node.title = `Radius ${step.radius_m ?? '?'} m`
          if (step.reason) node.lines.push(step.reason)
          if (step.before != null && step.after != null) {
            node.lines.push(`${step.before} → ${step.after} entities after radius guard`)
          }
          break
        }
        case 'rank_by_distance': {
          // Ordering → blue
          node.icon = 'bi-sort-numeric-down'
          node.iconColor = 'text-primary'
          node.title = 'Ranked by distance'
          if (step.anchor) node.lines.push(`anchor: ${step.anchor}`)
          if (step.top) node.lines.push(`closest: ${step.top}`)
          break
        }
        case 'heat_kernel': {
          // Diffusion → cyan; amenity value links to its OSM wiki tag page.
          node.icon = 'bi-thermometer-half'
          node.iconColor = 'text-info'
          node.title = 'Diffusion (heat kernel)'
          const segs = []
          const amenitySeg = this._tagValueSeg('amenity', step.amenity)
          if (amenitySeg) segs.push('amenity', amenitySeg)
          if (step.t != null) segs.push(`t=${step.t}`)
          if (step.total_amenity_nodes != null) segs.push(`${step.total_amenity_nodes} nodes`)
          if (segs.length) node.lines.push({ segments: segs })
          break
        }
        case 'multi_anchor_resolve':
        case 'multi_anchor_search':
        case 'multi_anchor_anchors':
        case 'multi_anchor_empty': {
          // Multiple anchors → blue; category (an amenity value) links to wiki.
          node.icon = 'bi-pin-angle'
          node.iconColor = 'text-primary'
          node.title = st.replace(/_/g, ' ')
          if (step.input) node.lines.push(step.input)
          const catSeg = this._tagValueSeg('amenity', step.anchor_category)
          if (catSeg) {
            node.lines.push({ segments: ['category', catSeg] })
          }
          const resSeg = this._tagValueSeg('amenity', step.resolved_amenity)
          if (resSeg) {
            node.lines.push({ segments: ['resolved', resSeg] })
          }
          if (step.anchor_count != null) node.lines.push(`anchors: ${step.anchor_count}`)
          if (step.radius_m != null) node.lines.push(`radius: ${step.radius_m} m`)
          if (step.warning) node.lines.push(step.warning)
          break
        }
        case 'augmented_enrichment': {
          // Enrichment added → green
          node.icon = 'bi-arrow-up-right'
          node.iconColor = 'text-success'
          node.title = 'Enrichment'
          if (step.output_count != null) node.lines.push(`enriched ${step.output_count} entities`)
          break
        }
        case 'place_search': {
          node.icon = 'bi-search'
          node.iconColor = 'text-primary'
          node.title = 'Place search'
          if (step.error) node.error = step.error
          if (step.warning) node.lines.push(step.warning)
          break
        }
        case 'factor_join': {
          // Factor-table retrieval — the heart of the answer. Surface the
          // table, subgraph scope, candidate/result counts and any note
          // (e.g. lenient eigenbasis fallback) instead of a bare label.
          node.icon = 'bi-table'
          node.iconColor = 'text-primary'
          node.title = step.op
            ? `Factor join: ${step.op.replace(/_/g, ' ')}`
            : 'Factor join'
          if (step.table) node.lines.push(`table: ${step.table}`)
          if (step.subgraph_slug) node.lines.push(`subgraph: ${step.subgraph_slug}`)
          if (step.anchor_osm_id != null) node.lines.push(`anchor: ${step.anchor_osm_id}`)
          if (step.candidates != null) node.lines.push(`candidates: ${step.candidates}`)
          if (step.output_count != null) node.lines.push(`results: ${step.output_count}`)
          if (step.note) node.lines.push(`note: ${step.note}`)
          if (step.detail) node.lines.push(step.detail)
          if (step.cross_subgraph_transport) {
            const t = step.cross_subgraph_transport
            if (t.transport_matrices_used != null) {
              node.lines.push(`cross-subgraph transport: ${t.transport_matrices_used} matrices`)
            }
          }
          if (step.error) node.error = step.error
          if (step.warning) node.lines.push(step.warning)
          break
        }
        case 'spatial_filter_skipped': {
          // Skipped → amber, flips red via the warning rule
          node.icon = 'bi-exclamation-triangle'
          node.iconColor = 'text-warning'
          node.title = st.replace(/_/g, ' ')
          if (step.error) node.error = step.error
          if (step.warning) node.lines.push(step.warning)
          break
        }
        default: {
          // Generic step: surface every scalar field as key: value — the
          // default keeps future step types visible instead of invisible.
          node.iconColor = 'text-secondary'
          const skip = new Set(['step', 'input', 'output'])
          for (const [k, v] of Object.entries(step)) {
            if (skip.has(k)) continue
            if (typeof v === 'string' || typeof v === 'number') {
              node.lines.push(`${k}: ${v}`)
            }
          }
          if (isEntity(step.output)) node.lines.push(this._entityLine(step.output))
        }
      }
      // Status protocol — errors → red badge/bar; warnings and degraded
      // states (NULL, unverified, lenient, pre-migration, fallback) →
      // amber badge/bar. The badge catches the eye; the heading keeps its
      // "Found…" color and the icon keeps its nature color.
      const attentionText = [step.warning, step.note, step.detail]
        .filter(Boolean)
        .join(' ')
        .toLowerCase()
      const degraded = /unverified|lenient|null|pre-migration|fallback|unavailable|degraded|skipped/.test(
        attentionText
      )
      if (node.error || step.error) {
        // step.error must count even when a case didn't copy it to
        // node.error (e.g. a failed geocode) — otherwise the error is
        // silently invisible to the status protocol.
        node.status = 'error'
        node.color = 'danger'
        node.iconColor = 'text-danger'
        node.live = false
        node.error = node.error || step.error
      } else if (step.warning || degraded) {
        node.status = 'warning'
        if (step.warning) node.iconColor = 'text-warning'
        node.live = false
      }
      // Icon chip background follows the icon color (subtle tint).
      node.iconBg = {
        'text-success': 'bg-success-subtle',
        'text-danger': 'bg-danger-subtle',
        'text-warning': 'bg-warning-subtle',
        'text-primary': 'bg-primary-subtle',
        'text-info': 'bg-info-subtle',
      }[node.iconColor] || 'bg-secondary-subtle'
      return node
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

/* Result-table entity name links: underline on hover only (the plain
   Bootstrap link default is always-underlined; its text-decoration-none
   utility carries !important and would beat a hover rule, hence the
   dedicated class). */
.results-name-link {
  text-decoration: none;
}
.results-name-link:hover {
  text-decoration: underline;
}

/* Execution trace decision flow: vertical spine with dashed separators
   between nodes so each step reads as one decision in the chain. When
   open, the spine matches the AI answer box's green border. */
.trace-flow {
  border-left: 2px solid var(--bs-secondary-border-subtle);
  padding-left: 0.75rem;
}
.trace-details[open] .trace-flow {
  border-left-color: var(--bs-success-border-subtle);
}

/* Trace caret: the native disclosure triangle, red while closed
   (attention), gray once fully open. */
.trace-details > summary::marker {
  color: var(--bs-danger);
}
.trace-details > summary::-webkit-details-marker {
  color: var(--bs-danger);
}
.trace-details[open] > summary::marker {
  color: var(--bs-secondary);
}
.trace-details[open] > summary::-webkit-details-marker {
  color: var(--bs-secondary);
}
.trace-node + .trace-node {
  margin-top: 0.5rem;
  padding-top: 0.5rem;
  border-top: 1px dashed var(--bs-secondary-border-subtle);
}

/* Status protocol: red = error, amber = warning/degraded. A left accent
   bar + a badge icon flag the node without recoloring the whole line. */
.trace-node {
  padding-left: 0.4rem;
}
.trace-node--error {
  border-left: 3px solid var(--bs-danger);
}
.trace-node--warning {
  border-left: 3px solid var(--bs-warning);
}
.trace-status {
  display: inline-flex;
  align-items: center;
  line-height: 1;
}
.trace-status i {
  font-size: 0.85rem;
}
.trace-status--error {
  color: var(--bs-danger);
}
.trace-status--warning {
  color: var(--bs-warning);
}
.trace-lines {
  margin: 0.15rem 0 0;
  padding-left: 1.35rem;
  list-style: none;
}

/* Icon chip: subtle tinted background so each node's icon has presence. */
.trace-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 1.35rem;
  height: 1.35rem;
  border-radius: 0.3rem;
  flex-shrink: 0;
}
.trace-icon i {
  font-size: 0.8rem;
  line-height: 1;
}

/* Live pulse for context-assembly nodes (entity_context). */
@keyframes trace-pulse {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0.55;
  }
}
.trace-icon--live {
  animation: trace-pulse 2.2s ease-in-out infinite;
}

/* Entity link: green, underline on hover, opens in a new tab. */
.trace-link {
  color: var(--bs-success);
  text-decoration: none;
}
.trace-link:hover {
  color: var(--bs-success);
  text-decoration: underline;
}

/* The font-monospace utility uses Bootstrap's --bs-font-monospace; ensure
   the textarea inherits a readable mono stack in dark mode. */
.font-monospace {
  font-family: var(--bs-font-monospace, ui-monospace, SFMono-Regular, Menlo,
    Monaco, Consolas, 'Liberation Mono', 'Courier New', monospace) !important;
}
</style>
