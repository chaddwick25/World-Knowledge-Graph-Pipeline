/**
 * DeckGlControls — map-top-right legend controls for the deck.gl labels.
 *
 * Only rendered while the Deck GL tab is active (Home.vue gates it). The
 * four sliders write into deckLabelStore; WorldKGMap.vue's $subscribe
 * rebuilds the TextLayers with the new size/limit settings. The Save
 * button persists the four settings to localStorage; they are restored
 * on the next session. Values are bound via storeToRefs — extracting
 * store props as plain values freezes the display at mount.
 *
 *   Class size    — scales the class-label pixel clamps (blurry-at-low-zoom
 *                   fix: crank it up when zoomed out)
 *   Class count   — top-N class labels rendered (of the fetched ~200)
 *   Entity size   — scales the entity-label pixel clamps + size basis
 *   Entity count  — cap on entity labels rendered (of the fetched top classes)
 */

<template>
  <div class="deckgl-controls">
    <div class="deckgl-controls__header" @click="collapsed = !collapsed">
      <span class="deckgl-controls__title">Entity Heatmap</span>
      <span class="deckgl-controls__badge">{{ collapsed ? 'show' : 'hide' }}</span>
    </div>

    <div v-if="!collapsed" class="deckgl-controls__body">
      <label class="deckgl-controls__row">
        <span class="deckgl-controls__label">Class size</span>
        <span class="deckgl-controls__value">{{ classSizeScale.toFixed(1) }}×</span>
        <input
          type="range"
          class="form-range deckgl-controls__slider"
          min="0.5" max="3" step="0.1"
          :value="classSizeScale"
          @input="store.setClassSizeScale(Number($event.target.value))"
        />
      </label>

      <label class="deckgl-controls__row">
        <span class="deckgl-controls__label">Class count</span>
        <span class="deckgl-controls__value">{{ classLimit }}</span>
        <input
          type="range"
          class="form-range deckgl-controls__slider"
          min="10" max="200" step="10"
          :value="classLimit"
          @input="store.setClassLimit(Number($event.target.value))"
        />
      </label>

      <label class="deckgl-controls__row">
        <span class="deckgl-controls__label">Entity size</span>
        <span class="deckgl-controls__value">{{ entitySizeScale.toFixed(1) }}×</span>
        <input
          type="range"
          class="form-range deckgl-controls__slider"
          min="0.5" max="3" step="0.1"
          :value="entitySizeScale"
          @input="store.setEntitySizeScale(Number($event.target.value))"
        />
      </label>

      <label class="deckgl-controls__row">
        <span class="deckgl-controls__label">Entity count</span>
        <span class="deckgl-controls__value">{{ entityLimit }}</span>
        <input
          type="range"
          class="form-range deckgl-controls__slider"
          min="100" max="5000" step="100"
          :value="entityLimit"
          @input="store.setEntityLimit(Number($event.target.value))"
        />
      </label>

      <div class="deckgl-controls__swatches">
        <span><i class="deckgl-controls__swatch deckgl-controls__swatch--class"></i>classes</span>
        <span><i class="deckgl-controls__swatch deckgl-controls__swatch--entity"></i>entities</span>
      </div>

      <button
        class="btn btn-sm btn-outline-primary deckgl-controls__save"
        @click="onSave"
      >{{ saved ? 'Saved' : 'Save settings' }}</button>
    </div>
  </div>
</template>

<script>
import { ref } from 'vue'
import { storeToRefs } from 'pinia'
import { useDeckLabelStore } from '../stores/deckLabelStore'

export default {
  name: 'DeckGlControls',
  setup() {
    const store = useDeckLabelStore()
    const collapsed = ref(false)
    const saved = ref(false)
    // storeToRefs keeps the template reactive — the previous
    // `store.classSizeScale` extraction captured a static number, so the
    // displayed values froze at mount and never tracked the sliders.
    const { classSizeScale, entitySizeScale, classLimit, entityLimit } = storeToRefs(store)

    function onSave() {
      store.saveSettings()
      saved.value = true
      setTimeout(() => { saved.value = false }, 1500)
    }

    return {
      store,
      collapsed,
      saved,
      onSave,
      classSizeScale,
      entitySizeScale,
      classLimit,
      entityLimit,
    }
  },
}
</script>

<style scoped>
.deckgl-controls {
  position: absolute;
  top: 12px;
  right: 12px;
  z-index: 1100; /* above Leaflet's control container (1000) */
  width: 210px;
  background: rgba(15, 23, 42, 0.92);
  border: 1px solid #1f2937;
  border-radius: 8px;
  padding: 0.45rem 0.6rem;
  backdrop-filter: blur(8px);
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.35);
  user-select: none;
}

.deckgl-controls__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  cursor: pointer;
}

.deckgl-controls__title {
  font-size: 0.72rem;
  font-weight: 600;
  color: #d1d5db;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.deckgl-controls__badge {
  font-size: 0.62rem;
  color: #6b7280;
  border: 1px solid #374151;
  border-radius: 999px;
  padding: 0 0.45rem;
  line-height: 1.4;
}

.deckgl-controls__body {
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
  margin-top: 0.5rem;
}

.deckgl-controls__row {
  display: grid;
  grid-template-columns: 1fr auto;
  grid-template-rows: auto auto;
  column-gap: 0.4rem;
  row-gap: 0.1rem;
  align-items: center;
}

.deckgl-controls__label {
  font-size: 0.66rem;
  color: #9ca3af;
}

.deckgl-controls__value {
  font-size: 0.66rem;
  color: #e5e7eb;
  font-variant-numeric: tabular-nums;
}

.deckgl-controls__slider {
  grid-column: 1 / -1;
  margin: 0;
  padding: 0;
}

.deckgl-controls__swatches {
  display: flex;
  gap: 0.7rem;
  margin-top: 0.15rem;
  font-size: 0.62rem;
  color: #6b7280;
}

.deckgl-controls__swatch {
  display: inline-block;
  width: 10px;
  height: 10px;
  border-radius: 2px;
  margin-right: 0.25rem;
}

.deckgl-controls__swatch--class {
  background: #c8c8c8;
}

.deckgl-controls__swatch--entity {
  background: #78b4ff;
}

.deckgl-controls__save {
  width: 100%;
  margin-top: 0.35rem;
  font-size: 0.66rem;
  padding: 0.15rem 0;
}
</style>
