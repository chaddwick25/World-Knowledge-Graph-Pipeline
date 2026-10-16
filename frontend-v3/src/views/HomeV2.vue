<template>
  <!-- HomeV2 (2026-10-16): two big square cards, Tools | Doppelganger
       agent. Clicking one opens CountryPickModal — the shared country
       list with the queryable-only filter on by default. Picks route to
       the leaf surface, which shows tools for processed countries and
       the request view for supported-but-unprocessed ones. -->
  <div class="home d-flex flex-column gap-3 p-3 h-100 overflow-auto">
    <!-- ── Header ── -->
    <header class="d-flex align-items-start justify-content-between gap-2">
      <div class="d-flex flex-column gap-1">
        <h1 class="fs-4 fw-bold mb-0" style="color: var(--bs-heading-color); letter-spacing: -0.03em;">
          <a href="https://www.vgiscience.org/projects/worldkg.html" target="_blank" rel="noopener noreferrer" class="app-shell__brand-mark text-decoration-none">&***REMOVED***127758;</a>
          World KG &amp; Geo Spatial Reasoning
        </h1>
      </div>
      <div class="flex-shrink-0">
        <span v-if="isCheckingSystem" class="badge rounded-pill text-bg-info">Checking…</span>
        <span v-else-if="isSystemReady" class="badge rounded-pill text-bg-success">System Ready</span>
        <span v-else class="badge rounded-pill text-bg-danger">System Offline</span>
      </div>
    </header>

    <!-- Planet init gate -->
    <div v-if="isSystemReady === false" class="flex-grow-1 d-flex align-items-center justify-content-center">
      <PlanetInitPanel
        :suggested-planet-file-path="suggestedPlanetFilePath"
        :system-error="systemError"
        :checking="isCheckingSystem"
        @refresh="checkSystem"
      />
    </div>

    <!-- ── Two square cards ── -->
    <section v-else-if="isSystemReady" class="home__cards">
      <button
        class="home-card"
        @click="openPicker('/tools/geo-semantic')"
      >
        Tools
      </button>
      <button
        class="home-card"
        @click="openPicker('/agents/doppelganger')"
      >
        Doppelganger Agent
      </button>
    </section>

    <CountryPickModal
      :open="pickerOpen"
      :leaf-base="pickerLeafBase"
      :countries="allCountries"
      :processed-countries="processedCountries"
      @close="pickerOpen = false"
    />
  </div>
</template>

<script>
import PlanetInitPanel from '../components/PlanetInitPanel.vue'
import CountryPickModal from '../components/CountryPickModal.vue'
import { fetchSystemStatus, fetchCountries } from '../services/systemStatus'

export default {
  name: 'HomeV2',
  components: { PlanetInitPanel, CountryPickModal },
  data() {
    return {
      isSystemReady: null,
      isCheckingSystem: true,
      systemError: '',
      suggestedPlanetFilePath: '',
      processedCountries: new Set(),

      pickerOpen: false,
      pickerLeafBase: '',
      allCountries: [],
      countriesLoading: false,
    }
  },
  mounted() {
    this.checkSystem()
  },
  methods: {
    async checkSystem() {
      this.isCheckingSystem = true
      this.systemError = ''
      try {
        const s = await fetchSystemStatus()
        this.isSystemReady = s.ready
        this.suggestedPlanetFilePath = s.suggestedPlanetFilePath
        this.processedCountries = s.processedCountries
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
    openPicker(leafBase) {
      this.pickerLeafBase = leafBase
      this.pickerOpen = true
      // Countries lazy-load on first open — the regions payload carries
      // geometries, so we don't fetch it for visitors who never click.
      if (!this.allCountries.length && !this.countriesLoading) {
        this.loadCountries()
      }
    },
    async loadCountries() {
      this.countriesLoading = true
      try {
        this.allCountries = await fetchCountries()
      } finally {
        this.countriesLoading = false
      }
    },
  },
}
</script>

<style scoped>
/* Two equal square blocks, collapsing to stacked on narrow viewports. */
.home__cards {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 1rem;
  flex: 1;
  align-content: center;
  max-width: 720px;
  margin: 0 auto;
  width: 100%;
}

@media (max-width: 720px) {
  .home__cards {
    grid-template-columns: 1fr;
  }
}

.home-card {
  aspect-ratio: 1 / 1;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--bs-emphasis-bg);
  border: 2px solid var(--bs-border-color);
  border-radius: var(--bs-border-radius-lg);
  color: inherit;
  font-size: 1.5rem;
  font-weight: 700;
  letter-spacing: -0.02em;
  transition: border-color 0.15s ease, background 0.15s ease;
}

.home-card:hover {
  border-color: var(--bs-primary);
  background: var(--bs-primary-bg-subtle);
}

.app-shell__brand-mark {
  transition: opacity 0.2s ease;
}

.app-shell__brand-mark:hover {
  opacity: 0.7;
}
</style>
