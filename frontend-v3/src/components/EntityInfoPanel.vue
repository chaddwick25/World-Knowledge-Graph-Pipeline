/**
 * EntityInfoPanel — dark-theme info card for the "focused entity".
 *
 * Rendered by WorldKGMap when entityInfoStore has an entity. Set by two
 * triggers: a marker click on the map, or hovering a result name in the
 * results table (SemanticSearchPanel) — both write the same store, so the
 * same panel appears "as if the marker was clicked".
 *
 * Positioning: a Google-Maps-style popover anchored to the entity's map
 * point (WorldKGMap passes the container-pixel position via the `position`
 * prop, refreshed on pan/zoom). Flips below the point near the top edge;
 * falls back to the bottom-left corner when the entity has no coordinates.
 *
 * Handles two entity shapes: full OSM results (osm_type/osm_id/tags/
 * wkg_class/scores/distance_m) and geocoded anchors (name/lat/lon).
 */

<template>
  <div
    v-if="store.hasEntity"
    class="entity-info-panel"
    :class="{ 'entity-info-panel--below': flipBelow }"
    :style="panelStyle"
  >
    <div class="entity-info-panel__header">
      <span class="entity-info-panel__title">{{ title }}</span>
      <button
        class="btn btn-sm btn-outline-secondary entity-info-panel__close py-0 px-1"
        @click="store.clear()"
      >×</button>
    </div>
    <div class="entity-info-panel__body">
      <div v-if="wkgClass" class="entity-info-panel__row">
        <span class="entity-info-panel__label">Class</span>
        <span class="entity-info-panel__value">{{ wkgClass.replace(/^wkgs:/, '') }}</span>
      </div>
      <div v-if="distanceM != null" class="entity-info-panel__row">
        <span class="entity-info-panel__label">Distance</span>
        <span class="entity-info-panel__value">{{ formatDistance(distanceM) }}</span>
      </div>
      <div v-if="score != null" class="entity-info-panel__row">
        <span class="entity-info-panel__label">Score</span>
        <span class="entity-info-panel__value">{{ Number(score).toFixed(3) }}</span>
      </div>
      <div v-if="entity.relation" class="entity-info-panel__row">
        <span class="entity-info-panel__label">Relation</span>
        <span class="entity-info-panel__value">{{ entity.relation }}</span>
      </div>
      <div v-if="entity.osm_type && entity.osm_id != null" class="entity-info-panel__row">
        <span class="entity-info-panel__label">OSM</span>
        <span class="entity-info-panel__value">
          <a
            :href="`https://www.openstreetmap.org/${entity.osm_type}/${entity.osm_id}`"
            target="_blank"
          >{{ entity.osm_type }}/{{ entity.osm_id }}</a>
        </span>
      </div>
      <div v-if="tagRows.length" class="entity-info-panel__tags">
        <span
          v-for="(tag, idx) in tagRows"
          :key="idx"
          class="entity-info-panel__tag"
        >{{ tag }}</span>
      </div>
    </div>
  </div>
</template>

<script>
import { useEntityInfoStore } from '../stores/entityInfoStore'

const TAG_LIMIT = 8

export default {
  name: 'EntityInfoPanel',
  props: {
    // Container-pixel {x, y} of the entity's map point (WorldKGMap).
    position: {
      type: Object,
      default: null,
    },
  },
  setup() {
    const store = useEntityInfoStore()
    return { store }
  },
  computed: {
    flipBelow() {
      return this.position && this.position.y != null && this.position.y < 180
    },
    /** Popover anchored to the map point, flipping below near the top
     *  edge; bottom-left corner when the entity has no coordinates. */
    panelStyle() {
      if (this.position && this.position.x != null && this.position.y != null) {
        const transform = this.flipBelow
          ? 'translate(-50%, 14px)'
          : 'translate(-50%, calc(-100% - 14px))'
        return {
          left: `${this.position.x}px`,
          top: `${this.position.y}px`,
          transform,
        }
      }
      return { bottom: '18px', left: '18px' }
    },
    entity() {
      return this.store.entity || {}
    },
    title() {
      return this.entity.name || this.entity.osm_id || 'Entity'
    },
    wkgClass() {
      return this.entity.wkg_class || this.entity.wkgClass || null
    },
    distanceM() {
      return this.entity.distance_m != null ? this.entity.distance_m : this.entity.distanceM
    },
    score() {
      return (
        this.entity.scores?.final_score ??
        this.entity.score ??
        this.entity.normalized_score ??
        null
      )
    },
    tagRows() {
      const tags = this.entity.tags || {}
      return Object.entries(tags)
        .slice(0, TAG_LIMIT)
        .map(([k, v]) => `${k}=${v}`)
    },
  },
  methods: {
    formatDistance(m) {
      if (m == null) return ''
      if (m >= 1000) return `${(m / 1000).toFixed(1)} km`
      return `${Math.round(m)} m`
    },
  },
}
</script>

<style scoped>
.entity-info-panel {
  position: absolute;
  z-index: 1050; /* above Leaflet's control container (1000) and the deck canvas */
  min-width: 190px;
  max-width: 260px;
  background: rgba(15, 23, 42, 0.94);
  border: 1px solid #1f2937;
  border-radius: 8px;
  padding: 0.5rem 0.6rem;
  backdrop-filter: blur(8px);
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.35);
}

/* Caret pointing at the map point (down when above, up when flipped below). */
.entity-info-panel::after {
  content: '';
  position: absolute;
  left: 50%;
  bottom: -6px;
  width: 10px;
  height: 10px;
  background: rgba(15, 23, 42, 0.94);
  border-right: 1px solid #1f2937;
  border-bottom: 1px solid #1f2937;
  transform: translateX(-50%) rotate(45deg);
  border-radius: 0 0 2px 0;
}

.entity-info-panel--below::after {
  top: -6px;
  bottom: auto;
  border: none;
  border-left: 1px solid #1f2937;
  border-top: 1px solid #1f2937;
  border-radius: 2px 0 0 0;
}

.entity-info-panel__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
  margin-bottom: 0.35rem;
}

.entity-info-panel__title {
  font-size: 0.78rem;
  font-weight: 600;
  color: var(--bs-meta-strong);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.entity-info-panel__close {
  font-size: 0.75rem;
  line-height: 1;
  flex-shrink: 0;
}

.entity-info-panel__body {
  display: flex;
  flex-direction: column;
  gap: 0.2rem;
}

.entity-info-panel__row {
  display: flex;
  justify-content: space-between;
  gap: 0.5rem;
  font-size: 0.68rem;
}

.entity-info-panel__label {
  color: var(--bs-meta-color);
}

.entity-info-panel__value {
  color: var(--bs-readout-color);
  font-variant-numeric: tabular-nums;
  text-align: right;
}

.entity-info-panel__value a {
  color: var(--bs-info-text-emphasis);
}

.entity-info-panel__tags {
  display: flex;
  flex-wrap: wrap;
  gap: 0.2rem;
  margin-top: 0.25rem;
}

.entity-info-panel__tag {
  font-family: var(--bs-font-monospace, ui-monospace, SFMono-Regular, Menlo, Consolas, monospace);
  font-size: 0.62rem;
  color: var(--bs-meta-color);
  background: rgba(148, 163, 184, 0.12);
  border-radius: 3px;
  padding: 0.05rem 0.3rem;
}
</style>
