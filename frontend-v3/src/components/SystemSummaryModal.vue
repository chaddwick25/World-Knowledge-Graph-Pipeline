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

<style scoped>
/* ── Backdrop ── */
.summary-backdrop {
  position: fixed;
  inset: 0;
  z-index: 1000;
  background: rgba(0, 0, 0, 0.65);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 1rem;
}

/* ── Modal ── */
.summary-modal {
  width: 100%;
  max-width: 820px;
  max-height: 85vh;
  display: flex;
  flex-direction: column;
  background: var(--bs-emphasis-bg);
  border: 1px solid var(--bs-border-color);
  border-radius: 1rem;
  overflow: hidden;
  box-shadow: 0 20px 60px rgba(0, 0, 0, 0.5);
}

.summary-modal__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 1rem 1.25rem;
  border-bottom: 1px solid var(--bs-emphasis-bg);
  flex-shrink: 0;
}

.summary-modal__title {
  margin: 0;
  font-size: 1.1rem;
  font-weight: 700;
  color: var(--bs-heading-color);
}

.summary-modal__header-right {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}



.summary-modal__close-btn {
  border: none;
  background: transparent;
  color: var(--bs-meta-color);
  font-size: 1.5rem;
  cursor: pointer;
  line-height: 1;
  padding: 0 0.25rem;
}

.summary-modal__close-btn:hover {
  color: var(--bs-heading-color);
}

/* ── Loading / Error ── */
.summary-modal__loading {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 0.75rem;
  padding: 3rem 1rem;
  color: var(--bs-meta-color);
}

.summary-modal__spinner {
  width: 28px;
  height: 28px;
  border-radius: 999px;
  border: 3px solid var(--bs-info-bg-subtle);
  border-top-color: var(--bs-info-text-emphasis);
  animation: spin 0.8s linear infinite;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

.summary-modal__error {
  padding: 1.5rem;
  color: #fecaca;
  text-align: center;
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
  align-items: center;
}

.summary-modal__retry-btn {
  padding: 0.3rem 0.75rem;
  border-radius: 0.4rem;
  border: 1px solid #ef4444;
  background: transparent;
  color: #fca5a5;
  font-size: 0.78rem;
  cursor: pointer;
}

/* ── Tabs ── */
.summary-modal__tabs {
  display: flex;
  gap: 0.2rem;
  padding: 0.5rem 1.25rem 0;
  background: rgba(0, 0, 0, 0.2);
}

.summary-modal__tab {
  padding: 0.35rem 0.75rem;
  border: none;
  border-radius: 0.35rem 0.35rem 0 0;
  background: transparent;
  color: var(--bs-meta-color);
  font-size: 0.78rem;
  font-weight: 500;
  cursor: pointer;
  transition: background 0.15s, color 0.15s;
}

.summary-modal__tab:hover {
  color: var(--bs-meta-strong);
  background: rgba(75, 85, 99, 0.2);
}

.summary-modal__tab--active {
  color: var(--bs-body-color);
  background: var(--bs-primary-bg-subtle);
  border-bottom: 2px solid var(--bs-primary);
}

/* ── Body (scrollable) ── */
.summary-modal__body {
  flex: 1;
  overflow-y: auto;
  padding: 1rem 1.25rem;
}

/* ── Cards ── */
.summary-cards {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 0.6rem;
  margin-bottom: 1.25rem;
}

.summary-card {
  display: flex;
  flex-direction: column;
  padding: 0.75rem;
  border-radius: 0.6rem;
  background: var(--bs-card-bg);
  border: 1px solid var(--bs-border-color);
}

.summary-card__label {
  /* Micro-header: 11px uppercase with tracking. */
  font-size: 0.7rem;
  color: var(--bs-meta-color);
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.summary-card__value {
  font-size: 1.3rem;
  font-weight: 700;
  color: var(--bs-readout-color);
  font-variant-numeric: tabular-nums;
  margin: 0.15rem 0;
}

.summary-card__sub {
  font-size: 0.7rem;
  color: var(--bs-meta-color);
}

/* ── Sections ── */
.summary-section {
  margin-bottom: 1rem;
}

.summary-section__title {
  font-size: 0.85rem;
  font-weight: 600;
  color: var(--bs-success-text-emphasis);
  margin: 0 0 0.35rem;
  display: flex;
  align-items: baseline;
  gap: 0.5rem;
}

.summary-section__subtitle {
  font-size: 0.72rem;
  font-weight: 400;
  color: var(--bs-meta-color);
}

.summary-section__subtitle--accent {
  color: var(--bs-success-text-emphasis);
  font-weight: 500;
}

.summary-section__subhead {
  font-size: 0.8rem;
  font-weight: 600;
  color: var(--bs-success-text-emphasis);
  margin: 0.75rem 0 0.25rem;
}

.summary-section__description {
  font-size: 0.78rem;
  font-weight: 400;
  color: var(--bs-heading-color);
  line-height: 1.5;
  margin: 0 0 0.75rem;
}

.summary-section__description a,
.summary-section__title a {
  color: var(--bs-info-text-emphasis);
  text-decoration: none;
  font-weight: 500;
}

.summary-section__description code {
  color: var(--bs-info-text-emphasis);
  font-weight: 500;
}

.summary-section__description a:hover,
.summary-section__title a:hover {
  text-decoration: underline;
}

/* ── Availability Banner ── */
.summary-availability-banner {
  display: flex;
  align-items: baseline;
  gap: 0.4rem;
  padding: 0.35rem 0.5rem;
  margin: 0 0 0.75rem;
  border-top: 2px solid var(--bs-success);
  border-bottom: 2px solid var(--bs-success);
  background: var(--bs-tertiary-bg);
}

.summary-availability-banner__count {
  color: var(--bs-success);
  font-weight: 600;
  font-size: 0.78rem;
  font-variant-numeric: tabular-nums;
}

.summary-availability-banner__label {
  color: var(--bs-meta-color);
  font-weight: 400;
  font-size: 0.72rem;
}

/* ── Steps ── */
.summary-steps {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
}

.summary-step {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  padding: 0.4rem 0.5rem;
  border-radius: 0.4rem;
  background: var(--bs-card-bg);
  border: 1px solid var(--bs-border-color);
}

.summary-step--completed {
  border-color: var(--bs-success-bg-subtle);
}

.summary-step__icon {
  font-size: 0.85rem;
  width: 1.2rem;
  text-align: center;
  flex-shrink: 0;
}

.summary-step__icon--ok {
  color: var(--bs-success);
}

.summary-step__icon--pending {
  color: var(--bs-meta-color);
}

.summary-step__body {
  flex: 1;
  min-width: 0;
}

.summary-step__name {
  display: block;
  font-size: 0.8rem;
  font-weight: 600;
  color: var(--bs-meta-strong);
  font-family: var(--bs-font-monospace, ui-monospace, SFMono-Regular, 'Courier New', monospace);
}

.summary-step__desc {
  display: block;
  font-size: 0.68rem;
  color: var(--bs-meta-color);
}

.summary-step__count {
  font-size: 0.7rem;
  color: var(--bs-meta-color);
  font-variant-numeric: tabular-nums;
  flex-shrink: 0;
  background: rgba(75, 85, 99, 0.2);
  padding: 0.1rem 0.4rem;
  border-radius: 4px;
}

/* ── Table ── */
/* Scroll container for the History tab: the full run list scrolls inside
   the modal instead of stretching it. */
.summary-table__scroll {
  max-height: 420px;
  overflow-y: auto;
  border: 1px solid #1e293b;
  border-radius: 0.375rem;
}

/* Sticky header inside the scroll container — matches the modal bg so
   rows pass underneath it cleanly. Headings use the theme emerald
   (--bs-success-text-emphasis), same as the section titles. */
.summary-table__scroll .summary-table th {
  position: sticky;
  top: 0;
  background: var(--bs-emphasis-bg);
  z-index: 1;
}
.summary-table__scroll .summary-table th {
  color: var(--bs-success-text-emphasis);
}

/* Sortable column header: transparent button inheriting the green text. */
.summary-table__sort {
  display: inline-flex;
  align-items: center;
  gap: 0.25rem;
  padding: 0;
  border: none;
  background: none;
  color: inherit;
  font: inherit;
  text-transform: inherit;
  letter-spacing: inherit;
  cursor: pointer;
}
.summary-table__sort:hover {
  color: var(--bs-success-text);
  text-decoration: underline;
}
.summary-table__sort i {
  font-size: 0.65rem;
}

.summary-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.78rem;
}

.summary-table th {
  text-align: left;
  padding: 0.35rem 0.5rem;
  color: var(--bs-meta-color);
  font-weight: 500;
  border-bottom: 1px solid var(--bs-border-color);
  font-size: 0.72rem;
  text-transform: uppercase;
  letter-spacing: 0.03em;
}

.summary-table td {
  padding: 0.35rem 0.5rem;
  color: var(--bs-meta-strong);
  border-bottom: 1px solid var(--bs-emphasis-bg);
}

.summary-table__continent {
  text-transform: capitalize;
  font-weight: 500;
}

.summary-table__key code {
  font-size: 0.7rem;
  color: var(--bs-meta-color);
  word-break: break-all;
}

.summary-table__path {
  font-size: 0.68rem;
  color: var(--bs-meta-color);
  word-break: break-all;
}

.summary-table__country {
  font-weight: 500;
}

.summary-table__iso {
  font-size: 0.65rem;
  color: var(--bs-meta-color);
  margin-left: 0.3rem;
}

.summary-table__date {
  font-size: 0.72rem;
  color: var(--bs-meta-color);
  font-variant-numeric: tabular-nums;
}

/* ── Bar chart ── */
.summary-bar {
  display: inline-flex;
  align-items: center;
  gap: 0.3rem;
  min-width: 80px;
  height: 16px;
  border-radius: 4px;
  background: #1f2937;
  position: relative;
  overflow: hidden;
}

.summary-bar__fill {
  position: absolute;
  left: 0;
  top: 0;
  height: 100%;
  background: var(--bs-success);
  border-radius: 4px;
  transition: width 0.5s ease;
}

.summary-bar--full .summary-bar__fill {
  background: var(--bs-success);
}

.summary-bar__label {
  position: relative;
  z-index: 1;
  font-size: 0.65rem;
  color: var(--bs-body-color);
  padding: 0 0.3rem;
  font-weight: 500;
}

/* ── Dot indicators ── */
.summary-dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 999px;
}

.summary-dot--ok {
  background: var(--bs-success);
}

.summary-dot--missing {
  background: var(--bs-danger);
}

/* ── Badges ── */
.summary-badge {
  padding: 0.1rem 0.35rem;
  border-radius: 4px;
  font-size: 0.68rem;
  font-weight: 500;
}

.summary-badge--ok {
  background: var(--bs-success-bg-subtle);
  color: var(--bs-success);
}

.summary-badge--fail {
  background: var(--bs-danger-bg-subtle);
  color: var(--bs-danger);
}

/* ── Storage ── */
.summary-storage {
  padding: 0.5rem 0.6rem;
  border-radius: 0.4rem;
  background: var(--bs-card-bg);
  border: 1px solid var(--bs-border-color);
}

.summary-storage__path code {
  font-size: 0.7rem;
  color: var(--bs-meta-color);
  word-break: break-all;
}

.summary-storage__stats {
  display: flex;
  gap: 0.75rem;
  margin-top: 0.3rem;
  font-size: 0.78rem;
  color: var(--bs-meta-strong);
}

.summary-storage__contents {
  margin: 0.25rem 0 0;
  font-size: 0.7rem;
  color: var(--bs-meta-color);
}

/* ── Empty ── */
.summary-empty {
  font-size: 0.8rem;
  color: var(--bs-meta-color);
  padding: 0.5rem 0;
}
</style>
