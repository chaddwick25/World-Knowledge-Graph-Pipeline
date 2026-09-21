<template>
  <div v-if="subdivisions.length > 0" class="subdivision-selector">
    <label class="subdivision-selector__label">Subdivision</label>
    <select
      v-model="selectedQid"
      class="subdivision-selector__select"
      @change="onSelect"
    >
      <option :value="null">All subdivisions</option>
      <option
        v-for="sd in subdivisions"
        :key="sd.wikidata_id"
        :value="sd.wikidata_id"
      >
        {{ sd.name }}
      </option>
    </select>
  </div>
</template>

<script>
/**
 * SubdivisionSelector
 *
 * Dropdown for selecting a subdivision (province/state/municipality) within
 * a country.  Fetches subdivisions from the backend and emits the selected
 * Wikidata QID.
 *
 * Props:
 *   countryName  - Required. Country name or ISO-2 code.
 *
 * Emits:
 *   subdivision-selected  - Wikidata QID string, or null when "All" is selected.
 */

import axios from 'axios'

export default {
  name: 'SubdivisionSelector',
  props: {
    countryName: {
      type: String,
      required: true,
    },
  },
  emits: ['subdivision-selected'],
  data() {
    return {
      subdivisions: [],
      selectedQid: null,
      loading: false,
      error: null,
    }
  },
  watch: {
    countryName: {
      immediate: true,
      handler() {
        this.reset()
        this.fetchSubdivisions()
      },
    },
  },
  methods: {
    reset() {
      this.subdivisions = []
      this.selectedQid = null
      this.error = null
    },

    async fetchSubdivisions() {
      if (!this.countryName) return
      this.loading = true
      this.error = null
      try {
        const response = await axios.get('/nca/subdivisions/', {
          params: { country_code: this.countryName },
        })
        this.subdivisions = response.data.subdivisions || []
      } catch (err) {
        // Silently fail — subdivision filtering is optional
        this.subdivisions = []
        this.error = null
      } finally {
        this.loading = false
      }
    },

    onSelect() {
      this.$emit('subdivision-selected', this.selectedQid)
    },
  },
}
</script>

<style scoped>
.subdivision-selector {
  margin-bottom: 0.5rem;
}

.subdivision-selector__label {
  /* Micro-header: 11px uppercase with tracking. */
  display: block;
  font-size: 0.7rem;
  color: var(--bs-meta-color);
  margin-bottom: 0.2rem;
  text-transform: uppercase;
  letter-spacing: 0.03em;
}

.subdivision-selector__select {
  width: 100%;
  padding: 0.35rem 0.5rem;
  background: var(--bs-tertiary-bg);
  border: 1px solid var(--bs-border-color);
  border-radius: 0.25rem;
  color: var(--bs-body-color);
  font-size: 0.8rem;
  cursor: pointer;
}

.subdivision-selector__select:focus {
  outline: none;
  border-color: var(--bs-primary);
}
</style>
