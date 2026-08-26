<template>
  <div class="d-flex flex-column gap-2">
    <form class="d-flex flex-column gap-2" @submit.prevent="performSearch">
      <!-- Query mode toggle -->
      <div class="d-flex flex-column gap-1">
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
      </div>

      <!-- Top K results limit + anchor/entity graph layer toggles -->
      <div class="d-flex flex-column gap-1">
        <div class="d-flex align-items-center gap-2">
          <label class="form-label small text-secondary mb-0">Top K</label>
          <input
            v-model.number="topK"
            type="number"
            min="1"
            max="100"
            class="form-control form-control-sm"
            style="max-width: 90px;"
          />
          <span class="small text-secondary">max results to show</span>
        </div>
        <div v-if="hasQueryGraph" class="d-flex flex-wrap gap-3 align-items-center small">
          <div class="form-check form-switch form-check-inline mb-0">
            <input id="qg-show-anchors" v-model="showAnchors" type="checkbox" class="form-check-input" role="switch" />
            <label class="form-check-label" for="qg-show-anchors">Anchors</label>
          </div>
          <div class="form-check form-switch form-check-inline mb-0">
            <input id="qg-show-entities" v-model="showEntities" type="checkbox" class="form-check-input" role="switch" />
            <label class="form-check-label" for="qg-show-entities">Entities</label>
          </div>
          <div class="form-check form-switch form-check-inline mb-0">
            <input id="qg-show-links" v-model="showLinks" type="checkbox" class="form-check-input" role="switch" />
            <label class="form-check-label" for="qg-show-links">Links</label>
          </div>
        </div>
      </div>

      <button
        type="submit"
        class="btn btn-primary btn-sm"
        :disabled="isLoading || !isValid"
      >
        <span v-if="isLoading" class="spinner-border spinner-border-sm me-1" role="status" aria-hidden="true"></span>
        {{ isLoading ? 'Searching…' : 'Search' }}
      </button>
    </form>

    <!-- Error -->
    <div v-if="displayError" class="alert alert-danger small py-1 px-2 mb-0">
      {{ displayError }}
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

    <!-- Results table -->
    <div v-if="displayResults.length > 0" class="d-flex flex-column gap-1">
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
      queryMode: 'tags',
      queryTagsInput: '{"amenity": "cafe"}',
      naturalQuery: '',
      templateQuery: '',
      lat: '',
      lon: '',
      rdfType: null,
      topK: 20,
      // Query-graph layer visibility (anchors / entities / links)
      showAnchors: true,
      showEntities: true,
      showLinks: true,
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
    displayAnswer() {
      return this.executeAnswer
    },
    displayTrace() {
      return this.executeTrace
    },
    /** Top K clamped to the backend-supported 1–100 range. */
    topKClamped() {
      return Math.min(100, Math.max(1, parseInt(this.topK, 10) || 20))
    },
    /** Normalized + top-k-capped results (table rows and map entities). */
    displayResults() {
      return this.results.map((r) => this.normalizeResult(r)).slice(0, this.topKClamped)
    },
    hasQueryGraph() {
      return this.displayResults.length > 0 || this.extractAnchors().length > 0
    },
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
    // Live controls: re-slice / re-toggle the emitted graph without re-querying.
    topK() {
      this.publishResults()
    },
    showAnchors() {
      this.publishResults()
    },
    showEntities() {
      this.publishResults()
    },
    showLinks() {
      this.publishResults()
    },
  },
  methods: {
    reset() {
      this.showAnchors = true
      this.showEntities = true
      this.showLinks = true
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
      await this.executeSync()
    },

    /** Synchronous POST to the executor / triplet-search endpoints. */
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
            // Results from the executor (entities with lat/lon). Kept in
            // full — the Top K control caps display/markers via the graph.
            if (data.result.results && Array.isArray(data.result.results)) {
              this.results = data.result.results.map((r) => this.normalizeResult(r))
            }
            if (data.result.error) {
              this.error = data.result.error
            }
          }

          this.searched = true
          this.publishResults()
        } else {
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
        }
      } catch (err) {
        console.error('Semantic search failed:', err)
        this.error = err.response?.data?.error || err.message || 'Search failed'
      } finally {
        this.loading = false
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
        name: r.name || '',
        tags: r.tags || {},
        wkg_class: r.wkg_class || null,
        geom: lat != null && lon != null ? { lat: Number(lat), lon: Number(lon) } : null,
        scores: r.scores || { final_score: null },
        distance_m: r.distance_m ?? null,
      }
    },

    /** Anchors = geocoded named locations from the executor trace (geocode steps). */
    extractAnchors() {
      const trace = this.executeTrace
      const seen = new Set()
      const anchors = []
      for (const step of trace || []) {
        if (step?.step !== 'geocode') continue
        const out = step.output
        const lat = out?.lat
        const lon = out?.lon
        if (lat == null || lon == null) continue
        const name = step.input || 'anchor'
        if (seen.has(name)) continue
        seen.add(name)
        anchors.push({ name, lat: Number(lat), lon: Number(lon) })
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
        showAnchors: this.showAnchors,
        showEntities: this.showEntities,
        showLinks: this.showLinks,
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
