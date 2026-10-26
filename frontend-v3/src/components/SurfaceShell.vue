<template>
  <div class="surface d-flex flex-column gap-2 p-3 h-100 overflow-hidden">
    <!-- ── Header ── -->
    <header class="d-flex align-items-center justify-content-between gap-2">
      <div class="d-flex align-items-center gap-2">
        <router-link :to="backTo" class="btn btn-link btn-sm text-secondary p-0 text-decoration-none">
          ← Back
        </router-link>
        <h1 class="fs-5 fw-bold mb-0" style="color: var(--bs-heading-color);">
          {{ title }}<template v-if="country"> — {{ countryDisplayName }}</template>
        </h1>
      </div>
      <span v-if="isCheckingSystem" class="badge rounded-pill text-bg-info">Checking…</span>
      <span v-else-if="isSystemReady" class="badge rounded-pill text-bg-success">System Ready</span>
      <span v-else class="badge rounded-pill text-bg-danger">System Offline</span>
    </header>
    <!-- Planet init gate (init flow is not a feature surface — kept). -->
    <div v-if="isSystemReady === false" class="flex-grow-1 d-flex align-items-center justify-content-center">
      <PlanetInitPanel
        :suggested-planet-file-path="suggestedPlanetFilePath"
        :system-error="systemError"
        :checking="isCheckingSystem"
        @refresh="checkSystem"
      />
    </div>
    <section v-else-if="isSystemReady" class="surface__main">
      <!-- Left: shared WorldKGMap -->
      <div class="surface__map rounded overflow-hidden">
        <WorldKGMap
          :selected-ids="selectedIds"
          :search-results="searchResults"
          :query-graph="queryGraph"
          :template-visualization="templateVisualization"
          :seed-overlay="seedOverlay"
          :twins-overlay="twinsOverlay"
          :seed-actions="surface === 'doppelganger'"
          @countries-loaded="onCountriesLoaded"
          @country-toggled="onCountryToggled"
        />
      </div>
      <!-- Right: sidebar -->
      <aside class="d-flex flex-column gap-2 p-3 rounded-3 border overflow-auto" style="background: var(--bs-emphasis-bg); min-height: 0;">
        <!-- No country resolved yet: the picker lives here — bare
             /tools/geo-semantic and /agents/doppelganger routes land on
             it, and an unsupported :cc warns then offers the picker
             rather than a broken surface. -->
        <template v-if="!country">
          <div v-if="unsupported" class="alert alert-warning small py-1 px-2 mb-0">
            <strong>{{ cc.toUpperCase() }}</strong> is not a queryable country.
            Pick one below:
          </div>
          <h2 v-else class="fs-6 fw-semibold mb-0">Choose a country</h2>
          <CountryList
            v-if="allCountries.length"
            :countries="allCountries"
            :processed-countries="processedCountries"
            @select="onCountryPick"
          />
          <div v-else class="small text-secondary">Loading countries…</div>
        </template>
        <!-- Resolved but unprocessed: the request view replaces the
             tools entirely (2026-10-16) — a picked or deep-linked
             supported-but-unprocessed country never mounts the panel. -->
        <CountryRequestPanel
          v-else-if="!countryIsQueryable"
          :country="country"
          :surface="surface"
          @pick-another="$router.push(leafBase)"
        />
        <template v-else>
          <!-- Snapshot date: static label (the picker was removed 2026-10-19;
               the surface shows the active snapshot for the demo). -->
          <div
            v-if="selectedSnapshotDate"
            class="form-label small text-secondary mb-0"
          >Snapshot Date - {{ formattedSnapshotDate }}</div>
          <slot
            :country="country"
            :snapshot-date="selectedSnapshotDate"
            :set-search-results="setSearchResults"
            :set-query-graph="setQueryGraph"
            :set-template-visualization="setTemplateVisualization"
            :set-seed-overlay="setSeedOverlay"
            :set-twins-overlay="setTwinsOverlay"
          ></slot>
        </template>
      </aside>
    </section>
  </div>
</template>

<script>
import WorldKGMap from './WorldKGMap.vue'
import CountryList from './CountryList.vue'
import CountryRequestPanel from './CountryRequestPanel.vue'
import PlanetInitPanel from './PlanetInitPanel.vue'
import { fetchSystemStatus, fetchCountries, countrySlug } from '../services/systemStatus'

export default {
  name: 'SurfaceShell',
  components: { WorldKGMap, CountryList, CountryRequestPanel, PlanetInitPanel },
  props: {
    cc: { type: String, default: '' },
    title: { type: String, required: true },
    backTo: { type: String, default: '/' },
    /** Base path of this leaf surface for country-switch navigation. */
    leafBase: { type: String, required: true },
  },
  data() {
    return {
      isSystemReady: null,
      isCheckingSystem: true,
      systemError: '',
      suggestedPlanetFilePath: '',
      processedCountries: new Set(),
      allCountries: [],
      country: null,
      unsupported: false,
      selectedIds: [],
      snapshotDates: [],
      selectedSnapshotDate: null,
      // Map overlay state the panel slot feeds back via the setters.
      searchResults: [],
      queryGraph: null,
      templateVisualization: null,
      // Doppelganger seed overlay (ISSUE-48): {confirmed, candidates}
      // from the panel's data-seed parts; rendered by WorldKGMap.
      seedOverlay: null,
      // Doppelganger twin overlay (ISSUE-50 Part A): {seed, twins} from
      // a completed run's similarEntities output; rendered by WorldKGMap.
      twinsOverlay: null,
    }
  },
  computed: {
    countryDisplayName() {
      if (!this.country) return ''
      return this.country.name.charAt(0).toUpperCase() + this.country.name.slice(1)
    },
    countryIsQueryable() {
      if (!this.country) return false
      return this.processedCountries.has(countrySlug(this.country.name))
    },
    surface() {
      return this.leafBase.startsWith('/tools') ? 'tools' : 'doppelganger'
    },
    formattedSnapshotDate() {
      const d = this.selectedSnapshotDate || ''
      const [y, m, day] = d.split('_')
      return y && m && day ? `${m}/${day}/${y}` : d
    },
  },
  watch: {
    // Map-driven or URL-driven cc changes re-resolve against the loaded
    // country list (countries-loaded only fires once).
    cc() {
      this.resolveCountry()
    },
  },
  mounted() {
    this.checkSystem()
    this.loadCountries()
  },
  methods: {
    async checkSystem() {
      this.isCheckingSystem = true
      this.systemError = ''
      try {
        const s = await fetchSystemStatus()
        this.isSystemReady = s.ready
        this.suggestedPlanetFilePath = s.suggestedPlanetFilePath
        this.snapshotDates = s.snapshotDates
        this.processedCountries = s.processedCountries
        this.selectedSnapshotDate = s.snapshotDates[0] || s.defaultDate || null
        if (!this.isSystemReady) {
          this.systemError = 'System not initialized. Planet init runs as a Docker startup step (python manage.py init_planet).'
        }
      } catch (err) {
        this.isSystemReady = false
        this.systemError = 'Failed to check system status: ' + (err.response?.data?.error || err.message)
      } finally {
        this.isCheckingSystem = false
      }
    },
    async loadCountries() {
      try {
        this.allCountries = await fetchCountries()
        this.resolveCountry()
      } catch (err) {
        this.systemError = 'Failed to load countries: ' + (err.response?.data?.error || err.message)
      }
    },
    onCountriesLoaded({ countries }) {
      // The map's own fetch is the same payload; prefer it when it
      // arrives first so the picker stays populated either way.
      if (!this.allCountries.length) {
        this.allCountries = countries
        this.resolveCountry()
      }
    },
    resolveCountry() {
      const cc = (this.cc || '').toLowerCase()
      if (!cc) {
        // Bare leaf route — no country in the URL, show the picker.
        this.country = null
        this.unsupported = false
        this.selectedIds = []
        return
      }
      const found = this.allCountries.find(
        (c) => (c.iso_code || '').toLowerCase() === cc
      )
      // Any country in the list resolves — supported or not — so a deep
      // link like /tools/geo-semantic/gh shows the request view rather
      // than silently dropping to the picker. Only a cc that matches
      // nothing at all is "unsupported".
      if (found) {
        this.country = found
        this.unsupported = false
        this.selectedIds = [found.id]
      } else if (this.allCountries.length) {
        this.country = null
        this.unsupported = true
        this.selectedIds = []
      }
    },
    onCountryPick(country) {
      this.$router.push(`${this.leafBase}/${(country.iso_code || '').toLowerCase()}`)
    },
    onCountryToggled({ countryId }) {
      const country = this.allCountries.find((c) => c.id === countryId)
      if (country) this.onCountryPick(country)
    },
    // Map-overlay setters exposed to the panel slot.
    setSearchResults(results) { this.searchResults = results },
    setQueryGraph(graph) { this.queryGraph = graph },
    setTemplateVisualization(viz) { this.templateVisualization = viz },
    setSeedOverlay(overlay) { this.seedOverlay = overlay },
    setTwinsOverlay(overlay) { this.twinsOverlay = overlay },
  },
}
</script>

<style scoped>
/* Same 2.5fr map / 1fr sidebar split as the old Home grid. */
.surface__main {
  display: grid;
  grid-template-columns: minmax(0, 2.5fr) minmax(0, 1fr);
  gap: 1rem;
  flex: 1;
  min-height: 0;
  overflow: hidden;
}

@media (max-width: 900px) {
  .surface__main {
    grid-template-columns: 1fr;
  }
}

.surface__map {
  position: relative;
  min-height: 0;
}
</style>
