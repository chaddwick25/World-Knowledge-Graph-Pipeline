<template>
  <div class="search-panel">
    <form class="search-form" @submit.prevent="performSearch">
      <!-- Query mode toggle -->
      <div class="search-form__row">
        <label class="search-form__label">Query mode</label>
        <div class="search-form__toggle">
          <button
            type="button"
            class="search-form__toggle-btn"
            :class="{ 'search-form__toggle-btn--active': isTagsMode }"
            @click="queryMode = 'tags'"
          >
            Structured (JSON)
          </button>
          <button
            type="button"
            class="search-form__toggle-btn"
            :class="{ 'search-form__toggle-btn--active': isNaturalMode }"
            @click="queryMode = 'natural'"
          >
            Natural language
          </button>
        </div>
      </div>

      <!-- Subdivision selector -->
      <SubdivisionSelector
        :country-name="countryName"
        @subdivision-selected="subdivisionQid = $event"
      />

      <!-- Tags input -->
      <div v-if="isTagsMode" class="search-form__row">
        <label class="search-form__label">OSM Tag</label>
        <textarea
          v-model="queryTagsInput"
          class="search-form__textarea"
          rows="2"
          placeholder='{"amenity": "cafe"}'
        ></textarea>
      </div>

      <!-- Natural language input -->
      <div v-if="isNaturalMode" class="search-form__row">
        <label class="search-form__label">Natural language query</label>
        <textarea
          v-model="naturalQuery"
          class="search-form__textarea"
          rows="2"
          placeholder="Find areas with many cafes and some residential buildings"
        ></textarea>
      </div>

      <!-- Filters -->
      <div class="search-form__filters">
        <div class="search-form__filter">
          <label class="search-form__label">Lat</label>
          <input v-model="lat" type="number" step="any" class="search-form__input" placeholder="Optional" />
        </div>
        <div class="search-form__filter">
          <label class="search-form__label">Lon</label>
          <input v-model="lon" type="number" step="any" class="search-form__input" placeholder="Optional" />
        </div>
        <div class="search-form__filter">
          <label class="search-form__label">Class</label>
          <select v-model="rdfType" class="search-form__input">
            <option :value="null">Any class</option>
            <option v-for="opt in classOptions" :key="opt.value" :value="opt.value">
              {{ opt.text }}
            </option>
          </select>
        </div>
        <div class="search-form__filter">
          <label class="search-form__label">Top K</label>
          <input v-model="topK" type="number" min="1" max="100" class="search-form__input" />
        </div>
      </div>

      <!-- Options -->
      <div class="search-form__options">
        <div class="search-form__filter">
          <label class="search-form__label">Encoder</label>
          <select v-model="encoder" class="search-form__input">
            <option v-for="opt in encoderOptions" :key="opt.value" :value="opt.value">
              {{ opt.text }}
            </option>
          </select>
        </div>
        <label class="search-form__checkbox">
          <input v-model="useLearnedWeights" type="checkbox" />
          <span>Learned weights</span>
        </label>
        <label class="search-form__checkbox">
          <input v-model="useAnn" type="checkbox" />
          <span>ANN (400D)</span>
        </label>
      </div>

      <button
        type="submit"
        class="search-form__submit"
        :disabled="loading || !isValid"
      >
        {{ loading ? 'Searching\u2026' : 'Search' }}
      </button>
    </form>

    <!-- Error -->
    <p v-if="error" class="search-form__error">{{ error }}</p>

    <!-- Parsed query (MapQA parser — natural language mode only) -->
    <div v-if="parsedQuery" class="parsed-query">
      <div class="parsed-query__header">
        <span class="parsed-query__template">{{ parsedQuery.template }}</span>
        <span
          class="parsed-query__confidence"
          :class="confidenceClass"
        >
          {{ (parsedQuery.confidence * 100).toFixed(0) }}% confident
        </span>
      </div>
      <div class="parsed-query__concepts">
        <span
          v-for="(concept, idx) in parsedQuery.concepts"
          :key="idx"
          class="parsed-query__concept"
        >
          <span class="parsed-query__concept-type">{{ concept.type }}</span>
          <span class="parsed-query__concept-text">{{ concept.text || '—' }}</span>
        </span>
      </div>
    </div>

    <!-- Answer summary (MapQA executor) -->
    <div v-if="executeAnswer" class="execute-answer">
      <strong>Answer:</strong> {{ executeAnswer }}
      <div v-if="executeTrace.length > 0" class="execute-answer__trace">
        <details>
          <summary>Execution trace ({{ executeTrace.length }} steps)</summary>
          <ol>
            <li v-for="(step, idx) in executeTrace" :key="idx">
              <strong>{{ step.step }}</strong>
              <span v-if="step.input"> — in: {{ formatTraceValue(step.input) }}</span>
              <span v-if="step.output_count !== undefined"> — count: {{ step.output_count }}</span>
              <span v-if="step.output"> — out: {{ formatTraceValue(step.output) }}</span>
              <span v-if="step.output_km !== undefined"> — {{ step.output_km }} km</span>
              <span v-if="step.output_degrees !== undefined"> — {{ step.output_degrees }}°</span>
              <span v-if="step.error" class="execute-answer__trace-error"> — ERROR: {{ step.error }}</span>
            </li>
          </ol>
        </details>
      </div>
    </div>

    <!-- Results -->
    <div v-if="results.length > 0" class="search-results">
      <h4 class="search-results__title">Results ({{ results.length }})</h4>
      <div class="search-results__scroll">
        <table class="search-results__table">
          <thead>
            <tr>
              <th>OSM ID</th>
              <th>Tags</th>
              <th>Class</th>
              <th>Coords</th>
              <th>Score</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in results" :key="`${item.osm_type}-${item.osm_id}`">
              <td>
                <a
                  :href="`https://www.openstreetmap.org/${item.osm_type}/${item.osm_id}`"
                  target="_blank"
                  class="search-results__link"
                >
                  {{ item.osm_type }}/{{ item.osm_id }}
                </a>
              </td>
              <td><small>{{ formatTags(item.tags) }}</small></td>
              <td>{{ item.wkg_class || '\u2014' }}</td>
              <td>
                <template v-if="item.geom">
                  {{ item.geom.lat.toFixed(3) }}, {{ item.geom.lon.toFixed(3) }}
                </template>
                <span v-else class="search-results__muted">N/A</span>
              </td>
              <td class="search-results__score-cell">
                <strong>{{ item.scores?.final_score?.toFixed(3) || '\u2014' }}</strong>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <div v-else-if="searched" class="search-results__empty">
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
  emits: ['search-results'],
  data() {
    return {
      queryMode: 'tags',
      queryTagsInput: '{"amenity": "cafe"}',
      naturalQuery: '',
      lat: '',
      lon: '',
      rdfType: null,
      topK: 20,
      useLearnedWeights: false,
      useAnn: false,
      encoder: 'fasttext',
      loading: false,
      error: null,
      results: [],
      searched: false,
      subdivisionQid: null,
      // MapQA parser state (natural language mode)
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
      encoderOptions: [
        { value: 'fasttext', text: 'FastText (CPU)' },
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
    isValid() {
      if (this.isTagsMode) {
        try {
          JSON.parse(this.queryTagsInput)
          return true
        } catch {
          return false
        }
      }
      return this.naturalQuery.trim().length > 0
    },
    confidenceClass() {
      const c = this.parsedQuery?.confidence || 0
      if (c >= 0.8) return 'parsed-query__confidence--high'
      if (c >= 0.6) return 'parsed-query__confidence--medium'
      return 'parsed-query__confidence--low'
    },
  },
  watch: {
    countryName() {
      this.reset()
    },
  },
  methods: {
    reset() {
      this.queryMode = 'tags'
      this.queryTagsInput = '{"amenity": "cafe"}'
      this.naturalQuery = ''
      this.lat = ''
      this.lon = ''
      this.rdfType = null
      this.topK = 20
      this.loading = false
      this.error = null
      this.results = []
      this.searched = false
      this.useLearnedWeights = false
      this.useAnn = false
      this.encoder = 'fasttext'
      this.subdivisionQid = null
      this.parsedQuery = null
      this.executeAnswer = null
      this.executeTrace = []
    },

    async performSearch() {
      this.loading = true
      this.error = null
      this.results = []
      this.searched = false
      this.parsedQuery = null
      this.executeAnswer = null
      this.executeTrace = []

      try {
        if (this.isNaturalMode) {
          // Natural language mode: use the MapQA parser + executor pipeline
          if (!this.naturalQuery.trim()) {
            throw new Error('Please enter a natural language query')
          }
          const payload = {
            query: this.naturalQuery.trim(),
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
          // Structured (JSON) mode: use the existing triplet search
          const payload = {
            country_code: this.countryName,
            top_k: parseInt(this.topK) || 20,
            use_learned_weights: this.useLearnedWeights,
            encoder: this.encoder,
          }

          if (this.useAnn) {
            payload.use_ann = true
          }

          let queryTags = {}
          try {
            queryTags = JSON.parse(this.queryTagsInput)
          } catch {
            throw new Error('Invalid JSON in query tags')
          }
          payload.query_tags = queryTags

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
.search-panel {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}

.search-form {
  display: flex;
  flex-direction: column;
  gap: 0.6rem;
}

.search-form__row {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
}

.search-form__label {
  font-size: 0.75rem;
  color: #9ca3af;
}

.search-form__toggle {
  display: flex;
  gap: 0.3rem;
}

.search-form__toggle-btn {
  flex: 1;
  padding: 0.25rem 0.4rem;
  border-radius: 999px;
  border: 1px solid #374151;
  background: transparent;
  color: #e5e7eb;
  font-size: 0.75rem;
  cursor: pointer;
}

.search-form__toggle-btn--active {
  border-color: #4f46e5;
  background: #111827;
}

.search-form__textarea {
  width: 100%;
  padding: 0.4rem 0.5rem;
  border-radius: 0.4rem;
  border: 1px solid #1f2937;
  background: #020617;
  color: #e5e7eb;
  font-size: 0.8rem;
  resize: vertical;
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, 'Liberation Mono', 'Courier New', monospace;
}

.search-form__filters {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 0.4rem;
}

.search-form__filter {
  display: flex;
  flex-direction: column;
  gap: 0.15rem;
}

.search-form__input {
  width: 100%;
  padding: 0.35rem 0.5rem;
  border-radius: 0.4rem;
  border: 1px solid #1f2937;
  background: #020617;
  color: #e5e7eb;
  font-size: 0.8rem;
}

.search-form__options {
  display: flex;
  gap: 0.75rem;
  flex-wrap: wrap;
}

.search-form__checkbox {
  display: flex;
  align-items: center;
  gap: 0.3rem;
  font-size: 0.75rem;
  color: #e5e7eb;
}

.search-form__submit {
  align-self: flex-start;
  padding: 0.4rem 1rem;
  border-radius: 0.5rem;
  border: none;
  background: #4f46e5;
  color: #f9fafb;
  font-size: 0.85rem;
  cursor: pointer;
}

.search-form__submit[disabled] {
  opacity: 0.5;
  cursor: default;
}

.search-form__error {
  color: #fecaca;
  font-size: 0.8rem;
  margin: 0;
}

/* ── Results ── */

.search-results {
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
}

.search-results__title {
  margin: 0;
  font-size: 0.85rem;
  font-weight: 600;
}

.search-results__scroll {
  max-height: 220px;
  overflow-y: auto;
  border-radius: 0.4rem;
  border: 1px solid #1f2937;
}

.search-results__table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.72rem;
}

.search-results__table th,
.search-results__table td {
  padding: 0.25rem 0.35rem;
  border-bottom: 1px solid #111827;
}

.search-results__table th {
  text-align: left;
  color: #9ca3af;
  position: sticky;
  top: 0;
  background: #020617;
}

.search-results__link {
  color: #60a5fa;
  text-decoration: none;
}

.search-results__link:hover {
  text-decoration: underline;
}

.search-results__score-cell {
  text-align: right;
}

.search-results__muted {
  color: #6b7280;
}

.search-results__empty {
  color: #9ca3af;
  font-size: 0.8rem;
}

/* ── Parsed query (MapQA parser) ── */

.parsed-query {
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
  padding: 0.5rem 0.6rem;
  border-radius: 0.4rem;
  border: 1px solid #1f2937;
  background: #020617;
}

.parsed-query__header {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.parsed-query__template {
  font-size: 0.78rem;
  font-weight: 600;
  color: #a5b4fc;
}

.parsed-query__confidence {
  font-size: 0.68rem;
  padding: 0.1rem 0.4rem;
  border-radius: 999px;
}

.parsed-query__confidence--high {
  background: #064e3b;
  color: #6ee7b7;
}

.parsed-query__confidence--medium {
  background: #78350f;
  color: #fcd34d;
}

.parsed-query__confidence--low {
  background: #7f1d1d;
  color: #fca5a5;
}

.parsed-query__concepts {
  display: flex;
  flex-wrap: wrap;
  gap: 0.3rem;
}

.parsed-query__concept {
  display: inline-flex;
  align-items: center;
  gap: 0.25rem;
  font-size: 0.72rem;
}

.parsed-query__concept-type {
  padding: 0.1rem 0.3rem;
  border-radius: 0.25rem;
  background: #1e293b;
  color: #93c5fd;
  font-weight: 600;
}

.parsed-query__concept-text {
  color: #d1d5db;
}

/* ── Answer summary (MapQA executor) ── */

.execute-answer {
  padding: 0.5rem 0.6rem;
  border-radius: 0.4rem;
  border: 1px solid #064e3b;
  background: #022c22;
  font-size: 0.8rem;
  color: #d1fae5;
}

.execute-answer strong {
  color: #6ee7b7;
}

.execute-answer__trace {
  margin-top: 0.4rem;
  font-size: 0.72rem;
  color: #9ca3af;
}

.execute-answer__trace summary {
  cursor: pointer;
  color: #6b7280;
}

.execute-answer__trace ol {
  margin: 0.3rem 0 0 1rem;
  padding-left: 0.5rem;
}

.execute-answer__trace li {
  margin-bottom: 0.15rem;
}

.execute-answer__trace-error {
  color: #fca5a5;
}
</style>
