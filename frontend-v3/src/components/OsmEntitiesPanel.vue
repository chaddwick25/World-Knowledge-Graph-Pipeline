/**
 * OsmEntitiesPanel — merged USLP + Map Labels tab content.
 *
 * One tab ("OSM Entities") with a sub-mode toggle: the USLP view
 * (AugmentedDataPanel — predicted link summary, accepted/rejected map
 * links) and the OSM Entity view (MapLabelsPanel — class/entity labels
 * rendered on the map). Home.vue gates MapLabelsControls (the map-top-
 * right sliders) on the 'labels' sub-mode via the mode-change emit.
 *
 * Props:
 *   countryName  - Required. Country to show data for.
 *   countryCode  - ISO code, required by the labels view.
 *   snapshotDate - Optional. Snapshot date string.
 *
 * Emits:
 *   links-toggle - Pass-through from AugmentedDataPanel (accepted/rejected
 *                  link visibility; also fires null on unmount to clear).
 *   mode-change  - 'uslp' | 'labels', emitted on mount and whenever the
 *                  sub-mode toggle changes (mount emit keeps Home's
 *                  osmEntitiesMode gate in sync across tab switches).
 */

<template>
  <div class="d-flex flex-column gap-2">
    <!-- Sub-mode toggle: OSM entity labels vs USLP links (OSM Entity first
         and the default, 2026-09-28) -->
    <div class="btn-group btn-group-sm" role="group" aria-label="OSM entities view">
      <input
        type="radio"
        class="btn-check"
        name="osm-entity-mode"
        id="osm-mode-labels"
        autocomplete="off"
        value="labels"
        v-model="mode"
      >
      <label class="btn btn-outline-secondary" for="osm-mode-labels">OSM Entity</label>

      <input
        type="radio"
        class="btn-check"
        name="osm-entity-mode"
        id="osm-mode-uslp"
        autocomplete="off"
        value="uslp"
        v-model="mode"
      >
      <label class="btn btn-outline-secondary" for="osm-mode-uslp">USLP</label>
    </div>

    <AugmentedDataPanel
      v-if="mode === 'uslp'"
      :country-name="countryName"
      :snapshot-date="snapshotDate"
      @links-toggle="onLinksToggle"
    />
    <MapLabelsPanel
      v-else
      :country-name="countryName"
      :country-code="countryCode"
      :snapshot-date="snapshotDate"
    />
  </div>
</template>

<script>
import AugmentedDataPanel from './AugmentedDataPanel.vue'
import MapLabelsPanel from './MapLabelsPanel.vue'

export default {
  name: 'OsmEntitiesPanel',
  components: { AugmentedDataPanel, MapLabelsPanel },
  props: {
    countryName: {
      type: String,
      required: true,
    },
    countryCode: {
      type: String,
      default: '',
    },
    snapshotDate: {
      type: String,
      default: null,
    },
  },
  emits: ['links-toggle', 'mode-change'],
  data() {
    return {
      // Sub-mode: 'labels' (OSM entity labels, default) | 'uslp' (link
      // summary).
      mode: 'labels',
    }
  },
  watch: {
    mode(mode) {
      this.$emit('mode-change', mode)
    },
  },
  // The panel is destroyed and recreated on every tab switch (Home.vue
  // v-if), so its mode resets to the default on each mount. Report it on
  // mount too — otherwise Home's osmEntitiesMode flag goes stale (e.g.
  // left on 'uslp') and the MapLabelsControls legend stays hidden even
  // though the labels view is active.
  mounted() {
    this.$emit('mode-change', this.mode)
  },
  methods: {
    onLinksToggle(payload) {
      this.$emit('links-toggle', payload)
    },
  },
}
</script>
