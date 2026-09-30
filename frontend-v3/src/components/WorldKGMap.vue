<template>
  <div class="map-wrapper">
    <div ref="mapContainer" class="map-wrapper__map"></div>

    <div v-if="isLoading" class="map-wrapper__overlay">
      <div class="map-wrapper__spinner"></div>
      <p>Loading countries...</p>
    </div>

    <!-- Focused-entity info popover (marker click or results-table hover),
         anchored to the entity's map point -->
    <EntityInfoPanel :position="infoPanelPos" />

    <!-- Relation legend (only when augmented links are visible) -->
    <div v-if="activeRelationLegend.length" class="map-legend">
      <div class="map-legend__title">Link Types</div>
      <div class="map-legend__items">
        <div
          v-for="item in activeRelationLegend"
          :key="item.relation"
          class="map-legend__item"
        >
          <span
            class="map-legend__dot"
            :style="{ background: item.color }"
          ></span>
          <span class="map-legend__label">{{ item.relation }}</span>
        </div>
      </div>
      <div class="map-legend__hint">
        <span class="map-legend__line map-legend__line--solid"></span>
        accepted
        <span class="map-legend__line map-legend__line--dashed"></span>
        rejected
      </div>
    </div>
  </div>
</template>

<script>
/**
 * WorldKGMap
 *
 * Interactive Leaflet map of all WorldKG countries.
 * Emits selection-changed events for the parent pipeline controller.
 *
 * Props:
 *   selectedIds  - Array of currently selected country UUIDs
 *   searchResults - Array of spatial search results (for marker overlay)
 *   augmentedLinks - { accepted: [...], rejected: [...] } link geometries
 *   showAcceptedLinks - Boolean, render accepted link markers/lines
 *   showRejectedLinks - Boolean, render rejected link markers/lines
 *
 * Events:
 *   countries-loaded  - { countries[] } after fetch
 *   country-toggled    - { countryId } on click
 *   country-center     - { id, lat, lon } when selection updates
 *   error              - { error: string }
 */

import axios from 'axios'
import L from 'leaflet'
import 'leaflet/dist/leaflet.css'
import { useOverlayStore } from '../stores/overlayStore'
import { useMapLabelsStore } from '../stores/mapLabelsStore'
import { useEntityInfoStore } from '../stores/entityInfoStore'
import EntityInfoPanel from './EntityInfoPanel.vue'
import { labelManager } from '../mapLabels/labelManager'
import { createClassLabels, createEntityLabels } from '../mapLabels/textLayerConfig'
import { buildTemplateDeckLayers, isTemplateDeckSupported, templateVizBounds } from '../mapViz/templateDeckLayers'
import {
  RELATION_COLORS,
  buildCountryLayer,
  buildSearchResultLayer,
  buildQueryGraphLayers,
  buildAugmentedLinkLayers,
  buildAgentOverlayLayers,
  escapeHtml,
  getRelationColor,
} from '../mapViz/overlayDeckLayers'

// ── Basemap configuration ─────────────────────────────────────────────────
// CARTO basemaps now require an API key (free, request at
// https://carto.com/basemaps/apikey). When VITE_CARTO_API_KEY is set, the
// dark CARTO basemap is used with the key appended. When unset, we fall
// back to standard OSM raster tiles (light theme, no key required) so the
// map always renders.
const CARTO_API_KEY = import.meta.env.VITE_CARTO_API_KEY || ''
const USE_CARTO = CARTO_API_KEY.length > 0

const BASEMAP_URL = USE_CARTO
  ? `https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png?key=${CARTO_API_KEY}`
  : 'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png'

const BASEMAP_OPTIONS = USE_CARTO
  ? { subdomains: 'abcd', maxZoom: 19 }
  : { subdomains: 'abc', maxZoom: 19 }

const BASEMAP_ATTRIBUTION = USE_CARTO
  ? '© <a href="https://www.openstreetmap.org/copyright">OSM</a> · <a href="https://carto.com/">CARTO</a>'
  : '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'

// ── Deck.gl label zoom hierarchy (Phase 1c) ─────────────────────────────
// Leaflet zoom at which the label layers switch: class labels render below
// the threshold, entity labels at/above it. The layerFilter installed in
// initMap reads viewport.zoom (deck units = leaflet zoom - 1) and converts
// back, so this constant stays in Leaflet units.
const DECK_LABEL_ZOOM_THRESHOLD = 10

let mapInstance = null
let countryLookup = {}

export default {
  name: 'WorldKGMap',
  props: {
    selectedIds: {
      type: Array,
      default: () => [],
    },
    searchResults: {
      type: Array,
      default: () => [],
    },
    // Anchor/entity graph from SemanticSearchPanel:
    // { anchors: [{name, lat, lon}], entities: [{..., geom}],
    //   links: [{anchorIdx, entityIdx}], anchorLines: [{from, to, label}] }
    queryGraph: {
      type: Object,
      default: null,
    },
    // Deck.gl visualization payload for Kuhn-template question results.
    templateVisualization: {
      type: Object,
      default: null,
    },
    augmentedLinks: {
      type: Object,
      default: null,
    },
    showAcceptedLinks: {
      type: Boolean,
      default: false,
    },
    showRejectedLinks: {
      type: Boolean,
      default: false,
    },
    visibleRelations: {
      type: Array,
      default: null,
    },
  },
  emits: ['countries-loaded', 'country-toggled', 'country-center', 'error'],
  components: { EntityInfoPanel },
  setup() {
    const entityInfoStore = useEntityInfoStore()
    return { entityInfoStore }
  },
  data() {
    return {
      isLoading: true,
      overlayStore: null,
      overlayUnsub: null,
      mapLabelsStore: null,
      mapLabelsUnsub: null,
      entityInfoUnsub: null,
      // Container-pixel position of the focused entity (EntityInfoPanel popover).
      infoPanelPos: null,
    }
  },
  computed: {
    // Relations currently visible on the map — drives the legend.
    // Ordered by the RELATION_COLORS key sequence (same as the panel).
    activeRelationLegend() {
      if (!this.augmentedLinks) return []
      const visibleSet = this.visibleRelations ? new Set(this.visibleRelations) : null
      const seen = new Set()
      const collect = (links) => {
        if (!links) return
        for (const link of links) {
          if (visibleSet && !visibleSet.has(link.relation)) continue
          seen.add(link.relation)
        }
      }
      if (this.showAcceptedLinks) collect(this.augmentedLinks.accepted)
      if (this.showRejectedLinks) collect(this.augmentedLinks.rejected)

      // Order by RELATION_COLORS key sequence; unknown relations last
      const order = Object.keys(RELATION_COLORS)
      return order
        .filter((r) => seen.has(r))
        .map((r) => ({ relation: r, color: getRelationColor(r) }))
        .concat(
          [...seen]
            .filter((r) => !order.includes(r))
            .map((r) => ({ relation: r, color: getRelationColor(r) }))
        )
    },
  },
  mounted() {
    this.initMap()
    this.loadCountries()
    // Agent overlays (MCP renderToolOverlay → overlayStore → this renderer)
    this.overlayStore = useOverlayStore()
    this.renderAgentOverlays()
    this.overlayUnsub = this.overlayStore.$subscribe(() => {
      this.renderAgentOverlays()
    })
    // Map Labels (mapLabelsStore → labelManager.setLayers → DeckOverlay)
    this.mapLabelsStore = useMapLabelsStore()
    this.renderMapLabels()
    this.mapLabelsUnsub = this.mapLabelsStore.$subscribe(() => {
      this.renderMapLabels()
    })
    this.renderTemplateVisualization()
    // Anchor the info popover to the entity's map point (marker click or
    // table hover) and keep it pinned across pan/zoom.
    this.entityInfoUnsub = this.entityInfoStore.$subscribe(() => {
      this.updateInfoPanelPos()
    })
    this.updateInfoPanelPos()
  },
  beforeUnmount() {
    if (this.overlayUnsub) {
      this.overlayUnsub()
      this.overlayUnsub = null
    }
    if (this.mapLabelsUnsub) {
      this.mapLabelsUnsub()
      this.mapLabelsUnsub = null
    }
    if (this.entityInfoUnsub) {
      this.entityInfoUnsub()
      this.entityInfoUnsub = null
    }
    if (mapInstance) {
      labelManager.detach()
      mapInstance.remove()
      mapInstance = null
      this.entityInfoStore.clear()
    }
  },
  watch: {
    selectedIds: {
      handler(newIds) {
        // Sidebar selection: frame the country (map clicks fly in
        // handleClick — skip the double flight via _flyingFromClick).
        if (newIds.length === 1 && !this._flyingFromClick) {
          this.flyToCountry(newIds[0])
        }
        this._flyingFromClick = false
      },
      deep: true,
    },
    searchResults: {
      handler() {
        this.renderSearchResults()
      },
      deep: true,
    },
    queryGraph: {
      handler() {
        this.renderQueryGraph()
      },
      deep: true,
    },
    templateVisualization: {
      handler() {
        this.renderTemplateVisualization()
      },
      deep: true,
    },
    augmentedLinks: {
      handler() {
        this.renderAugmentedLinks()
      },
      deep: true,
    },
    showAcceptedLinks() {
      this.renderAugmentedLinks()
    },
    showRejectedLinks() {
      this.renderAugmentedLinks()
    },
    visibleRelations() {
      this.renderAugmentedLinks()
    },
  },
  methods: {
    async loadCountries() {
      try {
        this.isLoading = true
        const response = await axios.get('/recipes/regions-map-data/')
        const countries = response.data.countries || []
        this.$emit('countries-loaded', { countries })
        this.renderCountries(countries)
      } catch (error) {
        console.error('Failed to load countries:', error)
        this.$emit('error', { error: 'Failed to load map data' })
      } finally {
        this.isLoading = false
      }
    },

    initMap() {
      mapInstance = L.map(this.$refs.mapContainer, {
        center: [20, 0],
        zoom: 2.5,
        minZoom: 2,
        maxZoom: 18,
        zoomControl: true,
        attributionControl: false,
        worldCopyJump: false,
        maxBounds: [[-90, -180], [90, 180]],
        maxBoundsViscosity: 1.0,
      })

      L.tileLayer(BASEMAP_URL, BASEMAP_OPTIONS).addTo(mapInstance)

      L.control
        .attribution({ prefix: false, position: 'bottomright' })
        .addAttribution(BASEMAP_ATTRIBUTION)
        .addTo(mapInstance)

      // deck.gl DeckOverlay — the single overlay surface for countries,
      // map labels, template results, search results, query graph, USLP
      // links, and agent overlays (all registered as layer groups). Deck
      // owns all map interaction (countries included), so there are no
      // interactive Leaflet layers above the canvas.
      labelManager.attach(mapInstance)
      // Sync insurance: the bridge re-syncs on its own moveend/zoomend
      // handlers; this covers any Leaflet path that fires neither.
      mapInstance.on('moveend zoomend viewreset', () => labelManager.refresh())
      // Keep the info popover pinned to the entity's point across pan/zoom.
      mapInstance.on('moveend zoomend', () => this.updateInfoPanelPos())
      // Zoom hierarchy (Phase 1c): layerFilter re-evaluates every render,
      // so a single install tracks zoom — no per-event setProps needed.
      labelManager.setLayerFilter(({ layer, viewport }) => {
        const leafletZoom = viewport.zoom + 1
        if (layer.id === 'wkg-class-labels') {
          return leafletZoom < DECK_LABEL_ZOOM_THRESHOLD
        }
        if (layer.id === 'entity-tag-labels') {
          return leafletZoom >= DECK_LABEL_ZOOM_THRESHOLD
        }
        return true
      })

      // Hover tooltip + click info for every pickable deck layer (template
      // results, search results, query graph, USLP endpoints, agent
      // overlays). Click sets the same entityInfoStore the results-table
      // hover writes, so both show the identical info panel.
      labelManager.setInteraction({
        getTooltip: ({ object }) => {
          // Country features (the pickable base layer).
          if (object?.properties?.id) {
            return object.properties.is_geovectors_supported
              ? {
                  html: `<div>${escapeHtml(object.properties.name)}</div>` +
                    `<div class="text-secondary">${escapeHtml(object.properties.continent)}</div>`,
                }
              : null
          }
          // Entity rows (USLP endpoints carry the entity under `.info`).
          const obj = object?.info || object
          if (!obj || !obj.name) return null
          const lines = [String(obj.name)]
          const cls = obj.wkg_class || obj.wkgClass || null
          if (cls) lines.push(cls.replace(/^wkgs:/, ''))
          const dist = obj.distance_m != null ? obj.distance_m : obj.distanceM
          if (dist != null) {
            lines.push(dist >= 1000 ? `${(dist / 1000).toFixed(1)} km` : `${Math.round(dist)} m`)
          }
          return { html: lines.map((l) => `<div>${escapeHtml(l)}</div>`).join('') }
        },
        onClick: (info) => {
          console.log('[WorldKGMap onClick]', info.object && info.object.properties?.id, info.object && info.object.name, info.x, info.y)
          const obj = info.object?.info || info.object
          if (obj?.properties?.id) {
            // Country click → always select (deselect via the sidebar Clear).
            if (obj.properties.is_geovectors_supported) {
              this.handleClick(obj.properties.id)
            }
            return
          }
          if (obj) {
            // Marker click → open the info popover and keep it open until
            // dismissed (the × button or another marker click).
            this.entityInfoStore.setEntity(obj, 'map')
          }
        },
      })
    },

    // ── Map Labels layers (mapLabelsStore → labelManager) ────────────────
    // Rebuilds the TextLayers from store state (data + size/limit settings
    // from MapLabelsControls). Empty stores produce no layers, so the deck
    // canvas stays transparent until labels exist.
    renderMapLabels() {
      if (!this.mapLabelsStore) return
      const layers = []
      if (this.mapLabelsStore.classLabels.length) {
        layers.push(createClassLabels(this.mapLabelsStore.classLabels, {
          limit: this.mapLabelsStore.classLimit,
          sizeScale: this.mapLabelsStore.classSizeScale,
        }))
      }
      if (this.mapLabelsStore.entityLabels.length) {
        layers.push(createEntityLabels(this.mapLabelsStore.entityLabels, {
          limit: this.mapLabelsStore.entityLimit,
          sizeScale: this.mapLabelsStore.entitySizeScale,
        }))
      }
      labelManager.setLayerGroup('map-labels', layers)
    },

    renderTemplateVisualization() {
      const viz = this.templateVisualization
      const isSupported = isTemplateDeckSupported(viz?.template)
      if (!viz || !isSupported) {
        labelManager.clearLayerGroup('template-results')
        this.renderQueryGraph()
        this.renderSearchResults()
        return
      }

      const layers = buildTemplateDeckLayers(viz)
      labelManager.setLayerGroup('template-results', layers)
      labelManager.clearLayerGroup('query-graph')
      labelManager.clearLayerGroup('search-results')
      const bounds = templateVizBounds(viz)
      if (bounds.length && mapInstance) {
        mapInstance.fitBounds(bounds, { padding: [90, 90], maxZoom: 14 })
      }
    },

    renderCountries(countries) {
      if (!mapInstance) return

      const features = []
      countryLookup = {}

      countries.forEach((country) => {
        if (!country.geometry) return
        countryLookup[country.id] = country
        features.push({
          type: 'Feature',
          geometry: country.geometry,
          properties: {
            id: country.id,
            name: country.name,
            continent: country.continent,
            is_geovectors_supported: country.is_geovectors_supported,
          },
        })
      })

      // Countries as a pickable deck layer — deck owns all map interaction,
      // so there are no interactive Leaflet layers above the canvas (the
      // Leaflet GeoJSON layer and its style/refresh methods were removed
      // 2026-09-28).
      labelManager.setLayerGroup('countries', [buildCountryLayer(features)])
    },

    renderSearchResults() {
      // The anchor/entity graph and template deck viz supersede the plain
      // search-result dots.
      if (this.isTemplateDeckVizActive() || (
        this.queryGraph && (this.queryGraph.entities?.length || this.queryGraph.anchors?.length)
      )) {
        labelManager.clearLayerGroup('search-results')
        return
      }
      const results = this.searchResults || []
      labelManager.setLayerGroup('search-results', buildSearchResultLayer(results))
      const bounds = results
        .filter((r) => r.geom && r.geom.lat != null)
        .map((r) => [r.geom.lat, r.geom.lon])
      if (bounds.length && mapInstance) {
        mapInstance.fitBounds(bounds, { padding: [100, 100] })
      }
    },

    // ── Overlay rendering — deck layer groups on the shared DeckOverlay ──
    renderQueryGraph() {
      if (this.isTemplateDeckVizActive() || !this.queryGraph) {
        labelManager.clearLayerGroup('query-graph')
        return
      }
      labelManager.setLayerGroup('query-graph', buildQueryGraphLayers(this.queryGraph))
      const bounds = []
      for (const a of this.queryGraph.anchors || []) {
        if (a.lat != null) bounds.push([a.lat, a.lon])
      }
      for (const e of this.queryGraph.entities || []) {
        if (e.geom?.lat != null) bounds.push([e.geom.lat, e.geom.lon])
      }
      if (bounds.length && mapInstance) {
        mapInstance.fitBounds(bounds, { padding: [60, 60], maxZoom: 14 })
      }
    },

    renderAgentOverlays() {
      labelManager.setLayerGroup(
        'agent-overlay',
        buildAgentOverlayLayers(this.overlayStore?.overlays || []),
      )
    },

    renderAugmentedLinks() {
      labelManager.setLayerGroup('uslp-links', buildAugmentedLinkLayers({
        accepted: this.augmentedLinks?.accepted || [],
        rejected: this.augmentedLinks?.rejected || [],
        showAcceptedLinks: this.showAcceptedLinks,
        showRejectedLinks: this.showRejectedLinks,
        visibleRelations: this.visibleRelations,
      }))
    },

    handleClick(countryId) {
      this._flyingFromClick = true
      // Always select (never toggle-off) — deselect happens via the sidebar
      // Clear button. Map clicks on a selected country are a no-op instead
      // of silently deselecting.
      this.$emit('country-toggled', { countryId, selectOnly: true })
      this.flyToCountry(countryId)
    },

    /** Frame a country by its geometry bounds (deck countries have no
     *  Leaflet layer to getBounds() from). */
    flyToCountry(countryId) {
      if (!mapInstance) return
      const bounds = this.geometryBounds(countryLookup[countryId]?.geometry)
      if (bounds) {
        mapInstance.flyToBounds(bounds, {
          padding: [100, 100],
          maxZoom: 14,
          duration: 0.8,
        })
      }
    },

    /** Leaflet bounds [[south, west], [north, east]] from a GeoJSON
     *  Polygon/MultiPolygon, or null. Pure geometry math. */
    geometryBounds(geometry) {
      if (!geometry) return null
      const rings = geometry.type === 'Polygon'
        ? geometry.coordinates
        : geometry.type === 'MultiPolygon'
          ? geometry.coordinates.flat()
          : []
      let minLat = Infinity
      let maxLat = -Infinity
      let minLon = Infinity
      let maxLon = -Infinity
      for (const ring of rings) {
        for (const [lon, lat] of ring) {
          if (lat < minLat) minLat = lat
          if (lat > maxLat) maxLat = lat
          if (lon < minLon) minLon = lon
          if (lon > maxLon) maxLon = lon
        }
      }
      if (minLat === Infinity) return null
      return [[minLat, minLon], [maxLat, maxLon]]
    },

    isTemplateDeckVizActive() {
      return isTemplateDeckSupported(this.templateVisualization?.template)
    },

    /** {lat, lon} for the focused entity, across all shapes (normalized
     *  results with geom, anchors with lat/lon, deck rows with position). */
    extractEntityLatLon(entity) {
      if (!entity) return null
      const lat = entity.geom?.lat ?? entity.lat ?? (Array.isArray(entity.position) ? entity.position[1] : null)
      const lon = entity.geom?.lon ?? entity.lon ?? (Array.isArray(entity.position) ? entity.position[0] : null)
      if (lat == null || lon == null) return null
      return { lat, lon }
    },

    /** Reposition the info popover to the focused entity's map point
     *  (container pixels); null when no entity or no coordinates. */
    updateInfoPanelPos() {
      const coords = this.extractEntityLatLon(this.entityInfoStore.entity)
      if (!coords || !mapInstance) {
        this.infoPanelPos = null
        return
      }
      const pt = mapInstance.latLngToContainerPoint([coords.lat, coords.lon])
      this.infoPanelPos = { x: pt.x, y: pt.y }
    },
  },
}
</script>

<style scoped>
.map-wrapper {
  position: relative;
  width: 100%;
  height: 100%;
  min-height: 520px;
  border-radius: 12px;
  overflow: hidden;
  box-shadow: 0 4px 24px rgba(0, 0, 0, 0.5);
  border: 1px solid #1f2937;
}

.map-wrapper__map {
  width: 100%;
  height: 100%;
}

.map-wrapper__overlay {
  position: absolute;
  inset: 0;
  background: rgba(15, 23, 42, 0.92);
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 0.75rem;
  color: #e5e7eb;
  font-size: 0.9rem;
}

.map-wrapper__spinner {
  width: 28px;
  height: 28px;
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

/* ── Relation legend ── */
.map-legend {
  position: absolute;
  top: 12px;
  right: 12px;
  z-index: 1000;
  background: rgba(15, 23, 42, 0.92);
  border: 1px solid #1f2937;
  border-radius: 8px;
  padding: 0.5rem 0.65rem;
  backdrop-filter: blur(8px);
  max-width: 200px;
}

.map-legend__title {
  /* Micro-header: 11px uppercase with tracking. */
  font-size: 0.68rem;
  font-weight: 600;
  color: var(--bs-meta-color);
  text-transform: uppercase;
  letter-spacing: 0.05em;
  margin-bottom: 0.35rem;
}

.map-legend__items {
  display: flex;
  flex-direction: column;
  gap: 0.2rem;
  margin-bottom: 0.4rem;
}

.map-legend__item {
  display: flex;
  align-items: center;
  gap: 0.35rem;
}

.map-legend__dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}

.map-legend__label {
  font-size: 0.68rem;
  color: #d1d5db;
}

.map-legend__hint {
  display: flex;
  align-items: center;
  gap: 0.3rem;
  font-size: 0.62rem;
  color: #6b7280;
  border-top: 1px solid #1f2937;
  padding-top: 0.3rem;
}

.map-legend__line {
  display: inline-block;
  width: 16px;
  height: 2px;
  margin: 0 0.15rem;
}

.map-legend__line--solid {
  background: #6b7280;
}

.map-legend__line--dashed {
  border-top: 2px dashed #6b7280;
  height: 0;
}
</style>

<style>
/* (Dead code removed 2026-09-28: the Leaflet country hover tooltip styles
   were dropped when countries moved onto the pickable deck layer — the
   deck getTooltip renders the country name/continent now.) */
</style>
