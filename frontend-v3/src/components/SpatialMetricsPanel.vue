<script>
import axios from 'axios'

export default {
  name: 'SpatialMetricsPanel',
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
  data() {
    return {
      loading: false,
      error: null,
      subgraphs: [],
      isoCode: null,
      // Toggle view
      showSubgraphs: true,
      heatmapMetric: null, // null | 'entity_density' | 'accepted_links' | 'rejected_links' | 'acceptance_rate'
      // Summary data from augmented endpoint for overlay
      summary: null,
      // Augmented data service access
      selectedSubgraph: null,
    }
  },
  computed: {
    metricOptions() {
      return [
        { value: 'entity_density', label: 'Entity Density' },
        { value: 'accepted_links', label: 'Accepted Links' },
        { value: 'rejected_links', label: 'Rejected Links' },
        { value: 'acceptance_rate', label: 'Acceptance Rate' },
      ]
    },
    formattedMetrics() {
      if (!this.summary?.subgraph_groups?.length) return []
      return this.summary.subgraph_groups.map((sg) => {
        const total = sg.accepted_count + sg.rejected_count
        return {
          name: sg.subgraph_name,
          slug: sg.subgraph_slug,
          accepted: sg.accepted_count,
          rejected: sg.rejected_count,
          total,
          rate: total > 0 ? ((sg.accepted_count / total) * 100).toFixed(1) : '0.0',
          avg_conf: sg.avg_confidence ? (sg.avg_confidence * 100).toFixed(1) : '—',
        }
      })
    },
    coverageSummary() {
      if (!this.formattedMetrics.length) return null
      const totalAccepted = this.formattedMetrics.reduce((s, g) => s + g.accepted, 0)
      const totalRejected = this.formattedMetrics.reduce((s, g) => s + g.rejected, 0)
      const total = totalAccepted + totalRejected
      return {
        subgraphCount: this.formattedMetrics.length,
        totalAccepted,
        totalRejected,
        total,
        rate: total > 0 ? ((totalAccepted / total) * 100).toFixed(1) : '0.0',
      }
    },
  },
  watch: {
    countryName() {
      this.fetchData()
    },
    snapshotDate() {
      this.fetchData()
    },
  },
  mounted() {
    if (this.countryName) {
      this.fetchData()
    }
  },
  methods: {
    async fetchData() {
      if (!this.countryName) return
      this.loading = true
      this.error = null
      this.subgraphs = []
      this.summary = null
      this.selectedSubgraph = null

      try {
        // Fetch both subgraph profiles and augmented summary in parallel
        const encoded = encodeURIComponent(this.countryName)
        const params = {}
        if (this.snapshotDate) params.snapshot_date = this.snapshotDate

        // 1. Fetch subgraphs from country-subgraphs endpoint
        const sgPromise = axios
          .get(`/country-subgraphs/${encoded}/`, { params })
          .then((r) => {
            this.subgraphs = (r.data?.subgraphs || r.data?.results || []).map((sg) => ({
              slug: sg.slug || sg.subgraph_slug || '',
              name: sg.name || sg.subgraph_name || sg.slug || '',
              has_pbf: sg.has_subgraph_pbf ?? sg.has_pbf ?? false,
              has_poly: sg.has_subgraph_poly ?? sg.has_poly ?? false,
              has_pickle: sg.has_subgraph_pickle ?? sg.has_pickle ?? false,
              node_count: sg.node_count ?? 0,
              way_count: sg.way_count ?? 0,
              file_size_bytes: sg.file_size_bytes ?? 0,
              metadata_status: sg.metadata_status || '',
            }))
          })
          .catch(() => {
            // Subgraph endpoint may not exist — skip gracefully
            this.subgraphs = []
          })

        // 2. Fetch augmented summary for link metrics
        const summaryPromise = axios
          .get(`/data/augmented-summary/${encoded}/`, { params })
          .then((r) => {
            this.summary = r.data
          })
          .catch(() => {
            this.summary = null
          })

        await Promise.all([sgPromise, summaryPromise])
      } catch (e) {
        console.error('Error fetching spatial data:', e)
        this.error = e.message || 'Failed to load spatial data'
      } finally {
        this.loading = false
      }
    },
    selectSubgraph(sg) {
      this.selectedSubgraph = this.selectedSubgraph?.slug === sg.slug ? null : sg
    },
    setHeatmap(metric) {
      this.heatmapMetric = this.heatmapMetric === metric ? null : metric
    },
    subgraphBarColor(sg, metric) {
      if (!metric || !sg) return '#374151'
      // Simple color scale: higher = greener, lower = redder
      const all = this.formattedMetrics
      const vals = all.map((g) => {
        if (metric === 'acceptance_rate') return parseFloat(g.rate)
        if (metric === 'accepted_links') return g.accepted
        if (metric === 'rejected_links') return g.rejected
        if (metric === 'entity_density') return g.total
        return 0
      })
      const max = Math.max(...vals, 1)
      const val = metric === 'acceptance_rate' ? parseFloat(sg.rate) : (sg[metric] || 0)
      const pct = val / max
      if (pct > 0.66) return '#22c55e'
      if (pct > 0.33) return '#eab308'
      return '#ef4444'
    },
  },
}
</script>

<template>
  <div class="spatial">
    <!-- Loading -->
    <div v-if="loading" class="spatial__center">
      <div class="spinner"></div>
      <p class="spatial__hint">Loading spatial data…</p>
    </div>

    <!-- Error -->
    <p v-else-if="error" class="spatial__error">{{ error }}</p>

    <!-- No data -->
    <div v-else-if="!subgraphs.length && !formattedMetrics.length" class="spatial__center">
      <p class="spatial__hint">No spatial data available for {{ countryName }}.</p>
    </div>

    <!-- Content -->
    <div v-else class="spatial__content">
      <!-- Coverage summary -->
      <div v-if="coverageSummary" class="spatial__metrics">
        <div class="metric">
          <div class="metric__value">{{ coverageSummary.subgraphCount }}</div>
          <div class="metric__label">Subgraphs</div>
        </div>
        <div class="metric">
          <div class="metric__value metric__value--accent">{{ coverageSummary.totalAccepted.toLocaleString() }}</div>
          <div class="metric__label">Accepted</div>
        </div>
        <div class="metric">
          <div class="metric__value metric__value--danger">{{ coverageSummary.totalRejected.toLocaleString() }}</div>
          <div class="metric__label">Rejected</div>
        </div>
        <div class="metric">
          <div class="metric__value">{{ coverageSummary.rate }}%</div>
          <div class="metric__label">Rate</div>
        </div>
      </div>

      <!-- Subgraph profiles -->
      <div v-if="subgraphs.length" class="spatial__section">
        <h4 class="spatial__section-title">
          Subgraph Profiles ({{ subgraphs.length }})
        </h4>
        <div class="spatial__subgraphs">
          <div
            v-for="(sg, idx) in subgraphs"
            :key="idx"
            class="sg-card"
            :class="{ 'sg-card--selected': selectedSubgraph?.slug === sg.slug }"
            @click="selectSubgraph(sg)"
          >
            <div class="sg-card__header">
              <span class="sg-card__name">{{ sg.name }}</span>
              <span class="sg-card__slug">{{ sg.slug }}</span>
            </div>

            <!-- Feature badges -->
            <div class="sg-card__badges">
              <span
                v-if="sg.has_pbf"
                class="sg-card__badge sg-card__badge--success"
              >PBF</span>
              <span
                v-if="sg.has_poly"
                class="sg-card__badge sg-card__badge--success"
              >Poly</span>
              <span
                v-if="sg.has_pickle"
                class="sg-card__badge sg-card__badge--info"
              >Pickle</span>
              <span
                v-if="!sg.has_poly"
                class="sg-card__badge sg-card__badge--muted"
              >No Poly</span>
            </div>

            <!-- Metrics -->
            <div v-if="sg.node_count" class="sg-card__metrics">
              <span>{{ sg.node_count.toLocaleString() }} nodes</span>
              <span v-if="sg.way_count">{{ sg.way_count.toLocaleString() }} ways</span>
              <span v-if="sg.file_size_bytes">{{ (sg.file_size_bytes / (1024 * 1024)).toFixed(1) }} MB</span>
            </div>

            <!-- Status -->
            <div v-if="sg.metadata_status" class="sg-card__status">
              Status: {{ sg.metadata_status }}
            </div>
          </div>
        </div>
      </div>

      <!-- Link metrics by subgraph (from augmented data) -->
      <div v-if="formattedMetrics.length" class="spatial__section">
        <div class="spatial__section-header">
          <h4 class="spatial__section-title">USLP Link Coverage</h4>

          <!-- Heatmap metric selector -->
          <div class="spatial__heatmap-select">
            <button
              v-for="opt in metricOptions"
              :key="opt.value"
              class="spatial__heatmap-btn"
              :class="{ 'spatial__heatmap-btn--active': heatmapMetric === opt.value }"
              @click="setHeatmap(opt.value)"
            >
              {{ opt.label }}
            </button>
          </div>
        </div>

        <div class="spatial__link-metrics">
          <div
            v-for="(sg, idx) in formattedMetrics"
            :key="idx"
            class="link-row"
            @click="selectSubgraph({ slug: sg.slug, name: sg.name })"
          >
            <div class="link-row__header">
              <span class="link-row__name">{{ sg.name }}</span>
              <span class="link-row__value">{{ sg.rate }}%</span>
            </div>
            <div class="link-row__bar-track">
              <div
                class="link-row__bar link-row__bar--accepted"
                :style="{ width: sg.rate + '%', background: subgraphBarColor(sg, heatmapMetric) }"
              />
            </div>
            <div class="link-row__details">
              <span class="link-row__accepted">{{ sg.accepted }} accepted</span>
              <span class="link-row__rejected">{{ sg.rejected }} rejected</span>
              <span v-if="sg.avg_conf !== '—'" class="link-row__conf">Ø {{ sg.avg_conf }}%</span>
            </div>
          </div>
        </div>
      </div>

      <!-- Selected subgraph detail -->
      <div v-if="selectedSubgraph" class="spatial__section">
        <h4 class="spatial__section-title">
          Selected: {{ selectedSubgraph.name }}
        </h4>
        <!-- Show matching metric from formattedMetrics -->
        <div
          v-for="sg in formattedMetrics.filter((s) => s.slug === selectedSubgraph.slug)"
          :key="sg.slug"
          class="sg-detail"
        >
          <div class="sg-detail__grid">
            <div class="sg-detail__item">
              <span class="sg-detail__label">Accepted</span>
              <span class="sg-detail__value sg-detail__value--success">{{ sg.accepted.toLocaleString() }}</span>
            </div>
            <div class="sg-detail__item">
              <span class="sg-detail__label">Rejected</span>
              <span class="sg-detail__value sg-detail__value--danger">{{ sg.rejected.toLocaleString() }}</span>
            </div>
            <div class="sg-detail__item">
              <span class="sg-detail__label">Rate</span>
              <span class="sg-detail__value">{{ sg.rate }}%</span>
            </div>
            <div class="sg-detail__item">
              <span class="sg-detail__label">Avg Confidence</span>
              <span class="sg-detail__value">{{ sg.avg_conf }}%</span>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.spatial {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}

.spatial__center {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 0.5rem;
  padding: 1.5rem 0.5rem;
}

.spatial__hint {
  margin: 0;
  font-size: 0.78rem;
  color: #6b7280;
  text-align: center;
}

.spatial__error {
  margin: 0;
  font-size: 0.78rem;
  color: #fecaca;
  padding: 0.3rem 0.4rem;
  background: rgba(239, 68, 68, 0.08);
  border-radius: 0.4rem;
}

.spatial__metrics {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0.35rem;
}

.metric {
  padding: 0.4rem 0.5rem;
  border-radius: 0.5rem;
  background: #020617;
  border: 1px solid #111827;
  text-align: center;
}

.metric__value {
  font-size: 0.95rem;
  font-weight: 600;
  color: #e5e7eb;
}

.metric__value--accent {
  color: #60a5fa;
}

.metric__value--danger {
  color: #ef4444;
}

.metric__label {
  font-size: 0.68rem;
  color: #9ca3af;
}

.spatial__section {
  padding: 0.35rem 0;
  border-top: 1px solid rgba(31, 41, 55, 0.5);
}

.spatial__section-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 0.3rem;
  margin-bottom: 0.3rem;
}

.spatial__section-title {
  margin: 0;
  font-size: 0.78rem;
  font-weight: 600;
  color: #d1d5db;
}

/* Heatmap metric selector */

.spatial__heatmap-select {
  display: flex;
  flex-wrap: wrap;
  gap: 0.2rem;
}

.spatial__heatmap-btn {
  padding: 0.1rem 0.3rem;
  border: 1px solid #374151;
  border-radius: 3px;
  background: transparent;
  font-size: 0.6rem;
  color: #9ca3af;
  cursor: pointer;
}

.spatial__heatmap-btn:hover {
  background: rgba(75, 85, 99, 0.3);
}

.spatial__heatmap-btn--active {
  background: #1f2937;
  color: #e5e7eb;
  border-color: #6366f1;
}

/* Subgraph cards */

.spatial__subgraphs {
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
  max-height: 260px;
  overflow-y: auto;
}

.sg-card {
  padding: 0.3rem 0.45rem;
  border-radius: 0.4rem;
  background: rgba(15, 23, 42, 0.5);
  border: 1px solid #1f2937;
  cursor: pointer;
  transition: border-color 0.15s;
}

.sg-card:hover {
  border-color: #374151;
}

.sg-card--selected {
  border-color: #6366f1;
  background: rgba(99, 102, 241, 0.08);
}

.sg-card__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.sg-card__name {
  font-size: 0.75rem;
  font-weight: 600;
}

.sg-card__slug {
  font-size: 0.62rem;
  color: #6b7280;
}

.sg-card__badges {
  display: flex;
  gap: 0.25rem;
  margin-top: 0.2rem;
}

.sg-card__badge {
  padding: 0.05rem 0.25rem;
  border-radius: 3px;
  font-size: 0.58rem;
  border: 1px solid transparent;
}

.sg-card__badge--success {
  background: rgba(34, 197, 94, 0.12);
  border-color: #22c55e;
  color: #22c55e;
}

.sg-card__badge--info {
  background: rgba(59, 130, 246, 0.12);
  border-color: #60a5fa;
  color: #60a5fa;
}

.sg-card__badge--muted {
  background: rgba(75, 85, 99, 0.15);
  border-color: #4b5563;
  color: #9ca3af;
}

.sg-card__metrics {
  margin-top: 0.2rem;
  display: flex;
  gap: 0.4rem;
  font-size: 0.62rem;
  color: #6b7280;
}

.sg-card__status {
  margin-top: 0.15rem;
  font-size: 0.6rem;
  color: #9ca3af;
}

/* Link metrics rows */

.spatial__link-metrics {
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
}

.link-row {
  padding: 0.25rem 0.4rem;
  border-radius: 0.35rem;
  background: rgba(15, 23, 42, 0.5);
  cursor: pointer;
}

.link-row:hover {
  background: rgba(31, 41, 55, 0.5);
}

.link-row__header {
  display: flex;
  justify-content: space-between;
  font-size: 0.72rem;
}

.link-row__name {
  max-width: 130px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.link-row__value {
  font-weight: 600;
  color: #e5e7eb;
}

.link-row__bar-track {
  margin-top: 0.15rem;
  height: 6px;
  background: #111827;
  border-radius: 999px;
  overflow: hidden;
}

.link-row__bar {
  height: 100%;
  border-radius: 999px;
  transition: width 0.2s;
}

.link-row__details {
  display: flex;
  gap: 0.4rem;
  margin-top: 0.1rem;
  font-size: 0.62rem;
}

.link-row__accepted {
  color: #22c55e;
}

.link-row__rejected {
  color: #ef4444;
}

.link-row__conf {
  color: #9ca3af;
}

/* Selected subgraph detail */

.sg-detail__grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 0.3rem;
}

.sg-detail__item {
  padding: 0.3rem 0.4rem;
  border-radius: 0.35rem;
  background: rgba(15, 23, 42, 0.5);
  text-align: center;
}

.sg-detail__label {
  display: block;
  font-size: 0.6rem;
  color: #9ca3af;
}

.sg-detail__value {
  display: block;
  font-size: 0.85rem;
  font-weight: 600;
  margin-top: 0.1rem;
}

.sg-detail__value--success {
  color: #22c55e;
}

.sg-detail__value--danger {
  color: #ef4444;
}

/* Spinner */

.spinner {
  width: 24px;
  height: 24px;
  border-radius: 999px;
  border: 3px solid rgba(148, 163, 184, 0.4);
  border-top-color: #6366f1;
  animation: spin 0.8s linear infinite;
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}
</style>
