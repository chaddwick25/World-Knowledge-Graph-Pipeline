/**
 * deckManager — module-level singleton owning the deck.gl DeckOverlay.
 *
 * DECKGL_VISUALIZATION_INTEGRATION_PLAN_V2.md §3.2.2. The DeckOverlay is an
 * L.Layer added to the Leaflet map (camera-synced by the bridge; Leaflet
 * keeps controller:false so it owns pan/zoom). Vue never touches deck
 * internals — WorldKGMap.vue calls attach/detach around the map lifecycle,
 * and the deck label store drives setLayers.
 *
 * Module-level state mirrors the layer-singleton style of WorldKGMap.vue
 * (mapInstance, geoJsonLayer, ...).
 */

import { DeckOverlay } from '@deck.gl-community/leaflet'
import { MapView } from '@deck.gl/core'

let deckOverlay = null
let mapInstance = null

export const deckManager = {
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
  },

  /** Proxy to deckOverlay.setProps({layers}). No-op when not attached. */
  setLayers(layers) {
    if (!deckOverlay) return
    deckOverlay.setProps({ layers })
  },

  /** Proxy to deckOverlay.setProps({layerFilter}). No-op when not attached. */
  setLayerFilter(filterFn) {
    if (!deckOverlay) return
    deckOverlay.setProps({ layerFilter: filterFn })
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
