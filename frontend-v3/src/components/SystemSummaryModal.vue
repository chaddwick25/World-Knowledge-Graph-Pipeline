<template>
  <Teleport to="body">
    <div v-if="open" class="summary-backdrop" @click.self="$emit('close')">
      <div class="summary-modal">
        <!-- ── Header ── -->
        <header class="summary-modal__header">
          <h2 class="summary-modal__title">System Summary</h2>
          <div class="summary-modal__header-right">
            <button class="summary-modal__close-btn" @click="$emit('close')">&times;</button>
          </div>
        </header>

        <!-- ── Loading / Error ── -->
        <div v-if="loading" class="summary-modal__loading">
          <div class="summary-modal__spinner"></div>
          <p>Loading system summary...</p>
        </div>
        <div v-else-if="error" class="summary-modal__error">
          {{ error }}
          <button class="summary-modal__retry-btn" @click="fetchSummary">Retry</button>
        </div>

        <!-- ── Tabs ── -->
        <template v-else-if="data">
          <div class="summary-modal__tabs">
            <button
              class="summary-modal__tab"
              :class="{ 'summary-modal__tab--active': activeTab === 'overview' }"
              @click="activeTab = 'overview'"
            >Overview</button>
            <button
              class="summary-modal__tab"
              :class="{ 'summary-modal__tab--active': activeTab === 'embeddings' }"
              @click="activeTab = 'embeddings'"
            >Data</button>
            <button
              class="summary-modal__tab"
              :class="{ 'summary-modal__tab--active': activeTab === 'storage' }"
              @click="activeTab = 'storage'"
            >Storage</button>
            <button
              class="summary-modal__tab"
              :class="{ 'summary-modal__tab--active': activeTab === 'paths' }"
              @click="activeTab = 'paths'"
            >Paths</button>
            <button
              class="summary-modal__tab"
              :class="{ 'summary-modal__tab--active': activeTab === 'history' }"
              @click="activeTab = 'history'"
            >History</button>
          </div>

          <div class="summary-modal__body">
            <!-- ── Overview Tab ── -->
            <div v-if="activeTab === 'overview'" class="summary-panel">
              <div class="summary-cards">
                <div class="summary-card">
                  <span class="summary-card__label">Planet PBF</span>
                  <span class="summary-card__value">{{ planetSizeDisplay }}</span>
                  <span class="summary-card__sub">{{ data.planet.available ? 'Available' : 'Missing' }}</span>
                </div>
                <div class="summary-card">
                  <span class="summary-card__label">Countries</span>
                  <span class="summary-card__value">{{ countriesWithEmbeddings }}/{{ totalCountries }}</span>
                  <span class="summary-card__sub">With embeddings</span>
                </div>
                <div class="summary-card">
                  <span class="summary-card__label">Preprocessing</span>
                  <span class="summary-card__value">{{ preprocessingCompleted }}/{{ preprocessingTotal }}</span>
                  <span class="summary-card__sub">Steps completed</span>
                </div>
                <div class="summary-card">
                  <span class="summary-card__label">Pipelines</span>
                  <span class="summary-card__value">{{ totalPipelines }}</span>
                  <span class="summary-card__sub">{{ data.pipeline_runs?.failed || 0 }} failed</span>
                </div>
              </div>

              <!-- Preprocessing steps detail -->
              <div class="summary-section">
                <h3 class="summary-section__title">Preprocessing Steps</h3>
                <div class="summary-steps">
                  <div
                    v-for="(step, name) in data.preprocessing_steps"
                    :key="name"
                    class="summary-step"
                    :class="`summary-step--${step.status}`"
                  >
                    <span
                      class="summary-step__icon"
                      :class="{
                        'summary-step__icon--ok': step.status === 'completed',
                        'summary-step__icon--pending': step.status === 'not_run',
                      }"
                    >
                      {{ step.status === 'completed' ? '✓' : '○' }}
                    </span>
                    <div class="summary-step__body">
                      <span class="summary-step__name">{{ name }}</span>
                      <span class="summary-step__desc">{{ step.description }}</span>
                    </div>
                    <span v-if="step.countries != null" class="summary-step__count">{{ step.countries }} countries</span>
                    <span v-else-if="step.count != null" class="summary-step__count">{{ step.count }} items</span>
                  </div>
                </div>
              </div>
            </div>

            <!-- ── Embeddings Tab ── -->
            <div v-if="activeTab === 'embeddings'" class="summary-panel">
              <div class="summary-section">
                <h3 class="summary-section__title">How Places Are Represented</h3>
                <p class="summary-section__description">
                  Every place on the map gets two independent embeddings.
                  <a href="https://github.com/NicolasTe/GeoVectors/blob/master/Encoder.py" target="_blank" rel="noopener noreferrer">GV-Tags</a>
                  (300D, semantic: "what something is") is produced by FastText encoding
                  of OSM tags during Step 1.
                  <a href="https://github.com/NicolasTe/GeoVectors/blob/master/Encoder.py" target="_blank" rel="noopener noreferrer">GV-NLE</a>
                  (100D, spatial: "where something is") is produced by weighted DeepWalk
                  on the k-NN graph of the 50 nearest geographic neighbors during Step 5.
                  Reference the
                  <a href="https://geovectors.l3s.uni-hannover.de/data" target="_blank" rel="noopener noreferrer">Embeddings</a>
                  dataset.
                </p>
                <h4 class="summary-section__subhead">GV-Tags: FastText encoder (300D)</h4>
                <p class="summary-section__description">
                  OSM tags are split into tokens and each token is looked up in a
                  pretrained FastText word-vector model. The embedding is the L2-normalized
                  mean of the token vectors, so entities with similar tags land close
                  together. This is word-vector lookup plus mean aggregation, not a learned
                  encode, and runs at about 1s per 20K batch.
                </p>
                <h4 class="summary-section__subhead">GV-NLE: Weighted DeepWalk (100D)</h4>
                <p class="summary-section__description">
                  Step 5 builds a k-NN graph over the country's entities using haversine
                  distance, limited to the 50 nearest neighbors per entity. Edges are
                  weighted by log-inverse distance
                  (<code>w = max(1/ln(max(d_km, 1.1)), e)</code>, floored at e (about 2.72)
                  so the graph stays connected). Weighted DeepWalk random walks train a
                  100D embedding per entity, saved as a
                  <code>wdw.pickle</code> mapping each entity to its vector; the BallTree
                  used to find neighbors is rebuilt in-memory each time and never
                  persisted. New snapshot entities get a provisional GV-NLE from their 50
                  nearest trained entities through the same in-memory BallTree and
                  log-inverse distance weighting. Full DeepWalk retraining remains the
                  authoritative path and should run periodically: the provisional vectors
                  interpolate the trained manifold, they do not replace it.
                </p>
                <div class="summary-availability-banner">
                  <span class="summary-availability-banner__count">{{ countriesWithEmbeddings }}/{{ totalCountries }}</span>
                  <span class="summary-availability-banner__label">countries available</span>
                </div>
                <table class="summary-table">
                  <thead>
                    <tr>
                      <th>Continent</th>
                      <th>Countries</th>
                      <th>With Embeddings</th>
                      <th>Pipelines Ran</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="c in data.embeddings.by_continent" :key="c.name">
                      <td class="summary-table__continent">{{ c.name }}</td>
                      <td class="num">{{ c.total }}</td>
                      <td>
                        <span
                          class="summary-bar"
                          :class="{ 'summary-bar--full': c.with_embeddings === c.total }"
                        >
                          <span
                            class="summary-bar__fill"
                            :style="{ width: (c.total > 0 ? (c.with_embeddings / c.total) * 100 : 0) + '%' }"
                          ></span>
                          <span class="summary-bar__label num">{{ c.with_embeddings }}/{{ c.total }}</span>
                        </span>
                      </td>
                      <td class="num">{{ c.pipelines_completed }}</td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </div>

            <!-- ── Storage Tab ── -->
            <div v-if="activeTab === 'storage'" class="summary-panel">
              <div class="summary-section">
                <h3 class="summary-section__title">Hot Storage (NVME/SSD) - Runtime Tasks, Stream Processing Inputs </h3> 
                <div class="summary-storage">
                  <div class="summary-storage__path">
                    <code>{{ hotStorage.path }}</code>
                  </div>
                  <div class="summary-storage__stats">
                    <span><span class="num">{{ hotStorage.file_count || 0 }}</span> files</span>
                    <span><span class="num">{{ hotStorage.size_gb || 0 }}</span> GB</span>
                  </div>
                  <p class="summary-storage__contents">{{ hotStorage.contents }}</p>
                </div>
              </div>
              <div class="summary-section">
                <h3 class="summary-section__title">Cold Storage (HDD - Archive) - Preprocessing Tasks, Data for Batch Processing.</h3>
                <div class="summary-storage">
                  <div class="summary-storage__path">
                    <code>{{ coldStorage.path }}</code>
                  </div>
                  <div class="summary-storage__stats">
                    <span><span class="num">{{ coldStorage.file_count || 0 }}</span> files</span>
                    <span><span class="num">{{ coldStorage.size_gb || 0 }}</span> GB</span>
                  </div>
                  <p class="summary-storage__contents">{{ coldStorage.contents }}</p>
                </div>
              </div>
            </div>

            <!-- ── Paths Tab ── -->
            <div v-if="activeTab === 'paths'" class="summary-panel">
              <div class="summary-section">
                <h3 class="summary-section__title">Configured Paths</h3>
                <table class="summary-table">
                  <thead>
                    <tr>
                      <th>Key</th>
                      <th>Exists</th>
                      <th>Details</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="p in formattedPaths" :key="p.key">
                      <td class="summary-table__key">
                        <code>{{ p.key }}</code>
                      </td>
                      <td>
                        <span
                          class="summary-dot"
                          :class="p.exists ? 'summary-dot--ok' : 'summary-dot--missing'"
                        ></span>
                      </td>
                      <td class="summary-table__detail">
                        <span v-if="p.size_display">{{ p.size_display }}</span>
                        <span v-else-if="p.file_count">{{ p.file_count }} files</span>
                        <span v-else-if="p.is_executable">executable</span>
                        <span v-else-if="!p.exists">Not found</span>
                        <span v-else class="summary-table__path">{{ p.path }}</span>
                      </td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </div>

            <!-- ── History Tab ── -->
            <div v-if="activeTab === 'history'" class="summary-panel">
              <div class="summary-section">
                <h3 class="summary-section__title">Pipeline Run History</h3>
                <p class="summary-section__subtitle">
                  {{ data.pipeline_runs.completed }} completed, {{ data.pipeline_runs.failed }} failed
                </p>
                <div v-if="data.pipeline_runs.runs.length > 0" class="summary-table__scroll">
                  <table class="summary-table">
                    <thead>
                      <tr>
                        <th>
                          <button type="button" class="summary-table__sort" @click="toggleHistorySort('country_code')">
                            Country
                            <i v-if="historySortKey === 'country_code'"
                               :class="historySortDir === 'asc' ? 'bi bi-caret-up-fill' : 'bi bi-caret-down-fill'"></i>
                          </button>
                        </th>
                        <th>
                          <button type="button" class="summary-table__sort" @click="toggleHistorySort('snapshot')">
                            Snapshot
                            <i v-if="historySortKey === 'snapshot'"
                               :class="historySortDir === 'asc' ? 'bi bi-caret-up-fill' : 'bi bi-caret-down-fill'"></i>
                          </button>
                        </th>
                        <th>
                          <button type="button" class="summary-table__sort" @click="toggleHistorySort('status')">
                            Status
                            <i v-if="historySortKey === 'status'"
                               :class="historySortDir === 'asc' ? 'bi bi-caret-up-fill' : 'bi bi-caret-down-fill'"></i>
                          </button>
                        </th>
                        <th>
                          <button type="button" class="summary-table__sort" @click="toggleHistorySort('completed_at')">
                            Completed
                            <i v-if="historySortKey === 'completed_at'"
                               :class="historySortDir === 'asc' ? 'bi bi-caret-up-fill' : 'bi bi-caret-down-fill'"></i>
                          </button>
                        </th>
                      </tr>
                    </thead>
                    <tbody>
                    <tr v-for="run in sortedRuns" :key="run.country_code + run.completed_at">
                      <td>
                        <span class="summary-table__country">{{ run.country_name || run.country_code }}</span>
                        <code class="summary-table__iso">{{ run.country_code }}</code>
                      </td>
                      <td class="data-mono">{{ run.snapshot }}</td>
                      <td>
                        <span
                          class="summary-badge"
                          :class="{
                            'summary-badge--ok': run.status === 'COMPLETED' || run.status === 'SUCCESS',
                            'summary-badge--fail': run.status === 'FAILED',
                          }"
                        >
                          {{ run.status }}
                        </span>
                      </td>
                      <td class="summary-table__date">{{ run.completed_at ? new Date(run.completed_at).toLocaleDateString() : '-' }}</td>
                    </tr>
                    </tbody>
                  </table>
                </div>
                <p v-else class="summary-empty">No pipeline runs yet.</p>
              </div>
            </div>
          </div>
        </template>
      </div>
    </div>
  </Teleport>
</template>

<script>
import axios from 'axios'
// Modal styles live in a dedicated stylesheet (monolith split, Phase 5).
// The .summary-* classes are namespaced to this modal, so global CSS is safe.

export default {
  name: 'SystemSummaryModal',
  props: {
    open: { type: Boolean, default: false },
  },
  emits: ['close'],
  data() {
    return {
      data: null,
      loading: false,
      error: '',
      activeTab: 'overview',
      // History tab column sort — defaults to newest-first (the backend
      // order). Nulls always sort last regardless of direction.
      historySortKey: 'completed_at',
      historySortDir: 'desc',
    }
  },
  computed: {
    planetSizeDisplay() {
      if (!this.data?.planet?.size_gb) return 'N/A'
      return `${this.data.planet.size_gb} GB`
    },
    countriesWithEmbeddings() {
      if (!this.data?.embeddings) return 0
      return this.data.embeddings.with_embeddings
    },
    totalCountries() {
      return this.data?.embeddings?.total_countries || 0
    },
    totalPipelines() {
      return this.data?.pipeline_runs?.completed || 0
    },
    /** History runs sorted by the active column (nulls last). */
    sortedRuns() {
      const runs = [...(this.data?.pipeline_runs?.runs || [])]
      const key = this.historySortKey
      const dir = this.historySortDir === 'asc' ? 1 : -1
      runs.sort((a, b) => {
        const va = a?.[key]
        const vb = b?.[key]
        if (va == null && vb == null) return 0
        if (va == null) return 1
        if (vb == null) return -1
        if (key === 'completed_at') {
          return (new Date(va) - new Date(vb)) * dir
        }
        return String(va).localeCompare(String(vb)) * dir
      })
      return runs
    },
    preprocessingCompleted() {
      if (!this.data?.preprocessing_steps) return 0
      const steps = Object.values(this.data.preprocessing_steps)
      return steps.filter((s) => s.status === 'completed').length
    },
    preprocessingTotal() {
      return this.data?.preprocessing_steps ? Object.keys(this.data.preprocessing_steps).length : 0
    },
    hotStorage() {
      return this.data?.storage?.hot || {}
    },
    coldStorage() {
      return this.data?.storage?.cold || {}
    },
    formattedPaths() {
      if (!this.data?.paths) return []
      return this.data.paths.map((p) => {
        const formatted = { ...p }
        if (p.size_mb != null) {
          if (p.size_mb >= 1000) {
            formatted.size_display = `${(p.size_mb / 1024).toFixed(1)} GB`
          } else {
            formatted.size_display = `${p.size_mb} MB`
          }
        }
        return formatted
      })
    },
  },
  watch: {
    open(isOpen) {
      if (isOpen && !this.data) this.fetchSummary()
    },
  },
  mounted() {
    if (this.open) this.fetchSummary()
  },
  methods: {
    /** Toggle the History column sort: same column flips direction,
     *  a new column starts asc (completed_at starts desc — newest first). */
    toggleHistorySort(key) {
      if (this.historySortKey === key) {
        this.historySortDir = this.historySortDir === 'asc' ? 'desc' : 'asc'
      } else {
        this.historySortKey = key
        this.historySortDir = key === 'completed_at' ? 'desc' : 'asc'
      }
    },

    async fetchSummary() {
      this.loading = true
      this.error = ''
      try {
        const { data: d } = await axios.get('/system/summary/')
        this.data = d
      } catch (err) {
        this.error = err.response?.data?.error || err.message || 'Failed to load summary'
      } finally {
        this.loading = false
      }
    },
  },
}
</script>

<style scoped src="../styles/system-summary-modal.css"></style>

