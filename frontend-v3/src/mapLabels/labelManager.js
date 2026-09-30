/**
 * labelManager — module-level singleton owning the deck.gl DeckOverlay
 * for the Map Labels feature.
 *
 * DECKGL_VISUALIZATION_INTEGRATION_PLAN_V2.md §3.2.2. The DeckOverlay is an
 * L.Layer added to the Leaflet map (camera-synced by the bridge; Leaflet
 * keeps controller:false so it owns pan/zoom). Vue never touches deck
 * internals — WorldKGMap.vue calls attach/detach around the map lifecycle,
 * and the map labels store drives setLayers.
 *
 * Module-level state mirrors the layer-singleton style of WorldKGMap.vue
 * (mapInstance, geoJsonLayer, ...).
 */

import { DeckOverlay } from '@deck.gl-community/leaflet'
import { MapView } from '@deck.gl/core'

let deckOverlay = null
let mapInstance = null
const layerGroups = new Map()

function rebuildLayers() {
  if (!deckOverlay) return
  const mergedLayers = []
  for (const groupLayers of layerGroups.values()) {
    mergedLayers.push(...groupLayers)
  }
  deckOverlay.setProps({ layers: mergedLayers })
}

export const labelManager = {
  /**
   * Create the DeckOverlay and add it to the map. Safe to call again
   * (HMR/remount): an existing overlay is detached first.
   */
  attach(map) {
    if (!map) return
    if (deckOverlay && mapInstance === map) {
      return deckOverlay
    }
    if (deckOverlay) {
      this.detach()
    }
    mapInstance = map
    deckOverlay = new DeckOverlay({
      views: [new MapView({ repeat: true })],
      layers: [],
    })
    map.addLayer(deckOverlay)
    return deckOverlay
  },

  /** Remove the overlay and null module state (beforeUnmount slot). */
  detach() {
    if (deckOverlay && mapInstance) {
      mapInstance.removeLayer(deckOverlay)
    }
    deckOverlay = null
    mapInstance = null
    layerGroups.clear()
  },

  /** Proxy to deckOverlay.setProps({layers}). No-op when not attached. */
  setLayers(layers) {
    this.setLayerGroup('map-labels', layers || [])
  },

  /**
   * Set/replace one logical layer group. Groups are flattened in insertion
   * order so map labels and template-result overlays can coexist.
   */
  setLayerGroup(groupId, layers) {
    if (!groupId) return
    layerGroups.set(groupId, layers || [])
    rebuildLayers()
  },

  /** Remove one logical group from the deck overlay. */
  clearLayerGroup(groupId) {
    if (!groupId) return
    layerGroups.delete(groupId)
    rebuildLayers()
  },

  /** Proxy to deckOverlay.setProps({layerFilter}). No-op when not attached. */
  setLayerFilter(filterFn) {
    if (!deckOverlay) return
    deckOverlay.setProps({ layerFilter: filterFn })
  },

  /**
   * Wire interaction props (getTooltip / onClick / onHover) onto the
   * DeckOverlay. The community DeckOverlay supports Tooltip + onHover +
   * onClick; the callbacks fire for pickable layers (template results,
   * search results, query graph, agent overlays, countries). No-op when
   * not attached.
   */
  setInteraction({ getTooltip, onClick, onHover } = {}) {
    if (!deckOverlay) return
    deckOverlay.setProps({
      getTooltip: getTooltip || null,
      onClick: onClick || null,
      onHover: onHover || null,
    })
  },

  /**
   * Force the deck camera back in sync with Leaflet (Leaflet 256px world vs
   * deck 512px: deckZoom = leafletZoom - 1). The bridge does this on its own
   * moveend/zoomend handlers; this is idempotent insurance for any Leaflet
   * path that fires neither (e.g. programmatic view changes).
   */
  refresh() {
    if (!deckOverlay || !mapInstance) return
    const c = mapInstance.getCenter()
    deckOverlay.setProps({
      viewState: {
        longitude: c.lng,
        latitude: c.lat,
        zoom: mapInstance.getZoom() - 1,
        pitch: 0,
        bearing: 0,
      },
    })
  },

  /** Fit the Leaflet map to latlngs [[lat, lon], ...] — frames loaded labels
   *  so they're always on screen instead of the world view. */
  fitToBounds(latlngs, options) {
    if (!mapInstance || !latlngs || !latlngs.length) return
    mapInstance.fitBounds(latlngs, options || { padding: [60, 60], maxZoom: 10 })
  },

  isAttached() {
    return !!deckOverlay
  },

  /** The attached Leaflet map (null when detached). */
  getMap() {
    return mapInstance
  },
}
