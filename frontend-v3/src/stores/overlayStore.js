/**
 * overlayStore — Pinia store for agent-driven map overlays (MCP).
 *
 * The agent calls renderToolOverlay → this store accumulates overlay
 * descriptors; WorldKGMap.vue subscribes and draws them into a dedicated
 * layerGroup. Human search results keep their own path (search-results
 * prop on WorldKGMap) so the user can always tell who drew what.
 *
 * Overlay shape:
 *   {
 *     id, tool, kind, result?, entities?, anchor?, radius?, color?
 *   }
 */

import { defineStore } from 'pinia'
import { ref, computed } from 'vue'

export const useOverlayStore = defineStore('overlay', () => {
  // ── State ──

  const overlays = ref([])

  // ── Computed ──

  const overlayCount = computed(() => overlays.value.length)

  // ── Actions ──

  function renderOverlay(overlay) {
    const id =
      overlay.id ||
      `ov-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
    overlays.value.push({ id, ...overlay })
    return { status: 'rendered', id, overlayCount: overlays.value.length }
  }

  function clearOverlays(tool) {
    if (tool) {
      overlays.value = overlays.value.filter((o) => o.tool !== tool)
    } else {
      overlays.value = []
    }
    return { status: 'cleared', overlayCount: overlays.value.length }
  }

  return {
    overlays,
    overlayCount,
    renderOverlay,
    clearOverlays,
  }
})
