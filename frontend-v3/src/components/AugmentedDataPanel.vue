<script>
import axios from 'axios'

// ── Relation type color map (must match WorldKGMap.vue) ──────────────────
const RELATION_COLORS = {
  addrCity:        '#8b5cf6',  // violet-500
  addrPlace:       '#10b981',  // emerald-500
  addrNeighbour:   '#06b6d4',  // cyan-500
  addrSuburb:      '#84cc16',  // lime-500
  addrDistrict:    '#3b82f6',  // blue-500
  addrState:       '#ef4444',  // red-500
  addrProvince:    '#ec4899',  // pink-500
  addrHamlet:      '#f59e0b',  // amber-500
}
const DEFAULT_RELATION_COLOR = '#6b7280'

function getRelationColor(relation) {
  return RELATION_COLORS[relation] || DEFAULT_RELATION_COLOR
}

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
  emits: ['links-toggle'],
  data() {
    return {
      summary: null,
      loading: false,
      error: null,
      // Map visualization toggle state
      showAcceptedLinks: false,
      showRejectedLinks: false,
      // Per-relation visibility (null = all visible; a Set = only those in the set)
      visibleRelations: null,
      // Active subgraph (slug) — highlighted border
      activeSubgraph: null,
      // Link geometries for map rendering
      linkGeom: null,
      loadingLinks: false,
    }
  },
  computed: {
    // Relations ordered by the RELATION_COLORS key sequence (the default
    // distribution order), not by count. Unknown relations go last.
    sortedRelations() {
      if (!this.summary?.relation_distribution?.length) return []
      const order = Object.keys(RELATION_COLORS)
      return [...this.summary.relation_distribution].sort((a, b) => {
        const ia = order.indexOf(a.relation)
        const ib = order.indexOf(b.relation)
        return (ia === -1 ? 999 : ia) - (ib === -1 ? 999 : ib)
      })
    },
    maxRelationCount() {
      if (!this.sortedRelations.length) return 1
      return this.sortedRelations[0].count
    },
    acceptedRate() {
      if (!this.summary) return 0
      return (this.summary.acceptance_rate * 100).toFixed(1)
    },
  },
  watch: {
    countryName() {
      this.fetchSummary()
      this.fetchLinkGeom()
      // Reset toggles on country change
      this.showAcceptedLinks = false
      this.showRejectedLinks = false
      this.visibleRelations = null
    },
    snapshotDate() {
      this.fetchSummary()
      this.fetchLinkGeom()
    },
  },
  mounted() {
    if (this.countryName) {
      this.fetchSummary()
      this.fetchLinkGeom()
    }
  },
  beforeUnmount() {
    // Clear map links when leaving the Augmented tab
    this.$emit('links-toggle', { links: null, showAccepted: false, showRejected: false, visibleRelations: null })
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
    async fetchLinkGeom() {
      if (!this.countryName) return
      this.loadingLinks = true
      try {
        const encoded = encodeURIComponent(this.countryName)
        const params = { limit: 500 }
        if (this.snapshotDate) params.snapshot_date = this.snapshotDate
        const { data } = await axios.get(`/data/augmented-links-geom/${encoded}/`, { params })
        this.linkGeom = data
        // Re-emit with updated link data
        this.emitToggle()
      } catch (e) {
        console.error('Error fetching augmented link geometries:', e)
        this.linkGeom = null
      } finally {
        this.loadingLinks = false
      }
    },
    toggleAccepted() {
      this.showAcceptedLinks = !this.showAcceptedLinks
      this.emitToggle()
    },
    toggleRejected() {
      this.showRejectedLinks = !this.showRejectedLinks
      this.emitToggle()
    },
    toggleRelation(relation) {
      // null = all visible. On first toggle of any relation, initialize
      // the set with all relations visible, then toggle the clicked one.
      if (this.visibleRelations === null) {
        const all = new Set()
        if (this.summary?.relation_distribution) {
          for (const rel of this.summary.relation_distribution) {
            all.add(rel.relation)
          }
        }
        this.visibleRelations = all
      }
      if (this.visibleRelations.has(relation)) {
        this.visibleRelations.delete(relation)
      } else {
        this.visibleRelations.add(relation)
      }
      // Trigger reactivity — Set mutations don't trigger Vue 3 reactivity
      this.visibleRelations = new Set(this.visibleRelations)
      this.emitToggle()
    },
    isRelationVisible(relation) {
      if (this.visibleRelations === null) return true
      return this.visibleRelations.has(relation)
    },
    emitToggle() {
      this.$emit('links-toggle', {
        links: this.linkGeom,
        showAccepted: this.showAcceptedLinks,
        showRejected: this.showRejectedLinks,
        visibleRelations: this.visibleRelations ? Array.from(this.visibleRelations) : null,
      })
    },
    scoreBarWidth(count) {
      if (!this.summary?.score_distribution?.counts) return '0%'
      const max = Math.max(...this.summary.score_distribution.counts, 1)
      return `${(count / max) * 100}%`
    },
    relationColor(relation) {
      return getRelationColor(relation)
    },
    toggleSubgraph(slug) {
      this.activeSubgraph = this.activeSubgraph === slug ? null : slug
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
      <!-- Map visualization toggle buttons -->
      <div class="augmented__map-toggles">
        <button
          class="augmented__map-btn"
          :class="{ 'augmented__map-btn--active-accepted': showAcceptedLinks }"
          :disabled="loadingLinks || !linkGeom?.accepted?.length"
          @click="toggleAccepted"
        >
          <span class="augmented__map-btn-dot augmented__map-btn-dot--accepted"></span>
          Accepted
          <small v-if="linkGeom" class="augmented__map-btn-count">
            {{ linkGeom.returned_accepted }}/{{ linkGeom.total_accepted }}
          </small>
        </button>
        <button
          class="augmented__map-btn"
          :class="{ 'augmented__map-btn--active-rejected': showRejectedLinks }"
          :disabled="loadingLinks || !linkGeom?.rejected?.length"
          @click="toggleRejected"
        >
          <span class="augmented__map-btn-dot augmented__map-btn-dot--rejected"></span>
          Rejected
          <small v-if="linkGeom" class="augmented__map-btn-count">
            {{ linkGeom.returned_rejected }}/{{ linkGeom.total_rejected }}
          </small>
        </button>
      </div>
      <p v-if="loadingLinks" class="augmented__hint augmented__hint--small">
        Loading link geometries…
      </p>
      <p v-else-if="showAcceptedLinks || showRejectedLinks" class="augmented__hint augmented__hint--small">
        {{ showAcceptedLinks ? 'Accepted' : '' }}{{ showAcceptedLinks && showRejectedLinks ? ' & ' : '' }}{{ showRejectedLinks ? 'Rejected' : '' }}
        links shown on map{{ (showAcceptedLinks && linkGeom?.returned_accepted < linkGeom?.total_accepted) || (showRejectedLinks && linkGeom?.returned_rejected < linkGeom?.total_rejected) ? ' (top 500 by score)' : '' }}
      </p>

      <!-- ── Summary statistics (always visible) ── -->
      <div class="augmented__body">
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
              v-for="(rel, idx) in sortedRelations"
              :key="idx"
              class="relations__row"
              :class="{ 'relations__row--off': !isRelationVisible(rel.relation) }"
              @click="toggleRelation(rel.relation)"
            >
              <div class="relations__header">
                <span class="relations__name">
                  <span
                    class="relations__dot"
                    :style="{ background: relationColor(rel.relation) }"
                  ></span>
                  {{ rel.relation }}
                </span>
                <span class="relations__meta">
                  {{ rel.count.toLocaleString() }} ·
                  {{ (rel.acceptance_rate * 100).toFixed(0) }}%
                </span>
              </div>
              <div class="relations__bar-track">
                <div
                  class="relations__bar"
                  :style="{
                    width: `${(rel.accepted / rel.count) * 100}%`,
                    background: relationColor(rel.relation),
                  }"
                />
              </div>
            </div>
          </div>
          <p class="augmented__hint augmented__hint--small">
            Click a relation to toggle it on the map
          </p>
        </div>

        <!-- Subgraph groups -->
        <div v-if="summary.subgraph_groups?.length" class="augmented__section">
          <h4 class="augmented__section-title">Per Subgraph</h4>
          <div class="subgraphs">
            <button
              v-for="(sg, idx) in summary.subgraph_groups"
              :key="idx"
              class="subgraphs__card"
              :class="{ 'subgraphs__card--active': activeSubgraph === sg.subgraph_slug }"
              @click="toggleSubgraph(sg.subgraph_slug)"
            >
              <span class="subgraphs__name">{{ sg.subgraph_name }}</span>
              <span class="subgraphs__stats">
                <span class="subgraphs__stat subgraphs__stat--success">
                  {{ sg.accepted_count.toLocaleString() }} accepted
                </span>
                <span class="subgraphs__stat subgraphs__stat--danger">
                  {{ sg.rejected_count.toLocaleString() }} rejected
                </span>
              </span>
            </button>
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

.augmented__hint--small {
  font-size: 0.68rem;
  text-align: left;
  padding: 0 0.2rem;
}

.augmented__error {
  margin: 0;
  font-size: 0.78rem;
  color: #fecaca;
  padding: 0.3rem 0.4rem;
  background: rgba(239, 68, 68, 0.08);
  border-radius: 0.4rem;
}

/* ── Map toggle buttons ── */
.augmented__map-toggles {
  display: flex;
  gap: 0.4rem;
}

.augmented__map-btn {
  flex: 1;
  display: flex;
  align-items: center;
  gap: 0.3rem;
  padding: 0.35rem 0.5rem;
  border: 1px solid #1f2937;
  border-radius: 0.4rem;
  background: #020617;
  color: #9ca3af;
  font-size: 0.72rem;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.15s;
}

.augmented__map-btn:hover:not(:disabled) {
  border-color: #374151;
  color: #d1d5db;
}

.augmented__map-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.augmented__map-btn--active-accepted {
  border-color: #22c55e;
  background: rgba(34, 197, 94, 0.12);
  color: #86efac;
}

.augmented__map-btn--active-rejected {
  border-color: #ef4444;
  background: rgba(239, 68, 68, 0.12);
  color: #fca5a5;
}

.augmented__map-btn-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}

.augmented__map-btn-dot--accepted {
  background: #22c55e;
}

.augmented__map-btn-dot--rejected {
  background: #ef4444;
}

.augmented__map-btn-count {
  margin-left: auto;
  font-size: 0.62rem;
  color: #6b7280;
  font-weight: 400;
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
  padding: 0.2rem 0.3rem;
  border-radius: 0.3rem;
  cursor: pointer;
  transition: background 0.15s, opacity 0.15s;
}

.relations__row:hover {
  background: rgba(55, 65, 81, 0.3);
}

.relations__row--off {
  opacity: 0.35;
}

.relations__row--off .relations__dot {
  filter: grayscale(1);
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
  display: flex;
  align-items: center;
  gap: 0.3rem;
}

.relations__dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
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

/* Subgraphs */

.subgraphs {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
}

.subgraphs__card {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.4rem;
  width: 100%;
  padding: 0.4rem 0.5rem 0.4rem 0.55rem;
  border-radius: 0.4rem;
  background: rgba(15, 23, 42, 0.5);
  border: 1px solid #1f2937;
  cursor: pointer;
  transition: all 0.15s;
  font: inherit;
  text-align: left;
  position: relative;
}

.subgraphs__card:hover {
  border-color: #374151;
  background: rgba(31, 41, 55, 0.6);
}

.subgraphs__card--active {
  border-color: #6366f1;
  background: rgba(99, 102, 241, 0.2);
  box-shadow: inset 3px 0 0 #6366f1, 0 0 10px rgba(99, 102, 241, 0.35);
}

.subgraphs__card--active .subgraphs__name {
  color: #c7d2fe;
}

.subgraphs__name {
  font-size: 0.75rem;
  font-weight: 600;
  color: #e5e7eb;
}

.subgraphs__stats {
  display: flex;
  gap: 0.5rem;
  font-size: 0.68rem;
  flex-shrink: 0;
}

.subgraphs__stat--success {
  color: #22c55e;
}

.subgraphs__stat--danger {
  color: #ef4444;
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
