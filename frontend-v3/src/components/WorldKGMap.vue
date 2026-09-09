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

// ── Relation type color map ──────────────────────────────────────────────
// Harmonious palette (Tailwind 400-500 range) that complements the app's
// dark theme (#0f172a backgrounds, #6366f1 indigo accent).
// Accepted links use solid lines at full opacity; rejected links use
// dashed lines at reduced opacity — both in the relation's color.
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
const DEFAULT_RELATION_COLOR = '#6b7280'  // gray-500 for unknown relations

// ── Agent overlay tool colors (docs/plans/MCP_AGENT_MVP_PLAN.md §9.2) ──
const TOOL_COLORS = {
  structuredSearch: '#3b82f6',  // blue
  nameSearch: '#ef4444',        // red
  templateQuery: '#10b981',     // green
}

function getRelationColor(relation) {
  return RELATION_COLORS[relation] || DEFAULT_RELATION_COLOR
}

// ── Query-graph (anchor/entity) markers ─────────────────────────────────
// Bootstrap Icons in a colored circular badge. Inline styles because
// Leaflet-injected divIcon HTML is outside the component's scoped DOM.
function makeQueryIcon({ bg, color, iconClass, size }) {
  const s = size || 26
  return L.divIcon({
    className: 'worldkg-query-icon',
    html:
      `<div style="width:${s}px;height:${s}px;border-radius:50%;background:${bg};` +
      `display:flex;align-items:center;justify-content:center;border:2px solid #fff;` +
      `box-shadow:0 1px 5px rgba(0,0,0,.5);color:${color};font-size:${Math.round(s * 0.6)}px">` +
      `<i class="bi ${iconClass}"></i></div>`,
    iconSize: [s, s],
    iconAnchor: [s / 2, s / 2],
  })
}
const ANCHOR_ICON = makeQueryIcon({ bg: '#6366f1', color: '#fff', iconClass: 'bi-geo-alt-fill', size: 30 })
const ENTITY_ICON = makeQueryIcon({ bg: '#f97316', color: '#fff', iconClass: 'bi-building', size: 24 })

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;')
}

let mapInstance = null
let geoJsonLayer = null
let searchMarkersLayer = null
let queryGraphLayer = null
let agentOverlayLayer = null
let augmentedAcceptedLayer = null
let augmentedRejectedLayer = null
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
    //   links: [{anchorIdx, entityIdx}], showAnchors, showEntities, showLinks }
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
      overlayUnsub: null,
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
  },
  beforeUnmount() {
    if (this.overlayUnsub) {
      this.overlayUnsub()
      this.overlayUnsub = null
    }
    if (mapInstance) {
      mapInstance.remove()
      mapInstance = null
      searchMarkersLayer = null
      queryGraphLayer = null
      agentOverlayLayer = null
      augmentedAcceptedLayer = null
      augmentedRejectedLayer = null
    }
  },
  watch: {
    selectedIds: {
      handler() {
        this.refreshStyles()
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
      queryGraphLayer = L.layerGroup().addTo(mapInstance)
      agentOverlayLayer = L.layerGroup().addTo(mapInstance)
      augmentedAcceptedLayer = L.layerGroup().addTo(mapInstance)
      augmentedRejectedLayer = L.layerGroup().addTo(mapInstance)
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

    // ── Anchor/entity graph rendering ──────────────────────────────────
    // Anchors (named query locations, indigo pins) + entities (top-k
    // results, orange building icons) + dashed links entity→nearest anchor.
    renderQueryGraph() {
      if (!mapInstance || !queryGraphLayer) return
      queryGraphLayer.clearLayers()

      const graph = this.queryGraph
      if (!graph) return

      const bounds = []

      if (graph.showAnchors !== false) {
        for (const a of graph.anchors || []) {
          if (a.lat == null || a.lon == null) continue
          const marker = L.marker([a.lat, a.lon], { icon: ANCHOR_ICON })
          marker.bindPopup(
            `<strong>${escapeHtml(a.name || 'Anchor')}</strong><br/>` +
            `<span class="text-secondary">anchor</span><br/>` +
            `${Number(a.lat).toFixed(4)}, ${Number(a.lon).toFixed(4)}`
          )
          marker.addTo(queryGraphLayer)
          bounds.push([a.lat, a.lon])
        }
      }

      if (graph.showEntities !== false) {
        for (const e of graph.entities || []) {
          if (!e.geom || e.geom.lat == null) continue
          const marker = L.marker([e.geom.lat, e.geom.lon], { icon: ENTITY_ICON })
          const popup = [
            `<strong>${escapeHtml(e.tags?.name || e.name || `${e.osm_type} ${e.osm_id}`)}</strong>`,
          ]
          if (e.wkg_class) popup.push(`<span>${escapeHtml(e.wkg_class)}</span>`)
          if (e.scores?.final_score != null) {
            popup.push(`<span>score: ${Number(e.scores.final_score).toFixed(3)}</span>`)
          }
          if (e.distance_m != null) {
            popup.push(`<span>${Math.round(e.distance_m)} m from anchor</span>`)
          }
          if (e.osm_type && e.osm_id) {
            popup.push(
              `<a href="https://www.openstreetmap.org/${e.osm_type}/${e.osm_id}" target="_blank">` +
              `OSM ${e.osm_type}/${e.osm_id}</a>`
            )
          }
          marker.bindPopup(popup.join('<br/>'))
          marker.addTo(queryGraphLayer)
          bounds.push([e.geom.lat, e.geom.lon])
        }
      }

      if (graph.showLinks !== false) {
        for (const link of graph.links || []) {
          const anchor = graph.anchors?.[link.anchorIdx]
          const entity = graph.entities?.[link.entityIdx]
          if (!anchor || anchor.lat == null || !entity?.geom) continue
          L.polyline(
            [[anchor.lat, anchor.lon], [entity.geom.lat, entity.geom.lon]],
            {
              color: '#a5b4fc',
              weight: 1.5,
              opacity: 0.55,
              dashArray: '4 4',
            }
          ).addTo(queryGraphLayer)
        }
      }

      if (bounds.length) {
        mapInstance.fitBounds(bounds, { padding: [60, 60], maxZoom: 14 })
      }
    },

    // ── Agent overlay rendering (MCP renderToolOverlay → overlayStore) ──
    normalizeOverlayEntities(overlay) {
      // Accept either a raw data-tool result or an explicit entities list.
      const raw = overlay.result || overlay.entities || []
      const source = Array.isArray(raw)
        ? raw
        : raw.results || (raw.result && raw.result.results) || []
      return source
        .map((e) => ({
          lat: e.lat != null ? e.lat : e.geom && e.geom.lat,
          lon: e.lon != null ? e.lon : e.geom && e.geom.lon,
          name:
            e.name ||
            (e.tags && e.tags.name) ||
            `${e.osm_type || 'osm'} ${e.osm_id || ''}`.trim(),
          wkgClass: e.wkg_class || e.wkgClass || null,
          score:
            e.score ??
            (e.scores && e.scores.final_score) ??
            e.diffusion_score ??
            null,
          distanceM: e.distance_m != null ? e.distance_m : e.distanceM,
        }))
        .filter((e) => e.lat != null && e.lon != null)
    },

    renderAgentOverlays() {
      if (!mapInstance || !agentOverlayLayer || !this.overlayStore) return
      agentOverlayLayer.clearLayers()

      const bounds = []
      for (const overlay of this.overlayStore.overlays) {
        const color = overlay.color || TOOL_COLORS[overlay.tool] || '#6366f1'
        const kind = overlay.kind || 'markers'
        const entities = this.normalizeOverlayEntities(overlay)
        const anchor = overlay.anchor || null

        for (const e of entities) {
          let marker
          if (kind === 'scaled-markers') {
            const score = e.score == null ? 0.5 : Math.min(Math.max(e.score, 0), 1)
            marker = L.circleMarker([e.lat, e.lon], {
              radius: 4 + Math.round(score * 12),
              color,
              fillColor: color,
              fillOpacity: 0.7,
              weight: 1.5,
            })
          } else {
            marker = L.circleMarker([e.lat, e.lon], {
              radius: 6,
              color,
              fillColor: color,
              fillOpacity: 0.75,
              weight: 2,
            })
          }
          const popup = [`<strong>${e.name || 'entity'}</strong>`]
          if (e.wkgClass) popup.push(`<span>${e.wkgClass}</span>`)
          if (e.score != null) popup.push(`<span>score: ${Number(e.score).toFixed(3)}</span>`)
          if (e.distanceM != null) popup.push(`<span>${Math.round(e.distanceM)} m</span>`)
          marker.bindPopup(popup.join('<br/>'))
          marker.addTo(agentOverlayLayer)
          bounds.push([e.lat, e.lon])
        }

        if (kind === 'radius' && anchor && overlay.radius != null) {
          L.circle([anchor.lat, anchor.lon], {
            radius: overlay.radius,
            color,
            fillColor: color,
            fillOpacity: 0.12,
            weight: 2,
          }).addTo(agentOverlayLayer)
          bounds.push([anchor.lat, anchor.lon])
        }

        if (kind === 'markers-line' && anchor) {
          for (const e of entities) {
            L.polyline(
              [
                [anchor.lat, anchor.lon],
                [e.lat, e.lon],
              ],
              { color, weight: 2, opacity: 0.7 },
            ).addTo(agentOverlayLayer)
          }
          bounds.push([anchor.lat, anchor.lon])
        }
      }

      if (bounds.length) {
        mapInstance.fitBounds(bounds, { padding: [80, 80] })
      }
    },

    renderAugmentedLinks() {
      if (!mapInstance) return
      if (augmentedAcceptedLayer) augmentedAcceptedLayer.clearLayers()
      if (augmentedRejectedLayer) augmentedRejectedLayer.clearLayers()

      if (!this.augmentedLinks) return

      const bounds = []
      const visibleSet = this.visibleRelations ? new Set(this.visibleRelations) : null
      const isVisible = (relation) => !visibleSet || visibleSet.has(relation)

      // Render accepted links — solid lines, filled markers, relation color
      if (this.showAcceptedLinks && this.augmentedLinks.accepted?.length) {
        this.augmentedLinks.accepted.forEach((link) => {
          if (!isVisible(link.relation)) return
          const hLat = link.head?.lat
          const hLon = link.head?.lon
          const tLat = link.tail?.lat
          const tLon = link.tail?.lon
          if (hLat == null || hLon == null || tLat == null || tLon == null) return

          const color = getRelationColor(link.relation)

          // Solid line connecting head → tail
          const line = L.polyline(
            [[hLat, hLon], [tLat, tLon]],
            {
              color: color,
              weight: 1.5,
              opacity: 0.6,
            }
          )
          line.bindPopup(
            `<strong>Accepted Link</strong><br>` +
            `Relation: ${link.relation}<br>` +
            `Score: ${(link.normalized_score * 100).toFixed(1)}%<br>` +
            `Head: ${link.head.osm_type}/${link.head.osm_id}<br>` +
            `Tail: ${link.tail.osm_type}/${link.tail.osm_id}`
          )
          line.addTo(augmentedAcceptedLayer)

          // Head marker — filled
          const headMarker = L.circleMarker([hLat, hLon], {
            radius: 4,
            color: color,
            fillColor: color,
            fillOpacity: 0.85,
            weight: 1.5,
          })
          headMarker.addTo(augmentedAcceptedLayer)
          bounds.push([hLat, hLon])

          // Tail marker — filled (lighter)
          const tailMarker = L.circleMarker([tLat, tLon], {
            radius: 4,
            color: color,
            fillColor: color,
            fillOpacity: 0.5,
            weight: 1.5,
          })
          tailMarker.addTo(augmentedAcceptedLayer)
          bounds.push([tLat, tLon])
        })
      }

      // Render rejected links — dashed lines, hollow markers, relation color
      if (this.showRejectedLinks && this.augmentedLinks.rejected?.length) {
        this.augmentedLinks.rejected.forEach((link) => {
          if (!isVisible(link.relation)) return
          const hLat = link.head?.lat
          const hLon = link.head?.lon
          const tLat = link.tail?.lat
          const tLon = link.tail?.lon
          if (hLat == null || hLon == null || tLat == null || tLon == null) return

          const color = getRelationColor(link.relation)

          // Dashed line connecting head → tail
          const line = L.polyline(
            [[hLat, hLon], [tLat, tLon]],
            {
              color: color,
              weight: 1.5,
              opacity: 0.4,
              dashArray: '4 3',
            }
          )
          line.bindPopup(
            `<strong>Rejected Link</strong><br>` +
            `Relation: ${link.relation}<br>` +
            `Score: ${(link.normalized_score * 100).toFixed(1)}%<br>` +
            `Head: ${link.head.osm_type}/${link.head.osm_id}<br>` +
            `Tail: ${link.tail.osm_type}/${link.tail.osm_id}`
          )
          line.addTo(augmentedRejectedLayer)

          // Head marker — hollow (outlined only)
          const headMarker = L.circleMarker([hLat, hLon], {
            radius: 4,
            color: color,
            fillColor: 'transparent',
            fillOpacity: 0,
            weight: 1.5,
          })
          headMarker.addTo(augmentedRejectedLayer)
          bounds.push([hLat, hLon])

          // Tail marker — hollow
          const tailMarker = L.circleMarker([tLat, tLon], {
            radius: 4,
            color: color,
            fillColor: 'transparent',
            fillOpacity: 0,
            weight: 1.5,
          })
          tailMarker.addTo(augmentedRejectedLayer)
          bounds.push([tLat, tLon])
        })
      }

      // Fit bounds to augmented links if we're showing them and no search results
      if (bounds.length && (!this.searchResults || !this.searchResults.length)) {
        mapInstance.fitBounds(bounds, { padding: [80, 80] })
      }
    },

    handleClick(countryId) {
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
          `<strong>${props.name}</strong><br><small>${props.continent}</small>`,
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
  font-size: 0.68rem;
  font-weight: 600;
  color: #9ca3af;
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
  color: #fff;
  border: none;
  border-radius: 6px;
  padding: 6px 10px;
  font-size: 0.85rem;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.2);
  line-height: 1.4;
}

.country-tooltip-leaflet::before {
  border-top-color: rgba(15, 23, 42, 0.95);
}
</style>
