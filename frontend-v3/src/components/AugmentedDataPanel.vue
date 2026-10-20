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

<style scoped src="../styles/augmented-data-panel.css"></style>
