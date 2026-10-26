<template>
  <
  <Teleport to="body">
    <div v-if="open" class="pick-backdrop" @click.self="$emit('close')">
      <div class="pick-modal">
        <header class="d-flex align-items-center justify-content-between mb-2">
          <h3 class="fs-6 fw-semibold mb-0">{{ title }}</h3>
          <button class="btn btn-sm btn-link text-secondary p-0 fs-5 lh-1" aria-label="Close" @click="$emit('close')">&times;</button>
        </header>
        <!-- Non-queryable pick: the request view swaps in inside the
             modal — same panel the leaf route shows on a deep link. -->
        <CountryRequestPanel
          v-if="requestedCountry"
          :country="requestedCountry"
          :surface="surface"
          @pick-another="requestedCountry = null"
        />
        <CountryList
          v-else-if="countries.length"
          :countries="countries"
          :processed-countries="processedCountries"
          @select="onSelect"
        />
        <div v-else class="small text-secondary">Loading countries…</div>
      </div>
    </div>
  </Teleport>
</template>

<script>
import CountryList from './CountryList.vue'
import CountryRequestPanel from './CountryRequestPanel.vue'
import { countrySlug } from '../services/systemStatus'

export default {
  name: 'CountryPickModal',
  components: { CountryList, CountryRequestPanel },
  props: {
    open: { type: Boolean, default: false },
    title: { type: String, default: 'Choose a country' },
    countries: { type: Array, default: () => [] },
    processedCountries: { type: Set, default: () => new Set() },
    /** Leaf surface base path, e.g. '/tools/geo-semantic'. */
    leafBase: { type: String, required: true },
  },
  emits: ['close'],
  computed: {
    surface() {
      return this.leafBase.startsWith('/tools') ? 'tools' : 'doppelganger'
    },
  },
  data() {
    return {
      // Set when a non-queryable country is picked — the modal shows
      // the request view in place instead of navigating to the tool.
      requestedCountry: null,
    }
  },
  watch: {
    open(open) {
      if (!open) this.requestedCountry = null
    },
  },
  methods: {
    onSelect(country) {
      // Server-computed queryable flag (2026-10-21); the client-side
      // slug check is the fallback for older payloads.
      const queryable = country.is_queryable != null
        ? country.is_queryable
        : this.processedCountries.has(countrySlug(country.name))
      if (!queryable) {
        this.requestedCountry = country
        return
      }
      const cc = (country.iso_code || '').toLowerCase()
      this.$emit('close')
      this.$router.push(`${this.leafBase}/${cc}`)
    },
  },
}
</script>
<style scoped>
.pick-backdrop {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.45);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1080;
}
.pick-modal {
  width: min(420px, calc(100vw - 2rem));
  max-height: 80vh;
  overflow-y: auto;
  background: var(--bs-body-bg);
  border: var(--bs-border-width) solid var(--bs-border-color);
  border-radius: var(--bs-border-radius-lg);
  padding: 1rem;
}
</style>
