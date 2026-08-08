<script>
import axios from 'axios'

export default {
  name: 'AugmentedDataPanel',
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
      summary: null,
      detail: null,
      loading: false,
      loadingDetail: false,
      error: null,
      activeView: 'summary', // 'summary' | 'detail'
      detailPage: 1,
      detailPageSize: 20,
      detailTotalAccepted: 0,
      detailTotalRejected: 0,
    }
  },
  computed: {
    maxRelationCount() {
      if (!this.summary?.relation_distribution?.length) return 1
      return this.summary.relation_distribution[0].count
    },
    maxSubgraphCount() {
      if (!this.summary?.subgraph_groups?.length) return 1
      return Math.max(...this.summary.subgraph_groups.map((sg) => sg.accepted_count + sg.rejected_count))
    },
    acceptedRate() {
      if (!this.summary) return 0
      return (this.summary.acceptance_rate * 100).toFixed(1)
    },
    displayedDetailLinks() {
      if (!this.detail) return []
      const start = (this.detailPage - 1) * this.detailPageSize
      const end = start + this.detailPageSize
      return [...(this.detail.accepted_links || []), ...(this.detail.rejected_links || [])].slice(start, end)
    },
  },
  watch: {
    countryName() {
      this.fetchSummary()
    },
    snapshotDate() {
      this.fetchSummary()
      if (this.activeView === 'detail') this.fetchDetail(this.detailPage)
    },
  },
  mounted() {
    if (this.countryName) {
      this.fetchSummary()
    }
  },
  methods: {
    async fetchSummary() {
      if (!this.countryName) return
      this.loading = true
      this.error = null
      this.summary = null
      try {
        const encoded = encodeURIComponent(this.countryName)
        const params = {}
        if (this.snapshotDate) params.snapshot_date = this.snapshotDate
        const { data } = await axios.get(`/data/augmented-summary/${encoded}/`, { params })
        this.summary = data
      } catch (e) {
        console.error('Error fetching augmented summary:', e)
        this.error = e.response?.data?.error || e.message || 'Failed to load augmented data'
      } finally {
        this.loading = false
      }
    },
    async fetchDetail(page = 1) {
      if (!this.countryName) return
      this.loadingDetail = true
      this.detailPage = page
      try {
        const encoded = encodeURIComponent(this.countryName)
        const params = { page: page, page_size: this.detailPageSize }
        if (this.snapshotDate) params.snapshot_date = this.snapshotDate
        const { data } = await axios.get(`/data/augmented-detail/${encoded}/`, {
          params,
        })
        this.detail = data
        this.detailTotalAccepted = data.total_accepted || 0
        this.detailTotalRejected = data.total_rejected || 0
      } catch (e) {
        console.error('Error fetching augmented detail:', e)
      } finally {
        this.loadingDetail = false
      }
    },
    switchView(view) {
      this.activeView = view
      if (view === 'detail' && !this.detail) {
        this.fetchDetail()
      }
    },
    scoreBarWidth(count) {
      if (!this.summary?.score_distribution?.counts) return '0%'
      const max = Math.max(...this.summary.score_distribution.counts, 1)
      return `${(count / max) * 100}%`
    },
    subgraphBarWidth(sg) {
      const total = sg.accepted_count + sg.rejected_count
      return `${(total / this.maxSubgraphCount) * 100}%`
    },
    acceptedSubgraphWidth(sg) {
      const total = sg.accepted_count + sg.rejected_count
      if (total === 0) return '0%'
      return `${(sg.accepted_count / total) * 100}%`
    },
    prevPage() {
      if (this.detailPage > 1) this.fetchDetail(this.detailPage - 1)
    },
    nextPage() {
      const total = this.detailTotalAccepted + this.detailTotalRejected
      if (this.detailPage * this.detailPageSize < total) this.fetchDetail(this.detailPage + 1)
    },
  },
}
</script>

<template>
  <div class="augmented">
    <!-- Loading -->
    <div v-if="loading" class="augmented__center">
      <div class="spinner"></div>
      <p class="augmented__hint">Loading augmented data…</p>
    </div>

    <!-- Error -->
    <p v-else-if="error" class="augmented__error">{{ error }}</p>

    <!-- No data -->
    <div v-else-if="!summary" class="augmented__center">
      <p class="augmented__hint">No augmented data available for {{ countryName }}.</p>
    </div>

    <!-- Summary content -->
    <div v-else class="augmented__content">
      <!-- Tab switcher -->
      <div class="augmented__tabs">
        <button
          class="augmented__tab"
          :class="{ 'augmented__tab--active': activeView === 'summary' }"
          @click="switchView('summary')"
        >Summary</button>
        <button
          class="augmented__tab"
          :class="{ 'augmented__tab--active': activeView === 'detail' }"
          @click="switchView('detail')"
        >Detail</button>
      </div>

      <!-- ── Summary View ── -->
      <div v-if="activeView === 'summary'" class="augmented__body">
        <!-- Top-level metrics -->
        <div class="augmented__metrics">
          <div class="metric">
            <div class="metric__value metric__value--accent">{{ summary.total_accepted.toLocaleString() }}</div>
            <div class="metric__label">Accepted</div>
          </div>
          <div class="metric">
            <div class="metric__value metric__value--danger">{{ summary.total_rejected.toLocaleString() }}</div>
            <div class="metric__label">Rejected</div>
          </div>
          <div class="metric">
            <div class="metric__value">{{ acceptedRate }}%</div>
            <div class="metric__label">Acceptance Rate</div>
          </div>
          <div class="metric">
            <div class="metric__value">{{ summary.total_entities.toLocaleString() }}</div>
            <div class="metric__label">Total Entities</div>
          </div>
        </div>

        <!-- Score distribution histogram -->
        <div v-if="summary.score_distribution" class="augmented__section">
          <h4 class="augmented__section-title">Score Distribution</h4>
          <div class="histogram">
            <div class="histogram__bars">
              <div
                v-for="(count, idx) in summary.score_distribution.counts"
                :key="idx"
                class="histogram__bar-wrapper"
              >
                <small class="histogram__value">{{ count.toLocaleString() }}</small>
                <div
                  class="histogram__bar"
                  :style="{ height: scoreBarWidth(count) }"
                />
                <small class="histogram__label">{{ summary.score_distribution.buckets[idx] }}</small>
              </div>
            </div>
          </div>
        </div>

        <!-- Link type breakdown -->
        <div v-if="summary.link_type_breakdown" class="augmented__section">
          <h4 class="augmented__section-title">Link Type Breakdown</h4>
          <div class="link-types">
            <div class="link-type-row">
              <span class="link-type-row__label">Geo Dominant</span>
              <span class="link-type-row__value">{{ summary.link_type_breakdown.geo_dominant.toLocaleString() }}</span>
            </div>
            <div class="link-type-row">
              <span class="link-type-row__label">Name Dominant</span>
              <span class="link-type-row__value">{{ summary.link_type_breakdown.name_dominant.toLocaleString() }}</span>
            </div>
            <div class="link-type-row">
              <span class="link-type-row__label">Class Dominant</span>
              <span class="link-type-row__value">{{ summary.link_type_breakdown.class_dominant.toLocaleString() }}</span>
            </div>
            <div class="link-type-row">
              <span class="link-type-row__label">Mixed</span>
              <span class="link-type-row__value">{{ summary.link_type_breakdown.mixed.toLocaleString() }}</span>
            </div>
          </div>
        </div>

        <!-- Top relations -->
        <div v-if="summary.relation_distribution?.length" class="augmented__section">
          <h4 class="augmented__section-title">Top Relations</h4>
          <div class="relations">
            <div
              v-for="(rel, idx) in summary.relation_distribution.slice(0, 8)"
              :key="idx"
              class="relations__row"
            >
              <div class="relations__header">
                <span class="relations__name">{{ rel.relation }}</span>
                <span class="relations__meta">
                  {{ rel.count.toLocaleString() }} ·
                  {{ (rel.acceptance_rate * 100).toFixed(0) }}%
                </span>
              </div>
              <div class="relations__bar-track">
                <div
                  class="relations__bar relations__bar--accepted"
                  :style="{ width: `${(rel.accepted / rel.count) * 100}%` }"
                />
              </div>
            </div>
          </div>
        </div>

        <!-- Subgraph groups -->
        <div v-if="summary.subgraph_groups?.length" class="augmented__section">
          <h4 class="augmented__section-title">Per Subgraph</h4>
          <div class="subgraphs">
            <div
              v-for="(sg, idx) in summary.subgraph_groups"
              :key="idx"
              class="subgraphs__card"
            >
              <div class="subgraphs__header">
                <span class="subgraphs__name">{{ sg.subgraph_name }}</span>
              </div>
              <div class="subgraphs__stats">
                <span class="subgraphs__stat subgraphs__stat--success">
                  {{ sg.accepted_count.toLocaleString() }} accepted
                </span>
                <span class="subgraphs__stat subgraphs__stat--danger">
                  {{ sg.rejected_count.toLocaleString() }} rejected
                </span>
              </div>
              <div class="relations__bar-track subgraphs__bar-track">
                <div
                  v-if="sg.accepted_count + sg.rejected_count > 0"
                  class="relations__bar relations__bar--accepted"
                  :style="{ width: acceptedSubgraphWidth(sg) }"
                />
              </div>
              <div v-if="sg.avg_confidence" class="subgraphs__confidence">
                Avg confidence: {{ (sg.avg_confidence * 100).toFixed(1) }}%
              </div>
            </div>
          </div>
        </div>

        <!-- Augmentation estimate -->
        <div v-if="summary.augmentation_estimate" class="augmented__section">
          <h4 class="augmented__section-title">Augmentation Estimate</h4>
          <div class="augment-box">
            <p class="augment-box__text">{{ summary.augmentation_estimate.reasoning }}</p>
            <div class="augmented__metrics">
              <div class="metric">
                <div class="metric__value metric__value--warning">
                  {{ summary.augmentation_estimate.estimated_augmentable.toLocaleString() }}
                </div>
                <div class="metric__label">Augmentable</div>
              </div>
              <div class="metric">
                <div class="metric__value">
                  {{ summary.augmentation_estimate.augmentable_pct }}%
                </div>
                <div class="metric__label">of Rejected</div>
              </div>
              <div class="metric">
                <div class="metric__value metric__value--cost">
                  ${{ summary.augmentation_estimate.estimated_total_cost.toFixed(2) }}
                </div>
                <div class="metric__label">Est. Cost (Geocoding)</div>
              </div>
            </div>
            <div v-if="summary.augmentation_estimate.by_relation?.length" class="augment-relations">
              <div
                v-for="(rel, idx) in summary.augmentation_estimate.by_relation.slice(0, 5)"
                :key="idx"
                class="relations__row"
              >
                <div class="relations__header">
                  <span class="relations__name">{{ rel.relation }}</span>
                  <span class="relations__meta">
                    {{ rel.estimated_augmentable }}/{{ rel.total }} · ${{ rel.cost_usd.toFixed(2) }}
                  </span>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- ── Detail View ── -->
      <div v-else class="augmented__body">
        <!-- Loading -->
        <div v-if="loadingDetail" class="augmented__center">
          <div class="spinner"></div>
          <p class="augmented__hint">Loading detail…</p>
        </div>

        <div v-else-if="detail" class="augmented__detail">
          <div class="augmented__metrics">
            <div class="metric">
              <div class="metric__value metric__value--accent">{{ detail.total_accepted.toLocaleString() }}</div>
              <div class="metric__label">Accepted</div>
            </div>
            <div class="metric">
              <div class="metric__value metric__value--danger">{{ detail.total_rejected.toLocaleString() }}</div>
              <div class="metric__label">Rejected</div>
            </div>
          </div>

          <!-- Accepted links -->
          <div class="augmented__section">
            <h4 class="augmented__section-title">Accepted Links (page {{ detailPage }})</h4>
            <div v-if="detail.accepted_links?.length" class="detail-list">
              <div
                v-for="link in detail.accepted_links"
                :key="link.id"
                class="detail-row"
              >
                <div class="detail-row__header">
                  <span class="detail-row__relation">{{ link.relation }}</span>
                  <span class="detail-row__score">score: {{ (link.normalized_score * 100).toFixed(1) }}%</span>
                  <span class="detail-row__type detail-row__type--{{ link.dominant_type }}">{{ link.dominant_type }}</span>
                </div>
                <div class="detail-row__ids">
                  <code>{{ link.head_osm_type }}/{{ link.head_osm_id }}</code>
                  <span class="detail-row__arrow">→</span>
                  <code>{{ link.tail_osm_type }}/{{ link.tail_osm_id }}</code>
                </div>
                <div class="detail-row__scores">
                  <span>geo: {{ link.geo_score.toFixed(3) }}</span>
                  <span>name: {{ link.name_score.toFixed(3) }}</span>
                  <span>class: {{ link.topo_score.toFixed(3) }}</span>
                </div>
              </div>
            </div>
            <p v-else class="augmented__hint">No accepted links on this page.</p>
          </div>

          <!-- Pagination -->
          <div class="augmented__pagination">
            <button
              class="augmented__page-btn"
              :disabled="detailPage <= 1"
              @click="prevPage"
            >← Prev</button>
            <span class="augmented__page-info">Page {{ detailPage }}</span>
            <button
              class="augmented__page-btn"
              :disabled="detailPage * detailPageSize >= (detail.total_accepted + detail.total_rejected)"
              @click="nextPage"
            >Next →</button>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.augmented {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}

.augmented__center {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 0.5rem;
  padding: 1.5rem 0.5rem;
}

.augmented__hint {
  margin: 0;
  font-size: 0.78rem;
  color: #6b7280;
  text-align: center;
}

.augmented__error {
  margin: 0;
  font-size: 0.78rem;
  color: #fecaca;
  padding: 0.3rem 0.4rem;
  background: rgba(239, 68, 68, 0.08);
  border-radius: 0.4rem;
}

.augmented__tabs {
  display: flex;
  gap: 0.2rem;
  background: rgba(15, 23, 42, 0.6);
  border-radius: 0.4rem;
  padding: 0.15rem;
}

.augmented__tab {
  flex: 1;
  padding: 0.25rem 0.3rem;
  border: none;
  border-radius: 0.3rem;
  background: transparent;
  color: #9ca3af;
  font-size: 0.7rem;
  cursor: pointer;
  transition: background 0.15s;
}

.augmented__tab:hover {
  background: rgba(75, 85, 99, 0.3);
}

.augmented__tab--active {
  background: #1f2937;
  color: #e5e7eb;
  font-weight: 600;
}

.augmented__body {
  display: flex;
  flex-direction: column;
  gap: 0.6rem;
}

.augmented__metrics {
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

.metric__value--warning {
  color: #facc15;
}

.metric__value--cost {
  color: #fb923c;
}

.metric__label {
  font-size: 0.68rem;
  color: #9ca3af;
}

.augmented__section-title {
  margin: 0 0 0.3rem;
  font-size: 0.78rem;
  font-weight: 600;
  color: #d1d5db;
}

.augmented__section {
  padding: 0.35rem 0;
  border-top: 1px solid rgba(31, 41, 55, 0.5);
}

/* Histogram */

.histogram__bars {
  display: flex;
  align-items: flex-end;
  gap: 0.25rem;
  padding-bottom: 0.15rem;
}

.histogram__bar-wrapper {
  flex: 1;
  text-align: center;
}

.histogram__value {
  display: block;
  font-size: 0.6rem;
  color: #9ca3af;
}

.histogram__bar {
  width: 100%;
  max-width: 30px;
  margin: 0 auto;
  border-radius: 3px 3px 0 0;
  background: linear-gradient(180deg, #6366f1, #4f46e5);
  min-height: 3px;
}

.histogram__label {
  display: block;
  font-size: 0.6rem;
  color: #6b7280;
  margin-top: 0.05rem;
}

/* Link types */

.link-types {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 0.25rem;
}

.link-type-row {
  display: flex;
  justify-content: space-between;
  padding: 0.25rem 0.4rem;
  border-radius: 0.3rem;
  background: rgba(15, 23, 42, 0.5);
  font-size: 0.72rem;
}

.link-type-row__value {
  font-weight: 600;
  color: #e5e7eb;
}

/* Relations */

.relations__row {
  margin-bottom: 0.4rem;
}

.relations__header {
  display: flex;
  justify-content: space-between;
  font-size: 0.72rem;
}

.relations__name {
  max-width: 130px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.relations__meta {
  color: #9ca3af;
  font-size: 0.68rem;
}

.relations__bar-track {
  margin-top: 0.15rem;
  height: 6px;
  background: #111827;
  border-radius: 999px;
  overflow: hidden;
}

.relations__bar {
  height: 100%;
  border-radius: 999px;
}

.relations__bar--accepted {
  background: #22c55e;
}

/* Subgraphs */

.subgraphs {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
}

.subgraphs__card {
  padding: 0.35rem 0.5rem;
  border-radius: 0.4rem;
  background: rgba(15, 23, 42, 0.5);
  border: 1px solid #1f2937;
}

.subgraphs__header {
  font-size: 0.75rem;
  font-weight: 600;
  margin-bottom: 0.15rem;
}

.subgraphs__stats {
  display: flex;
  gap: 0.5rem;
  font-size: 0.68rem;
  margin-bottom: 0.2rem;
}

.subgraphs__stat--success {
  color: #22c55e;
}

.subgraphs__stat--danger {
  color: #ef4444;
}

.subgraphs__bar-track {
  margin-bottom: 0.15rem;
}

.subgraphs__confidence {
  font-size: 0.65rem;
  color: #9ca3af;
}

/* Augmentation */

.augment-box {
  padding: 0.35rem 0.4rem;
  border-radius: 0.4rem;
  background: rgba(15, 23, 42, 0.4);
}

.augment-box__text {
  margin: 0 0 0.4rem;
  font-size: 0.72rem;
  color: #9ca3af;
  line-height: 1.3;
}

.augment-relations .relations__row {
  margin-bottom: 0.2rem;
}

/* Detail */

.detail-list {
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
  max-height: 300px;
  overflow-y: auto;
}

.detail-row {
  padding: 0.3rem 0.4rem;
  border-radius: 0.35rem;
  background: rgba(15, 23, 42, 0.5);
  border: 1px solid #1f2937;
}

.detail-row__header {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  font-size: 0.72rem;
}

.detail-row__relation {
  font-weight: 600;
  color: #e5e7eb;
}

.detail-row__score {
  color: #60a5fa;
}

.detail-row__type {
  padding: 0.05rem 0.3rem;
  border-radius: 3px;
  font-size: 0.6rem;
  background: rgba(99, 102, 241, 0.15);
  color: #818cf8;
}

.detail-row__ids {
  display: flex;
  align-items: center;
  gap: 0.3rem;
  margin-top: 0.1rem;
  font-size: 0.68rem;
}

.detail-row__ids code {
  color: #9ca3af;
  background: rgba(75, 85, 99, 0.2);
  padding: 0.05rem 0.3rem;
  border-radius: 3px;
}

.detail-row__arrow {
  color: #6b7280;
}

.detail-row__scores {
  display: flex;
  gap: 0.5rem;
  margin-top: 0.1rem;
  font-size: 0.62rem;
  color: #6b7280;
}

.augmented__pagination {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 0.5rem;
  margin-top: 0.3rem;
}

.augmented__page-btn {
  padding: 0.2rem 0.5rem;
  border: 1px solid #374151;
  border-radius: 0.35rem;
  background: #020617;
  color: #e5e7eb;
  font-size: 0.7rem;
  cursor: pointer;
}

.augmented__page-btn[disabled] {
  opacity: 0.5;
  cursor: default;
}

.augmented__page-info {
  font-size: 0.7rem;
  color: #9ca3af;
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
