<script>
/**
 * SemanticSearchModal
 *
 * Modal for querying OSM entities via semantic triplet search.
 * Becomes available once a country's pipeline completes.
 * Supports both structured (JSON tags) and natural language queries.
 */

import axios from 'axios'

export default {
  name: 'SemanticSearchModal',
  props: {
    modelValue: {
      type: Boolean,
      default: false,
    },
    countryName: {
      type: String,
      required: true,
    },
  },
  emits: ['update:modelValue', 'search-results'],
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
      loading: false,
      error: null,
      results: [],
      searched: false,
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
    open: {
      get() {
        return this.modelValue
      },
      set(val) {
        this.$emit('update:modelValue', val)
      },
    },
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
    modelValue(val) {
      if (val) this.reset()
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

    close() {
      this.open = false
    },
  },
}
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="modal-backdrop" @click.self="close">
      <div class="modal">
        <!-- ── Header ── -->
        <header class="modal__header">
          <h3 class="modal__title">Semantic Search — {{ countryName }}</h3>
          <button class="modal__close" type="button" @click="close">&times;</button>
        </header>

        <!-- ── Body ── -->
        <section class="modal__body">
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

            <!-- Tags input -->
            <div v-if="isTagsMode" class="search-form__row">
              <label class="search-form__label">Query tags (JSON)</label>
              <textarea
                v-model="queryTagsInput"
                class="search-form__textarea"
                rows="3"
                placeholder='{"amenity": "cafe"}'
              ></textarea>
            </div>

            <!-- Natural language input -->
            <div v-if="isNaturalMode" class="search-form__row">
              <label class="search-form__label">Natural language query</label>
              <textarea
                v-model="naturalQuery"
                class="search-form__textarea"
                rows="3"
                placeholder="Find areas with many cafes and some residential buildings"
              ></textarea>
            </div>

            <!-- Filters -->
            <div class="search-form__filters">
              <div class="search-form__filter">
                <label class="search-form__label">Latitude</label>
                <input v-model="lat" type="number" step="any" class="search-form__input" placeholder="Optional" />
              </div>
              <div class="search-form__filter">
                <label class="search-form__label">Longitude</label>
                <input v-model="lon" type="number" step="any" class="search-form__input" placeholder="Optional" />
              </div>
              <div class="search-form__filter">
                <label class="search-form__label">WorldKG class</label>
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
              <label class="search-form__checkbox">
                <input v-model="useLearnedWeights" type="checkbox" />
                <span>Use learned projection weights</span>
              </label>
              <label class="search-form__checkbox">
                <input v-model="useAnn" type="checkbox" />
                <span>Use ANN (400D static embedding)</span>
              </label>
            </div>

            <button
              type="submit"
              class="search-form__submit"
              :disabled="loading || !isValid"
            >
              {{ loading ? 'Searching…' : 'Search' }}
            </button>
          </form>

          <!-- Error -->
          <p v-if="error" class="search-form__error">{{ error }}</p>

          <!-- Results -->
          <div v-if="results.length > 0" class="search-results">
            <h4 class="search-results__title">Results ({{ results.length }})</h4>

            <table class="search-results__table">
              <thead>
                <tr>
                  <th>OSM ID</th>
                  <th>Tags</th>
                  <th>WorldKG Class</th>
                  <th>Coordinates</th>
                  <th>Scores</th>
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
                  <td>{{ item.wkg_class || '—' }}</td>
                  <td>
                    <span v-if="item.geom">
                      {{ item.geom.lat.toFixed(4) }}, {{ item.geom.lon.toFixed(4) }}
                    </span>
                    <span v-else class="search-results__muted">N/A</span>
                  </td>
                  <td>
                    <div class="search-results__scores">
                      <div>Name: {{ item.scores?.name_score?.toFixed(3) || '—' }}</div>
                      <div>Geo: {{ item.scores?.geo_score?.toFixed(3) || '—' }}</div>
                      <div>Class: {{ item.scores?.class_score?.toFixed(3) || '—' }}</div>
                      <strong>Total: {{ item.scores?.final_score?.toFixed(3) || '—' }}</strong>
                    </div>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>

          <div v-else-if="searched" class="search-results__empty">
            No results found for this query.
          </div>
        </section>

        <!-- ── Footer ── -->
        <footer class="modal__footer">
          <button type="button" class="modal__btn" @click="close">Close</button>
        </footer>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
/* ── Overlay ── */
.modal-backdrop {
  position: fixed;
  inset: 0;
  background: rgba(15, 23, 42, 0.7);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 9999;
}

/* ── Modal card ── */
.modal {
  background: #020617;
  border-radius: 0.9rem;
  border: 1px solid #1f2937;
  color: #e5e7eb;
  box-shadow: 0 25px 70px rgba(15, 23, 42, 0.8);
  max-height: 90vh;
  display: flex;
  flex-direction: column;
  width: min(1000px, 95vw);
}

.modal__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0.75rem 1rem;
  border-bottom: 1px solid #111827;
}

.modal__title {
  margin: 0;
  font-size: 1rem;
  font-weight: 600;
}

.modal__close {
  border: none;
  background: transparent;
  color: #9ca3af;
  font-size: 1.2rem;
  cursor: pointer;
}

.modal__body {
  padding: 1rem;
  overflow-y: auto;
  flex: 1;
}

.modal__footer {
  border-top: 1px solid #111827;
  padding: 0.75rem 1rem;
  display: flex;
  justify-content: flex-end;
}

.modal__btn {
  padding: 0.4rem 0.9rem;
  border-radius: 0.6rem;
  border: 1px solid #4b5563;
  background: #020617;
  color: #e5e7eb;
  font-size: 0.85rem;
  cursor: pointer;
}

/* ── Search form ── */
.search-form {
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
}

.search-form__row {
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
}

.search-form__label {
  font-size: 0.8rem;
  color: #9ca3af;
}

.search-form__toggle {
  display: flex;
  gap: 0.35rem;
}

.search-form__toggle-btn {
  flex: 1;
  padding: 0.3rem 0.5rem;
  border-radius: 999px;
  border: 1px solid #374151;
  background: transparent;
  color: #e5e7eb;
  font-size: 0.8rem;
  cursor: pointer;
}

.search-form__toggle-btn--active {
  border-color: #4f46e5;
  background: #111827;
}

.search-form__textarea {
  width: 100%;
  padding: 0.45rem 0.6rem;
  border-radius: 0.5rem;
  border: 1px solid #1f2937;
  background: #020617;
  color: #e5e7eb;
  font-size: 0.85rem;
  resize: vertical;
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, 'Liberation Mono', 'Courier New', monospace;
}

.search-form__filters {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 0.5rem;
}

@media (max-width: 700px) {
  .search-form__filters {
    grid-template-columns: repeat(2, 1fr);
  }
}

.search-form__filter {
  display: flex;
  flex-direction: column;
  gap: 0.2rem;
}

.search-form__input {
  width: 100%;
  padding: 0.4rem 0.6rem;
  border-radius: 0.5rem;
  border: 1px solid #1f2937;
  background: #020617;
  color: #e5e7eb;
  font-size: 0.85rem;
}

.search-form__options {
  display: flex;
  gap: 1rem;
  flex-wrap: wrap;
}

.search-form__checkbox {
  display: flex;
  align-items: center;
  gap: 0.35rem;
  font-size: 0.8rem;
  color: #e5e7eb;
}

.search-form__submit {
  align-self: flex-start;
  padding: 0.45rem 1.1rem;
  border-radius: 0.6rem;
  border: none;
  background: #4f46e5;
  color: #f9fafb;
  font-size: 0.9rem;
  cursor: pointer;
}

.search-form__submit[disabled] {
  opacity: 0.6;
  cursor: default;
}

.search-form__error {
  color: #fecaca;
  font-size: 0.85rem;
}

/* ── Results ── */
.search-results {
  margin-top: 0.75rem;
}

.search-results__title {
  margin: 0 0 0.5rem;
  font-size: 0.95rem;
}

.search-results__table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.8rem;
}

.search-results__table th,
.search-results__table td {
  padding: 0.35rem 0.5rem;
  border-bottom: 1px solid #111827;
}

.search-results__table th {
  text-align: left;
  color: #9ca3af;
}

.search-results__link {
  color: #60a5fa;
  text-decoration: none;
}

.search-results__link:hover {
  text-decoration: underline;
}

.search-results__scores {
  display: flex;
  flex-direction: column;
  gap: 0.1rem;
}

.search-results__muted {
  color: #6b7280;
}

.search-results__empty {
  margin-top: 0.75rem;
  color: #9ca3af;
  font-size: 0.85rem;
}
</style>
