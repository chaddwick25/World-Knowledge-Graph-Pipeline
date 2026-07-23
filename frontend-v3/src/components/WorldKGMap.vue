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
  },
  emits: ['countries-loaded', 'country-toggled', 'country-center', 'error'],
  data() {
    return {
      isLoading: true,
    }
  },
  mounted() {
    this.initMap()
    this.loadCountries()
  },
  beforeUnmount() {
    if (mapInstance) {
      mapInstance.remove()
      mapInstance = null
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
        maxZoom: 6,
        zoomControl: true,
        attributionControl: false,
        worldCopyJump: false,
        maxBounds: [[-90, -180], [90, 180]],
        maxBoundsViscosity: 1.0,
      })

      L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
        subdomains: 'abcd',
        maxZoom: 19,
      }).addTo(mapInstance)

      L.control
        .attribution({ prefix: false, position: 'bottomright' })
        .addAttribution(
          '© <a href="https://www.openstreetmap.org/copyright">OSM</a> · <a href="https://carto.com/">CARTO</a>'
        )
        .addTo(mapInstance)

      searchMarkersLayer = L.layerGroup().addTo(mapInstance)
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

    handleClick(countryId) {
      this.$emit('country-toggled', { countryId })
      const layer = layerLookup[countryId]
      if (layer && mapInstance) {
        mapInstance.flyToBounds(layer.getBounds(), {
          padding: [100, 100],
          maxZoom: 5,
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

<template>
  <div class="map-wrapper">
    <div ref="mapContainer" class="map-wrapper__map"></div>

    <div v-if="isLoading" class="map-wrapper__overlay">
      <div class="map-wrapper__spinner"></div>
      <p>Loading countries...</p>
    </div>
  </div>
</template>

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
