/**
 * deckLabelStore — Pinia store for the deck.gl label layers (v2 Phase 1a).
 *
 * Mirrors overlayStore.js: WorldKGMap.vue subscribes via $subscribe and
 * rebuilds the TextLayers from store state through deckManager.setLayers.
 *
 * Data shapes (backend endpoints, Phase 1b):
 *   classLabels  [{ label, centroid: [lon, lat], count }]
 *   entityLabels [{ name, position: [lon, lat], score }]
 *
 * Display settings (DeckGlControls, Phase 1c controls):
 *   classSizeScale / entitySizeScale — multiplies the text size clamps
 *   classLimit / entityLimit         — caps how many labels render
 * Mutating any of them triggers the same $subscribe rebuild as the data.
 */

import { defineStore } from 'pinia'
import { ref, computed } from 'vue'

export const useDeckLabelStore = defineStore('deckLabels', () => {
  // ── State ──

  const classLabels = ref([])
  const entityLabels = ref([])
  const classSizeScale = ref(1)
  const entitySizeScale = ref(1)
  const classLimit = ref(100)
  const entityLimit = ref(5000)

  // ── Computed ──

  const classLabelCount = computed(() => classLabels.value.length)
  const entityLabelCount = computed(() => entityLabels.value.length)
  const hasLabels = computed(
    () => classLabels.value.length > 0 || entityLabels.value.length > 0
  )

  // ── Actions ──

  function setClassLabels(data) {
    classLabels.value = Array.isArray(data) ? data : []
  }

  function setEntityLabels(data) {
    entityLabels.value = Array.isArray(data) ? data : []
  }

  function setClassSizeScale(v) {
    classSizeScale.value = v
  }

  function setEntitySizeScale(v) {
    entitySizeScale.value = v
  }

  function setClassLimit(v) {
    classLimit.value = v
  }

  function setEntityLimit(v) {
    entityLimit.value = v
  }

  function clear() {
    classLabels.value = []
    entityLabels.value = []
  }

  return {
    classLabels,
    entityLabels,
    classSizeScale,
    entitySizeScale,
    classLimit,
    entityLimit,
    classLabelCount,
    entityLabelCount,
    hasLabels,
    setClassLabels,
    setEntityLabels,
    setClassSizeScale,
    setEntitySizeScale,
    setClassLimit,
    setEntityLimit,
    clear,
  }
})
