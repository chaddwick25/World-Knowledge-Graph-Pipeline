<template>
  <!-- Shared country picker (2026-10-15): the searchable country list
       that used to live inside Home.vue's sidebar, lifted so the home
       modal and the leaf-surface fallback in SurfaceShell can reuse it.
       Emits the selected country object; what happens next (navigate vs
       request view) is the parent's business. -->
  <div class="d-flex flex-column gap-1">
    <div class="d-flex align-items-center gap-2">
      <input
        v-model="countrySearchQuery"
        type="text"
        class="form-control form-control-sm"
        placeholder="Search countries..."
      />
      <div class="form-check form-switch flex-shrink-0 mb-0">
        <input
          id="only-queryable"
          v-model="onlyQueryable"
          type="checkbox"
          class="form-check-input"
        />
        <label class="form-check-label small text-secondary text-nowrap" for="only-queryable">
          Available countries
        </label>
      </div>
    </div>
    <div v-if="filteredAvailable.length > 0" class="list-group country-list">
      <button
        v-for="country in filteredAvailable"
        :key="'sel-' + country.id"
        class="list-group-item list-group-item-action small d-flex justify-content-between align-items-center"
        @click="$emit('select', country)"
      >
        <span>{{ country.name }}</span>
        <span class="d-flex align-items-center gap-1">
          <span class="text-secondary">{{ country.continent }}</span>
          <span
            v-if="hasData(country)"
            class="data-dot"
            title="Pipeline data ready — this country is queryable"
          ></span>
        </span>
      </button>
    </div>
    <div v-else class="small text-secondary">
      {{ countrySearchQuery ? 'No matching countries found.' : 'No available countries yet — toggle off the filter to see all.' }}
    </div>
  </div>
</template>

<script>
import { countrySlug } from '../services/systemStatus'

export default {
  name: 'CountryList',
  props: {
    countries: { type: Array, required: true },
    processedCountries: { type: Set, default: () => new Set() },
  },
  emits: ['select'],
  data() {
    return {
      countrySearchQuery: '',
      // Default on (2026-10-15): the picker opens showing only
      // countries with pipeline data — the green-dot set.
      onlyQueryable: true,
    }
  },
  computed: {
    filteredAvailable() {
      // With the queryable filter off the whole list shows —
      // non-processed picks land on the request view, not the tools.
      let countries = this.countries
      if (this.onlyQueryable) {
        countries = countries.filter((c) => this.hasData(c))
      }
      if (!this.countrySearchQuery) return countries
      const q = this.countrySearchQuery.toLowerCase()
      return countries.filter(
        (c) => c.name.toLowerCase().includes(q) || c.continent.toLowerCase().includes(q)
      )
    },
  },
  methods: {
    hasData(country) {
      return this.processedCountries.has(countrySlug(country.name))
    },
  },
}
</script>

<style scoped>
/* Queryable-country dot — green = pipeline data ready for that country. */
.data-dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 999px;
  background: var(--bs-success);
  flex-shrink: 0;
}

/* Cap the list so the search box and Request-a-country link stay in
   view on short viewports. */
.country-list {
  max-height: 45vh;
  overflow-y: auto;
}
</style>
