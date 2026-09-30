/**
 * entityInfoStore — the "focused entity" shared by the map and the results
 * table. Two writers, one reader: marker clicks (WorldKGMap) and result-
 * name hovers (SemanticSearchPanel) set the same entity; the info panel
 * renders it. `clearEntity(key)` only clears when the key matches, so a
 * mouse-leave on a table row never wipes a marker-clicked entity.
 */
import { defineStore } from 'pinia'

function entityKey(entity) {
  if (!entity) return null
  if (entity.osm_type && entity.osm_id != null) {
    return `${entity.osm_type}:${entity.osm_id}`
  }
  if (entity.name && entity.lat != null && entity.lon != null) {
    return `anchor:${entity.name}:${entity.lat}:${entity.lon}`
  }
  return null
}

export const useEntityInfoStore = defineStore('entityInfo', {
  state: () => ({
    entity: null,
    source: null, // 'map' | 'table'
  }),
  getters: {
    hasEntity: (state) => !!state.entity,
  },
  actions: {
    setEntity(entity, source = 'map') {
      this.entity = entity || null
      this.source = source
    },
    clearEntity(key) {
      if (key == null || entityKey(this.entity) === key) {
        this.entity = null
        this.source = null
      }
    },
    clear() {
      this.entity = null
      this.source = null
    },
  },
})
