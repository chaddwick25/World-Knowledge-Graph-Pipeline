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
            >Embeddings</button>
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
                <h3 class="summary-section__title">
                  Embeddings by Continent
                  <span class="summary-section__subtitle">{{ countriesWithEmbeddings }}/{{ totalCountries }} countries available</span>
                </h3>
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
                      <td>{{ c.total }}</td>
                      <td>
                        <span
                          class="summary-bar"
                          :class="{ 'summary-bar--full': c.with_embeddings === c.total }"
                        >
                          <span
                            class="summary-bar__fill"
                            :style="{ width: (c.total > 0 ? (c.with_embeddings / c.total) * 100 : 0) + '%' }"
                          ></span>
                          <span class="summary-bar__label">{{ c.with_embeddings }}/{{ c.total }}</span>
                        </span>
                      </td>
                      <td>{{ c.pipelines_completed }}</td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </div>

            <!-- ── Storage Tab ── -->
            <div v-if="activeTab === 'storage'" class="summary-panel">
              <div class="summary-section">
                <h3 class="summary-section__title">Hot Storage (NVME/SSD)</h3>
                <div class="summary-storage">
                  <div class="summary-storage__path">
                    <code>{{ hotStorage.path }}</code>
                  </div>
                  <div class="summary-storage__stats">
                    <span>{{ hotStorage.file_count || 0 }} files</span>
                    <span>{{ hotStorage.size_gb || 0 }} GB</span>
                  </div>
                  <p class="summary-storage__contents">{{ hotStorage.contents }}</p>
                </div>
              </div>
              <div class="summary-section">
                <h3 class="summary-section__title">Cold Storage (HDD - Archive)</h3>
                <div class="summary-storage">
                  <div class="summary-storage__path">
                    <code>{{ coldStorage.path }}</code>
                  </div>
                  <div class="summary-storage__stats">
                    <span>{{ coldStorage.file_count || 0 }} files</span>
                    <span>{{ coldStorage.size_gb || 0 }} GB</span>
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
                    <tr v-for="p in data.paths" :key="p.key">
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
                        <span v-if="p.size_mb">{{ p.size_mb }} MB</span>
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
                <table v-if="data.pipeline_runs.recent.length > 0" class="summary-table">
                  <thead>
                    <tr>
                      <th>Country</th>
                      <th>Snapshot</th>
                      <th>Status</th>
                      <th>Completed</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="run in data.pipeline_runs.recent" :key="run.country_code + run.completed_at">
                      <td>
                        <span class="summary-table__country">{{ run.country_name || run.country_code }}</span>
                        <code class="summary-table__iso">{{ run.country_code }}</code>
                      </td>
                      <td>{{ run.snapshot }}</td>
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
  background: #020617;
  border: 1px solid #1e293b;
  border-radius: 1rem;
  overflow: hidden;
  box-shadow: 0 20px 60px rgba(0, 0, 0, 0.5);
}

.summary-modal__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 1rem 1.25rem;
  border-bottom: 1px solid #111827;
  flex-shrink: 0;
}

.summary-modal__title {
  margin: 0;
  font-size: 1.1rem;
  font-weight: 700;
  color: #f3f4f6;
}

.summary-modal__header-right {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}



.summary-modal__close-btn {
  border: none;
  background: transparent;
  color: #6b7280;
  font-size: 1.5rem;
  cursor: pointer;
  line-height: 1;
  padding: 0 0.25rem;
}

.summary-modal__close-btn:hover {
  color: #f3f4f6;
}

/* ── Loading / Error ── */
.summary-modal__loading {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 0.75rem;
  padding: 3rem 1rem;
  color: #9ca3af;
}

.summary-modal__spinner {
  width: 28px;
  height: 28px;
  border-radius: 999px;
  border: 3px solid rgba(59, 130, 246, 0.2);
  border-top-color: #60a5fa;
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
  color: #6b7280;
  font-size: 0.78rem;
  font-weight: 500;
  cursor: pointer;
  transition: background 0.15s, color 0.15s;
}

.summary-modal__tab:hover {
  color: #d1d5db;
  background: rgba(75, 85, 99, 0.2);
}

.summary-modal__tab--active {
  color: #e5e7eb;
  background: rgba(59, 130, 246, 0.1);
  border-bottom: 2px solid #3b82f6;
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
  background: #0a0f1e;
  border: 1px solid #1f2937;
}

.summary-card__label {
  font-size: 0.7rem;
  color: #6b7280;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.summary-card__value {
  font-size: 1.3rem;
  font-weight: 700;
  color: #f3f4f6;
  margin: 0.15rem 0;
}

.summary-card__sub {
  font-size: 0.7rem;
  color: #9ca3af;
}

/* ── Sections ── */
.summary-section {
  margin-bottom: 1rem;
}

.summary-section__title {
  font-size: 0.85rem;
  font-weight: 600;
  color: #e5e7eb;
  margin: 0 0 0.35rem;
  display: flex;
  align-items: baseline;
  gap: 0.5rem;
}

.summary-section__subtitle {
  font-size: 0.72rem;
  font-weight: 400;
  color: #6b7280;
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
  background: #0a0f1e;
  border: 1px solid #1f2937;
}

.summary-step--completed {
  border-color: rgba(34, 197, 94, 0.15);
}

.summary-step__icon {
  font-size: 0.85rem;
  width: 1.2rem;
  text-align: center;
  flex-shrink: 0;
}

.summary-step__icon--ok {
  color: #22c55e;
}

.summary-step__icon--pending {
  color: #6b7280;
}

.summary-step__body {
  flex: 1;
  min-width: 0;
}

.summary-step__name {
  display: block;
  font-size: 0.8rem;
  font-weight: 600;
  color: #d1d5db;
  font-family: ui-monospace, SFMono-Regular, 'Courier New', monospace;
}

.summary-step__desc {
  display: block;
  font-size: 0.68rem;
  color: #6b7280;
}

.summary-step__count {
  font-size: 0.7rem;
  color: #9ca3af;
  flex-shrink: 0;
  background: rgba(75, 85, 99, 0.2);
  padding: 0.1rem 0.4rem;
  border-radius: 4px;
}

/* ── Table ── */
.summary-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.78rem;
}

.summary-table th {
  text-align: left;
  padding: 0.35rem 0.5rem;
  color: #6b7280;
  font-weight: 500;
  border-bottom: 1px solid #1f2937;
  font-size: 0.72rem;
  text-transform: uppercase;
  letter-spacing: 0.03em;
}

.summary-table td {
  padding: 0.35rem 0.5rem;
  color: #d1d5db;
  border-bottom: 1px solid #111827;
}

.summary-table__continent {
  text-transform: capitalize;
  font-weight: 500;
}

.summary-table__key code {
  font-size: 0.7rem;
  color: #9ca3af;
  word-break: break-all;
}

.summary-table__path {
  font-size: 0.68rem;
  color: #525252;
  word-break: break-all;
}

.summary-table__country {
  font-weight: 500;
}

.summary-table__iso {
  font-size: 0.65rem;
  color: #6b7280;
  margin-left: 0.3rem;
}

.summary-table__date {
  font-size: 0.72rem;
  color: #9ca3af;
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
  background: #22c55e;
  border-radius: 4px;
  transition: width 0.5s ease;
}

.summary-bar--full .summary-bar__fill {
  background: #22c55e;
}

.summary-bar__label {
  position: relative;
  z-index: 1;
  font-size: 0.65rem;
  color: #e5e7eb;
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
  background: #22c55e;
}

.summary-dot--missing {
  background: #ef4444;
}

/* ── Badges ── */
.summary-badge {
  padding: 0.1rem 0.35rem;
  border-radius: 4px;
  font-size: 0.68rem;
  font-weight: 500;
}

.summary-badge--ok {
  background: rgba(34, 197, 94, 0.15);
  color: #22c55e;
}

.summary-badge--fail {
  background: rgba(239, 68, 68, 0.15);
  color: #ef4444;
}

/* ── Storage ── */
.summary-storage {
  padding: 0.5rem 0.6rem;
  border-radius: 0.4rem;
  background: #0a0f1e;
  border: 1px solid #1f2937;
}

.summary-storage__path code {
  font-size: 0.7rem;
  color: #9ca3af;
  word-break: break-all;
}

.summary-storage__stats {
  display: flex;
  gap: 0.75rem;
  margin-top: 0.3rem;
  font-size: 0.78rem;
  color: #d1d5db;
}

.summary-storage__contents {
  margin: 0.25rem 0 0;
  font-size: 0.7rem;
  color: #6b7280;
}

/* ── Empty ── */
.summary-empty {
  font-size: 0.8rem;
  color: #6b7280;
  padding: 0.5rem 0;
}
</style>
