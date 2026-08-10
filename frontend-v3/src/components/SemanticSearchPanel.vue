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
    },

    async performSearch() {
      this.loading = true
      this.error = null
      this.results = []
      this.searched = false

      try {
        const payload = {
          country_code: this.countryName,
          top_k: parseInt(this.topK) || 20,
          use_learned_weights: this.useLearnedWeights,
          encoder: this.encoder,
        }

        if (this.useAnn) {
          payload.use_ann = true
        }

        if (this.isNaturalMode) {
          if (!this.naturalQuery.trim()) {
            throw new Error('Please enter a natural language query')
          }
          payload.natural_query = this.naturalQuery.trim()
        } else {
          let queryTags = {}
          try {
            queryTags = JSON.parse(this.queryTagsInput)
          } catch {
            throw new Error('Invalid JSON in query tags')
          }
          payload.query_tags = queryTags
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
  },
}
</script>

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
        <label class="search-form__label">Query tags (JSON)</label>
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
</style>
