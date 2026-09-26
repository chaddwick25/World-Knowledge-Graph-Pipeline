<template>
  <div class="map-wrapper">
    <div ref="mapContainer" class="map-wrapper__map"></div>

    <div v-if="isLoading" class="map-wrapper__overlay">
      <div class="map-wrapper__spinner"></div>
      <p>Loading countries...</p>
    </div>

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
import { labelManager } from '../mapLabels/labelManager'
import { createClassLabels, createEntityLabels } from '../mapLabels/textLayerConfig'
import {
  RELATION_COLORS,
  getRelationColor,
  useMapOverlayLayers,
} from '../composables/useMapOverlayLayers'

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
let geoJsonLayer = null
let searchMarkersLayer = null
let countryLookup = {}
let layerLookup = {}

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
  data() {
    return {
      isLoading: true,
      overlayStore: null,
      overlayLayers: null,
      overlayUnsub: null,
      mapLabelsStore: null,
      mapLabelsUnsub: null,
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
    this.overlayLayers = useMapOverlayLayers()
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
    if (mapInstance) {
      labelManager.detach()
      mapInstance.remove()
      mapInstance = null
      searchMarkersLayer = null
      this.overlayLayers?.detach()
    }
  },
  watch: {
    selectedIds: {
      handler(newIds) {
        this.refreshStyles()
        // Sidebar selection: frame the country. Map-polygon clicks already
        // fly in handleClick — skip the double flight via _flyingFromClick.
        if (newIds.length === 1 && !this._flyingFromClick) {
          const layer = layerLookup[newIds[0]]
          if (layer && mapInstance) {
            mapInstance.flyToBounds(layer.getBounds(), {
              padding: [100, 100],
              maxZoom: 14,
              duration: 0.8,
            })
          }
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

      searchMarkersLayer = L.layerGroup().addTo(mapInstance)
      // Overlay layer groups (query graph / agent overlay / augmented links)
      // live in the useMapOverlayLayers composable (monolith split, Phase 5).
      this.overlayLayers.attach(mapInstance)

      // deck.gl DeckOverlay (Map Labels) — added last so it sits in the
      // overlay pane below the Leaflet layer groups above (markers/popups
      // stay on top).
      labelManager.attach(mapInstance)
      // Sync insurance: the bridge re-syncs on its own moveend/zoomend
      // handlers; this covers any Leaflet path that fires neither.
      mapInstance.on('moveend zoomend viewreset', () => labelManager.refresh())
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
      labelManager.setLayers(layers)
    },

    renderCountries(countries) {
      if (!mapInstance) return

      if (geoJsonLayer) {
        mapInstance.removeLayer(geoJsonLayer)
      }

      const features = []
      countryLookup = {}
      layerLookup = {}

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

      geoJsonLayer = L.geoJSON(
        { type: 'FeatureCollection', features },
        {
          style: (feature) => this.countryStyle(feature),
          onEachFeature: (feature, layer) => {
            layerLookup[feature.properties.id] = layer
            layer.on({
              click: () => {
                if (!feature.properties.is_geovectors_supported) return
                this.handleClick(feature.properties.id)
              },
              mouseover: (e) => {
                if (!feature.properties.is_geovectors_supported) return
                this.handleMouseOver(e, feature.properties)
              },
              mouseout: (e) => {
                if (!feature.properties.is_geovectors_supported) return
                this.handleMouseOut(e)
              },
            })
          },
        }
      ).addTo(mapInstance)
    },

    countryStyle(feature) {
      const isSupported = feature.properties.is_geovectors_supported
      const isSelected = this.selectedIds.includes(feature.properties.id)

      if (!isSupported) {
        return {
          fillColor: 'transparent',
          fillOpacity: 0,
          color: 'transparent',
          weight: 0,
          opacity: 0,
        }
      }

      if (isSelected) {
        return {
          fillColor: '#6366f1',
          fillOpacity: 0.5,
          color: '#818cf8',
          weight: 3,
          opacity: 1,
        }
      }
      return {
        fillColor: '#1f2937',
        fillOpacity: 0.3,
        color: '#374151',
        weight: 1.5,
        opacity: 0.8,
      }
    },

    refreshStyles() {
      if (!geoJsonLayer) return
      geoJsonLayer.setStyle((feature) => this.countryStyle(feature))
    },

    renderSearchResults() {
      if (!mapInstance || !searchMarkersLayer) return
      searchMarkersLayer.clearLayers()

      // The anchor/entity graph supersedes the plain-circle fallback.
      if (this.queryGraph && (this.queryGraph.entities?.length || this.queryGraph.anchors?.length)) {
        return
      }

      if (!this.searchResults || !this.searchResults.length) return

      const bounds = []

      this.searchResults.forEach((result) => {
        const lat = result.geom && result.geom.lat
        const lon = result.geom && result.geom.lon
        if (lat == null || lon == null) return

        const name =
          (result.tags && result.tags.name) || `${result.osm_type} ${result.osm_id}`

        const marker = L.circleMarker([lat, lon], {
          radius: 6,
          color: '#f97316',
          fillColor: '#fed7aa',
          fillOpacity: 0.9,
          weight: 2,
        })

        marker.bindPopup(`<strong>${name}</strong>`)
        marker.addTo(searchMarkersLayer)
        bounds.push([lat, lon])
      })

      if (bounds.length) {
        mapInstance.fitBounds(bounds, { padding: [100, 100] })
      }
    },

    // ── Overlay layer rendering — delegated to useMapOverlayLayers ──
    renderQueryGraph() {
      this.overlayLayers.renderQueryGraph(this.queryGraph)
    },

    renderAgentOverlays() {
      this.overlayLayers.renderAgentOverlays(this.overlayStore)
    },

    renderAugmentedLinks() {
      this.overlayLayers.renderAugmentedLinks({
        augmentedLinks: this.augmentedLinks,
        showAcceptedLinks: this.showAcceptedLinks,
        showRejectedLinks: this.showRejectedLinks,
        visibleRelations: this.visibleRelations,
        hasSearchResults: !!(this.searchResults && this.searchResults.length),
      })
    },

    handleClick(countryId) {
      this._flyingFromClick = true
      this.$emit('country-toggled', { countryId })
      const layer = layerLookup[countryId]
      if (layer && mapInstance) {
        mapInstance.flyToBounds(layer.getBounds(), {
          padding: [100, 100],
          maxZoom: 14,
          duration: 0.8,
        })
      }
    },

    handleMouseOver(e, props) {
      const layer = e.target
      const isSelected = props && this.selectedIds.includes(props.id)
      layer.setStyle({
        fillOpacity: isSelected ? 0.6 : 0.4,
        weight: isSelected ? 3 : 2,
      })
      layer.bringToFront()

      layer
        .bindTooltip(
          `<div class="country-tooltip__name">${props.name}</div>` +
          `<div class="country-tooltip__continent">${props.continent}</div>`,
          { sticky: true, className: 'country-tooltip-leaflet' }
        )
        .openTooltip()
    },

    handleMouseOut(e) {
      const layer = e.target
      if (geoJsonLayer) {
        geoJsonLayer.resetStyle(layer)
      }
      layer.unbindTooltip()
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
  font-size: 0.6rem;
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
.country-tooltip-leaflet {
  background: rgba(15, 23, 42, 0.95);
  color: var(--bs-body-color);
  border: 1px solid var(--bs-border-color);
  border-radius: 6px;
  padding: 6px 10px;
  font-size: 0.85rem;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.2);
  line-height: 1.4;
}

.country-tooltip__name {
  color: var(--bs-readout-color);
  font-weight: 700;
  font-size: 0.85rem;
}

.country-tooltip__continent {
  color: var(--bs-meta-color);
  font-size: 0.62rem;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  margin-top: 1px;
}

.country-tooltip-leaflet::before {
  border-top-color: rgba(15, 23, 42, 0.95);
}
</style>
