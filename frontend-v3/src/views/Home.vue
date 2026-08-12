<template>
  <div class="home">
    <!-- ── Header ── -->
    <header class="home__header">
      <div class="home__header-row">
        <div class="home__brand">
          <h1 class="home__title"> 
            <a href="https://www.vgiscience.org/projects/worldkg.html" target="_blank" rel="noopener noreferrer" class="app-shell__brand-mark">&#127758;</a> 
            World Knowledge Graph Pipeline
          </h1>
          <p class="home__subtitle">
          OSM and Wikidata KG that produces semantic tags, location embeddings, geo-spatial links and enriched entity classes
          </p>
        </div>

        <!-- System status badge -->
        <div class="home__status">
          <span v-if="isCheckingSystem" class="home__badge home__badge--info">
            Checking…
          </span>
          <span v-else-if="isSystemReady" class="home__badge home__badge--success">
            System Ready
          </span>
          <span v-else class="home__badge home__badge--danger">
            System Offline
          </span>

          <!-- System Summary button (visible when ready) -->
          <button
            v-if="isSystemReady"
            class="home__summary-btn"
            @click="showSystemSummary = true"
          >
            System Summary
          </button>
        </div>
      </div>

      <p v-if="systemError && isSystemReady" class="home__alert">{{ systemError }}</p>
    </header>

    <!-- ── Main Layout: Map + Sidebar ── -->
    <section class="home__main">
      <!-- Left: Map (hidden until system is initialized) -->
      <div class="home__map">
        <template v-if="isSystemReady === true">
          <WorldKGMap
            :selected-ids="selectedCountryIds"
            :search-results="searchResults"
            :augmented-links="augmentedLinks"
            :show-accepted-links="showAcceptedLinks"
            :show-rejected-links="showRejectedLinks"
            :visible-relations="visibleRelations"
            @countries-loaded="onCountriesLoaded"
            @country-toggled="onCountryToggled"
          />
        </template>
        <PlanetInitTerminal v-else @continue="onPlanetInitReady" />
      </div>

      <!-- Right: Sidebar -->
      <aside class="home__sidebar">
        <!-- Planet Initialization Panel (shown only when system init failed or never ran) -->
        <PlanetInitPanel
          v-if="isSystemReady === false"
          :suggested-planet-file-path="suggestedPlanetFilePath"
          @system-ready="onPlanetInitReady"
        />

        <!-- Selected country info (hidden until planet init is complete) -->
        <div v-if="isSystemReady" class="sidebar__section">
          <div class="sidebar__section-header">
            <span class="sidebar__section-title">
              <span class="sidebar__dot sidebar__dot--primary"></span>
              Selected Country
            </span>
            <button
              v-if="hasSelection"
              class="sidebar__clear-btn"
              @click="clearSelection"
            >
              Clear
            </button>
          </div>

          <!-- Search + dropdown when nothing selected -->
          <div v-if="!singleCountry" class="sidebar__search-box">
            <input
              v-model="countrySearchQuery"
              type="text"
              class="sidebar__search-input"
              placeholder="Search countries..."
            />
            <div v-if="filteredAvailable.length > 0" class="sidebar__search-results">
              <button
                v-for="country in filteredAvailable"
                :key="'sel-' + country.id"
                class="sidebar__search-item"
                @click="selectedCountryIds = [country.id]; countrySearchQuery = ''"
              >
                <span class="sidebar__search-item-name">{{ country.name }}</span>
                <span class="sidebar__search-item-continent">{{ country.continent }}</span>
              </button>
            </div>
            <div v-else-if="countrySearchQuery" class="sidebar__search-empty">
              No matching countries found.
            </div>
          </div>

          <!-- Selected country card -->
          <div v-else class="sidebar__country-card">
            <div class="sidebar__country-name">{{ countryDisplayName }}</div>
            <div class="sidebar__country-continent">{{ singleCountry.continent }}</div>

            <div v-if="isLoadingStatus" class="sidebar__status-text">
              Checking status…
            </div>
            <div v-else-if="countryStatus" class="sidebar__status-tags">
              <span
                v-if="countryStatus.is_db_processed"
                class="sidebar__tag sidebar__tag--success"
              >
                Preprocessed
              </span>
              <span
                v-if="countryStatus.has_pickle"
                class="sidebar__tag sidebar__tag--success"
              >
                Has Pickle
              </span>
              <span
                v-if="!countryStatus.is_db_processed"
                class="sidebar__tag sidebar__tag--muted"
              >
                Not Preprocessed
              </span>
            </div>
          </div>
        </div>

        <!-- Year selector (visible only after init) -->
        <div v-if="singleCountry && isSystemReady && isContinentSnapshotsReady" class="sidebar__section">
          <div class="sidebar__label-row">
            <label class="sidebar__label">Snapshot Year</label>
            <button
              v-if="hasAnyYearsUsed()"
              class="sidebar__reset-btn"
              @click="resetYearSelections"
              :disabled="isPipelineRunning"
            >
              Reset
            </button>
          </div>
          <div class="sidebar__year-buttons">
            <button
              v-for="year in snapshotYears"
              :key="year"
              class="sidebar__year-btn"
              :class="{
                'sidebar__year-btn--active': isYearActive(year),
                'sidebar__year-btn--used': isYearUsed(year) && !isYearActive(year),
                'sidebar__year-btn--disabled': yearDisabled
              }"
              :disabled="yearDisabled"
              @click="selectYear(year)"
              :title="isYearUsed(year) ? `Re-run pipeline for ${year}` : `Use snapshot from ${year}`"
            >
              <span class="sidebar__year-btn-label">{{ year }}</span>
              <span v-if="isYearUsed(year) && !isYearActive(year)" class="sidebar__year-btn-check">&#10003;</span>
            </button>
          </div>
        </div>

        <!-- Pipeline trigger (hidden until init) -->
        <div v-if="isSystemReady" class="sidebar__section">
          <button
            class="sidebar__pipeline-btn"
            :class="`sidebar__pipeline-btn--${pipelineBtnVariant}`"
            :disabled="pipelineBtnDisabled"
            @click="handleRunPipeline"
          >
            <span v-if="isPipelineRunning" class="sidebar__btn-spinner"></span>
            <span>{{ pipelineTitle }}</span>
          </button>

          <p v-if="errorMessage" class="sidebar__error">{{ errorMessage }}</p>
        </div>

        <!-- Pipeline progress -->
        <div class="sidebar__section">
          <PipelineProgressPanelV3
            v-if="singleCountry"
            :country-name="singleCountry.name"
            :session-id="pipelineSessionId"
            @pipeline-done="onPipelineDone"
          />
        </div>

        <!-- Tabbed panel (post-pipeline or search ready) -->
        <div v-if="canSearch" class="sidebar__section">
          <div class="sidebar__tabs">
            <button
              class="sidebar__tab"
              :class="{ 'sidebar__tab--active': activeTab === 'query' }"
              @click="switchTab('query')"
            >Query</button>
            <button
              class="sidebar__tab"
              :class="{ 'sidebar__tab--active': activeTab === 'metrics' }"
              @click="switchTab('metrics')"
            >Metrics</button>
            <button
              class="sidebar__tab"
              :class="{ 'sidebar__tab--active': activeTab === 'augmented' }"
              @click="switchTab('augmented')"
            >USLP </button>
            <button
              class="sidebar__tab"
              :class="{ 'sidebar__tab--active': activeTab === 'spatial' }"
              @click="switchTab('spatial')"
            >Spatial</button>
          </div>

          <!-- Tab bodies -->
          <div class="sidebar__tab-body">
            <!-- Query tab -->
            <SemanticSearchPanel
              v-if="activeTab === 'query'"
              :country-name="singleCountry.name"
              :snapshot-date="selectedSnapshotDate"
              @search-results="onSearchResults"
            />

            <!-- Metrics tab -->
            <PipelineMetricsPanel
              v-else-if="activeTab === 'metrics'"
              :country-name="singleCountry.name"
              :snapshot-date="selectedSnapshotDate"
            />

            <!-- Augmented Data tab -->
            <AugmentedDataPanel
              v-else-if="activeTab === 'augmented'"
              :country-name="singleCountry.name"
              :snapshot-date="selectedSnapshotDate"
              @links-toggle="onLinksToggle"
            />

            <!-- Spatial Layers tab -->
            <SpatialMetricsPanel
              v-else-if="activeTab === 'spatial'"
              :country-name="singleCountry.name"
              :snapshot-date="selectedSnapshotDate"
            />
          </div>
        </div>
      </aside>
    </section>

    <SystemSummaryModal :open="showSystemSummary" @close="showSystemSummary = false" />
  </div>
</template>


<script>
/**
 * Home — WorldKG Dashboard
 *
 * Redesigned single-page interface for the World Knowledge Graph Pipeline.
 *
 * Workflow:
 *   1. Select a country by clicking on the map
 *   2. Click "Run WorldKG Pipeline" → starts Celery canvas pipeline
 *   3. Progress panel shows step-by-step updates via WebSocket
 *   4. When complete → Semantic Search modal becomes available
 *
 * API endpoints consumed:
 *   GET  /api/status/initial/                      — system init check
 *   GET  /api/recipes/regions-map-data/             — country geometries
 *   GET  /api/country-search-status/{name}/         — pipeline state
 *   GET  /api/worldkg-pipeline/state/{name}/        — detailed pipeline state
 *   POST /api/worldkg-pipeline-v2/start/            — trigger pipeline
 *   WS   ws://{host}/ws/pipeline/{pipeline_run_id}/ — real-time updates
 *   POST /api/nca/semantic-triplet-search/          — semantic search
 *   GET  /api/worldkg-pipeline/summary/{name}/      — predicted links
 */
// TODO: remove relative import references
// TODO: get rid of the custom styling and use bootstrap's styling classes
import axios from 'axios'
import { usePipelineStore } from '../stores/pipelineStore'
import WorldKGMap from '../components/WorldKGMap.vue'
import PipelineProgressPanelV3 from '../components/PipelineProgressPanelV3.vue'
import SemanticSearchPanel from '../components/SemanticSearchPanel.vue'
import PipelineMetricsPanel from '../components/PipelineMetricsPanel.vue'
import AugmentedDataPanel from '../components/AugmentedDataPanel.vue'
import SpatialMetricsPanel from '../components/SpatialMetricsPanel.vue'
import PlanetInitPanel from '../components/PlanetInitPanel.vue'
import PlanetInitTerminal from '../components/PlanetInitTerminal.vue'
import SystemSummaryModal from '../components/SystemSummaryModal.vue'

import { usePlanetInit } from '../composables/usePlanetInit'

export default {
  name: 'Home',
  components: {
    WorldKGMap,
    PipelineProgressPanelV3,
    SemanticSearchPanel,
    PipelineMetricsPanel,
    AugmentedDataPanel,
    SpatialMetricsPanel,
    PlanetInitPanel,
    PlanetInitTerminal,
    SystemSummaryModal,
  },
  data() {
    // TODO: just pass a state object(use Pinia Primitives) to the components intiate on mount
    return {
      // ── System state ──
      isSystemReady: null,  // null = unknown, false = not ready, true = ready
      isCheckingSystem: true,
      systemError: '',
      suggestedPlanetFilePath: '',
      showSystemSummary: false,

      // ── Country selection ──
      allCountries: [],
      selectedCountryIds: [],
      selectedCountryName: null,

      // ── Pipeline state ──
      isPipelineRunning: false,
      pipelineSessionId: null,
      pipelineDoneStatus: null, // 'completed' | 'failed' | 'skipped' | null

      // ── Country status (preprocessed / has pickle) ──
      countryStatus: null,
      isLoadingStatus: false,

      // ── Sidebar tabs ──
      activeTab: 'query',
      searchResults: [],

      // ── Augmented links map overlay ──
      augmentedLinks: null,
      showAcceptedLinks: false,
      showRejectedLinks: false,
      visibleRelations: null,

      // ── Country search ──
      countrySearchQuery: '',

      // ── Error handling ──
      errorMessage: '',

      // ── Snapshot date selector ──
      snapshotDates: [],
      selectedSnapshotDate: null,
      
      // ── Year selector (Option 3) ──
      snapshotYears: [],
    }
  },
  computed: {
    hasSelection() {
      return this.selectedCountryIds.length > 0
    },
    selectedCountries() {
      return this.allCountries.filter((c) => this.selectedCountryIds.includes(c.id))
    },
    filteredAvailable() {
      let countries = this.allCountries.filter((c) => !this.selectedCountryIds.includes(c.id) && c.is_geovectors_supported)
      if (!this.countrySearchQuery) return countries
      const q = this.countrySearchQuery.toLowerCase()
      return countries.filter(
        (c) => c.name.toLowerCase().includes(q) || c.continent.toLowerCase().includes(q)
      )
    },
    singleCountry() {
      return this.selectedCountries.length === 1 ? this.selectedCountries[0] : null
    },
    countryDisplayName() {
      if (!this.singleCountry) return ''
      return this.singleCountry.name.charAt(0).toUpperCase() + this.singleCountry.name.slice(1)
    },
    pipelineCanRun() {
      return this.hasSelection
        && !this.isPipelineRunning
        && this.isSystemReady
        && this.singleCountry?.is_geovectors_supported
    },
    pipelineTitle() {
      if (this.isPipelineRunning) return 'Pipeline Running…'
      if (this.pipelineDoneStatus === 'completed') return 'Pipeline Complete'
      if (this.pipelineDoneStatus === 'failed') return 'Pipeline Failed'
      if (this.pipelineDoneStatus === 'skipped') return 'Pipeline Skipped'
      if (this.selectedYear) {
        const isReRun = this.isYearUsed(this.selectedYear)
        return isReRun
          ? `Re-run WorldKG Pipeline — ${this.selectedYear}`
          : `Run WorldKG Pipeline — ${this.selectedYear}`
      }
      return 'Run WorldKG Pipeline'
    },
    pipelineBtnDisabled() {
      return !this.pipelineCanRun
    },
    pipelineBtnVariant() {
      if (this.isPipelineRunning) return 'neutral'
      if (this.pipelineDoneStatus === 'completed') return 'success'
      if (this.pipelineDoneStatus === 'failed') return 'danger'
      if (this.pipelineDoneStatus === 'skipped') return 'warning'
      return 'primary'
    },

    pipelineIsDone() {
      return ['completed', 'failed', 'skipped'].includes(this.pipelineDoneStatus)
    },
    // TODO: rethink this (overrides)
    canSearch() {
      return (this.countryStatus?.is_processed || this.pipelineDoneStatus === 'completed') && this.singleCountry
    },

    // ── Year selector computed ──
    isContinentSnapshotsReady() {
      return this.snapshotDates.length > 0
    },
    selectedYear() {
      if (!this.selectedSnapshotDate) return null
      return parseInt(this.selectedSnapshotDate.split('_')[0], 10)
    },
    yearDisabled() {
      return !this.isContinentSnapshotsReady || this.isPipelineRunning
    },
  },
  watch: {
    selectedCountryIds: {
      async handler(newIds) {
        if (newIds.length === 1) {
          this.fetchCountryStatus()
          // Fetch DB-backed snapshot job status so the year selector
          // shows accurate "already processed" badges across refreshes.
          if (this.singleCountry?.iso_code) {
            const store = usePipelineStore()
            await store.fetchSnapshotJobs(
              this.singleCountry.name,
              this.singleCountry.iso_code,
            )
            // If the default selected year is already completed, fetch
            // its durable results so the progress panel shows them.
            await this.maybeFetchDurableResults()
          }
        } else {
          this.countryStatus = null
        }
        // Reset pipeline and search state when country changes
        this.pipelineSessionId = null
        this.pipelineDoneStatus = null
        this.isPipelineRunning = false
        this.searchResults = []
        this.augmentedLinks = null
        this.showAcceptedLinks = false
        this.showRejectedLinks = false
        this.visibleRelations = null
        this.errorMessage = ''
      },
      deep: true,
    },
    // When the user selects a different year, fetch durable results
    // from the DB if that year has a completed SnapshotJob.
    selectedSnapshotDate() {
      this.maybeFetchDurableResults()
    },
  },
  mounted() {
    this.checkSystem()
    this.fetchSnapshotDates()
  },
  methods: {
    // ── System initialization ──
    onPlanetInitReady() {
      console.log('[DEBUG] onPlanetInitReady called')
      this.isSystemReady = true
      this.systemError = ''
      // Don't call checkSystem() here — it would overwrite with stale data.
      // Continent/region data is fetched lazily by the map component.
    },

    async checkSystem() {
      this.isCheckingSystem = true
      this.systemError = ''
      try {
        const { data } = await axios.get('/status/initial/')
        console.log('[DEBUG] checkSystem response:', JSON.stringify(data))
        this.isSystemReady = !!data.planet_file_available
        this.suggestedPlanetFilePath = data.suggested_planet_file_path || ''
        console.log('[DEBUG] isSystemReady set to:', this.isSystemReady)
        if (this.isSystemReady === false) {
          this.systemError = 'System not initialized. Please run initialization first.'
        }
      } catch (err) {
        console.log('[DEBUG] checkSystem error:', err.message)
        this.isSystemReady = false
        this.systemError = 'Failed to check system status: ' + (err.response?.data?.error || err.message)
      } finally {
        this.isCheckingSystem = false
      }
    },
    // ── Country selection ──
    onCountriesLoaded({ countries }) {
      this.allCountries = countries.map((c) => ({
        id: c.id,
        name: c.name,
        iso_code: c.iso_code || '',
        continent: c.continent,
        continent_id: c.continent_id,
        is_geovectors_supported: c.is_geovectors_supported,
      }))
    },
    onCountryToggled({ countryId }) {
      const idx = this.selectedCountryIds.indexOf(countryId)
      if (idx >= 0) {
        this.selectedCountryIds.splice(idx, 1)
      } else {
        // Replace selection (single-select)
        this.selectedCountryIds = [countryId]
      }
    },
    clearSelection() {
      this.selectedCountryIds = []
      this.selectedCountryName = null
      this.countryStatus = null
      this.pipelineSessionId = null
      this.pipelineDoneStatus = null
      this.isPipelineRunning = false
    },
    // ── Country status ──
    async fetchCountryStatus() {
      if (!this.singleCountry) return
      this.isLoadingStatus = true
      try {
        const { data } = await axios.get(`/country-search-status/${this.singleCountry.name}/`)
        this.countryStatus = data
      } catch {
        this.countryStatus = null
      } finally {
        this.isLoadingStatus = false
      }
    },
    // ── Snapshot dates ──
    async fetchSnapshotDates() {
      try {
        const { data } = await axios.get('/planet/snapshot-dates/')
        this.snapshotDates = data.snapshot_dates || []
        // Derive unique years from the dates, sorted newest-first
        const years = new Set()
        for (const date of this.snapshotDates) {
          const year = parseInt(date.split('_')[0], 10)
          if (!isNaN(year)) years.add(year)
        }
        this.snapshotYears = Array.from(years).sort((a, b) => b - a)
        // Default to the first available date, or the backend default
        this.selectedSnapshotDate = this.snapshotDates[0] || data.default || null
      } catch {
        this.snapshotDates = []
        this.snapshotYears = []
        this.selectedSnapshotDate = null
      }
    },

    // ── Year selector methods ──
    selectYear(year) {
      if (!this.singleCountry) return
      // Allow selecting any year, including completed ones (re-run with force=true)
      const matchingDate = this.snapshotDates.find(d => d.startsWith(String(year)))
      this.selectedSnapshotDate = matchingDate || `${year}_12_31`
    },

    /**
     * If the selected year has a completed SnapshotJob, fetch its durable
     * results from the DB so the progress panel renders step-by-step data
     * without needing an active WebSocket connection.
     */
    async maybeFetchDurableResults() {
      if (!this.singleCountry?.iso_code || !this.selectedSnapshotDate) return
      // Don't fetch if a pipeline is actively running (WebSocket is live)
      if (this.isPipelineRunning) return
      const year = String(this.selectedSnapshotDate).split('_')[0]
      const store = usePipelineStore()
      const usedYears = store.runsByYear[this.singleCountry.name]
      const isCompleted = usedYears && usedYears.has(year)
      if (!isCompleted) return
      await store.fetchSnapshotJobResults(
        this.singleCountry.name,
        this.singleCountry.iso_code,
        this.selectedSnapshotDate,
      )
      // Update pipelineDoneStatus so the UI shows "Complete" badge
      const run = store.runs[this.singleCountry.name]
      if (run?.status === 'completed') {
        this.pipelineDoneStatus = 'completed'
      }
    },
    isYearUsed(year) {
      if (!this.singleCountry) return false
      const store = usePipelineStore()
      const usedYears = store.runsByYear[this.singleCountry.name]
      return usedYears ? usedYears.has(String(year)) : false
    },
    isYearActive(year) {
      if (!this.selectedSnapshotDate) return false
      const matchingDate = this.snapshotDates.find(d => d.startsWith(String(year)))
      return this.selectedSnapshotDate === (matchingDate || `${year}_12_31`)
    },
    resetYearSelections() {
      if (!this.singleCountry) return
      const store = usePipelineStore()
      store.clearRunsByYear(this.singleCountry.name)
      store.resetYearsForCountry(this.singleCountry.name)
      // Reset pipeline state
      this.pipelineSessionId = null
      this.pipelineDoneStatus = null
      this.isPipelineRunning = false
      this.errorMessage = ''
      // Re-select first available date
      this.selectedSnapshotDate = this.snapshotDates[0] || null
    },
    hasAnyYearsUsed() {
      if (!this.singleCountry) return false
      const store = usePipelineStore()
      const usedYears = store.runsByYear[this.singleCountry.name]
      return usedYears ? usedYears.size > 0 : false
    },
    // ── Pipeline ──
    async handleRunPipeline() {
      if (!this.singleCountry || this.isPipelineRunning) return
      if (!this.singleCountry.is_geovectors_supported) {
        this.errorMessage = 'No embeddings available for this country — pipeline cannot run'
        this.pipelineDoneStatus = 'failed'
        return
      }

      // If the selected year is already completed, confirm before re-running
      const selectedYear = this.selectedSnapshotDate?.split('_')[0]
      const isCompleted = selectedYear && this.isYearUsed(selectedYear)
      if (isCompleted && !window.confirm(
        `Pipeline already completed for ${selectedYear}. Re-run and overwrite?`
      )) {
        return
      }

      this.isPipelineRunning = true
      this.pipelineDoneStatus = null
      this.errorMessage = ''
      try {
        const store = usePipelineStore()
        const sessionId = await store.startPipeline(
          this.singleCountry.name,
          this.selectedSnapshotDate,
          { force: isCompleted }
        )
        this.pipelineSessionId = sessionId || null
      } catch (err) {
        const status = err.response?.status
        const reason = err.response?.data?.error || err.message || 'Failed to start pipeline'
        this.errorMessage = reason
        this.isPipelineRunning = false
        if (status === 409) {
          this.pipelineDoneStatus = 'skipped'
          // Refresh DB-backed snapshot jobs so the year badge updates.
          if (this.singleCountry?.iso_code) {
            const store = usePipelineStore()
            await store.fetchSnapshotJobs(
              this.singleCountry.name,
              this.singleCountry.iso_code,
            )
          }
        } else {
          this.pipelineDoneStatus = 'failed'
        }
      }
    },
    onPipelineDone(event) {
      this.isPipelineRunning = false
      this.pipelineDoneStatus = event.status || 'failed'

      if (event.status === 'completed') {
        // Refresh country status to see new data
        this.fetchCountryStatus()
        // Refresh DB-backed snapshot jobs so the year badge updates
        if (this.singleCountry?.iso_code) {
          const store = usePipelineStore()
          store.fetchSnapshotJobs(
            this.singleCountry.name,
            this.singleCountry.iso_code,
          )
        }
      }
    },
    // ── Semantic search ──
    switchTab(tab) {
      // Clear augmented links from map when leaving the Augmented tab
      if (this.activeTab === 'augmented' && tab !== 'augmented') {
        this.augmentedLinks = null
        this.showAcceptedLinks = false
        this.showRejectedLinks = false
        this.visibleRelations = null
      }
      this.activeTab = tab
    },
    onSearchResults(results) {
      this.searchResults = results
    },
    onLinksToggle({ links, showAccepted, showRejected, visibleRelations }) {
      this.augmentedLinks = links
      this.showAcceptedLinks = showAccepted
      this.showRejectedLinks = showRejected
      this.visibleRelations = visibleRelations
    },
  },
}
</script>


<style scoped>
.home {
  padding: 1.5rem;
  height: 100%;
  display: flex;
  flex-direction: column;
  gap: 1rem;
  overflow: hidden;
}

/* ── Header ── */

.home__header {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}

.home__header-row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 1rem;
}

.home__brand {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
}

.home__title {
  margin: 0;
  font-size: 1.5rem;
  font-weight: 700;
  color: #f3f4f6;
  letter-spacing: -0.03em;
  text-align: left;
}

.home__title .app-shell__brand-mark {
  text-decoration: none;
  transition: opacity 0.2s ease;
}

.home__title .app-shell__brand-mark:hover {
  opacity: 0.7;
}

.home__subtitle {
  margin: 0;
  font-size: 1.05rem;
  color: #f3f4f6;
  margin-left: 0.2rem;
}

.home__status {
  flex-shrink: 0;
}

.home__badge {
  display: inline-block;
  padding: 0.15rem 0.55rem;
  border-radius: 999px;
  font-size: 0.75rem;
  border: 1px solid transparent;
}

.home__badge--success {
  background: rgba(34, 197, 94, 0.15);
  border-color: #22c55e;
  color: #22c55e;
}

.home__badge--danger {
  background: rgba(239, 68, 68, 0.15);
  border-color: #ef4444;
  color: #ef4444;
}

.home__badge--info {
  background: rgba(59, 130, 246, 0.15);
  border-color: #60a5fa;
  color: #60a5fa;
}

.home__summary-btn {
  margin-left: 0.5rem;
  padding: 0.2rem 0.55rem;
  border-radius: 0.4rem;
  border: 1px solid #374151;
  background: #111827;
  color: #9ca3af;
  font-size: 0.72rem;
  cursor: pointer;
  transition: background 0.15s, border-color 0.15s, color 0.15s;
  white-space: nowrap;
}

.home__summary-btn:hover {
  background: #1f2937;
  border-color: #6366f1;
  color: #e5e7eb;
}

.home__alert {
  margin: 0;
  padding: 0.5rem 0.75rem;
  border-radius: 0.5rem;
  background: rgba(239, 68, 68, 0.1);
  border: 1px solid rgba(248, 113, 113, 0.6);
  color: #fecaca;
  font-size: 0.85rem;
}

/* ── Main Layout ── */

.home__main {
  display: grid;
  grid-template-columns: minmax(0, 2.5fr) minmax(0, 1fr);
  gap: 1rem;
  flex: 1;
  min-height: 0;
  overflow: hidden;
}

@media (max-width: 900px) {
  .home__main {
    grid-template-columns: 1fr;
  }
}

.home__map {
  min-height: 0;
  border-radius: 12px;
  overflow: hidden;
}

.home__map-placeholder {
  min-height: 520px;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 0.5rem;
  border-radius: 12px;
  background: #0a0f1e;
  border: 1px dashed #1f2937;
  text-align: center;
  padding: 2rem;
}

.home__map-placeholder-icon {
  font-size: 3rem;
  opacity: 0.3;
}

.home__map-placeholder-title {
  margin: 0;
  color: #9ca3af;
  font-size: 1.2rem;
  font-weight: 600;
}

.home__map-placeholder-text {
  margin: 0;
  color: #6b7280;
  font-size: 0.85rem;
  max-width: 360px;
  line-height: 1.5;
}

.home__map-placeholder-path {
  margin: 0.25rem 0 0;
  color: #525252;
  font-size: 0.75rem;
}

.home__map-placeholder-path code {
  color: #9ca3af;
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, 'Liberation Mono', 'Courier New', monospace;
  font-size: 0.7rem;
  word-break: break-all;
}

/* ── Sidebar ── */

.home__sidebar {
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
  padding: 1rem;
  border-radius: 0.9rem;
  background: #020617;
  border: 1px solid #111827;
  overflow-y: auto;
  min-height: 0;
}

.sidebar__section {
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
}

.sidebar__section-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.sidebar__section-title {
  font-size: 0.85rem;
  font-weight: 600;
  display: flex;
  align-items: center;
  gap: 0.35rem;
}

.sidebar__dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 999px;
}

.sidebar__dot--primary {
  background: #6366f1;
}

.sidebar__clear-btn {
  border: none;
  background: transparent;
  color: #f97373;
  font-size: 0.8rem;
  cursor: pointer;
}

.sidebar__empty {
  font-size: 0.8rem;
  color: #6b7280;
  padding: 0.25rem 0;
}

.sidebar__country-card {
  padding: 0.6rem 0.75rem;
  border-radius: 0.6rem;
  background: rgba(99, 102, 241, 0.08);
  border: 1px solid rgba(99, 102, 241, 0.2);
}

.sidebar__country-name {
  font-size: 1rem;
  font-weight: 600;
  color: #f3f4f6;
}

.sidebar__country-continent {
  font-size: 0.75rem;
  color: #9ca3af;
  text-transform: capitalize;
}

.sidebar__status-text {
  margin-top: 0.4rem;
  font-size: 0.75rem;
  color: #9ca3af;
}

.sidebar__status-tags {
  margin-top: 0.4rem;
  display: flex;
  gap: 0.35rem;
  flex-wrap: wrap;
}

.sidebar__tag {
  padding: 0.1rem 0.4rem;
  border-radius: 4px;
  font-size: 0.7rem;
  border: 1px solid transparent;
}

.sidebar__tag--success {
  background: rgba(34, 197, 94, 0.15);
  border-color: #22c55e;
  color: #22c55e;
}

.sidebar__tag--muted {
  background: rgba(75, 85, 99, 0.2);
  border-color: #4b5563;
  color: #9ca3af;
}

/* ── Pipeline button ── */

.sidebar__pipeline-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 0.5rem;
  width: 100%;
  padding: 0.6rem 1rem;
  border-radius: 0.6rem;
  border: 1px solid transparent;
  font-size: 0.9rem;
  font-weight: 600;
  cursor: pointer;
  transition: background 0.15s, border-color 0.15s, opacity 0.15s;
}

.sidebar__pipeline-btn[disabled] {
  opacity: 0.5;
  cursor: default;
}

.sidebar__pipeline-btn--primary {
  background: #4f46e5;
  border-color: #6366f1;
  color: #fff;
}

.sidebar__pipeline-btn--primary:hover:not([disabled]) {
  background: #6366f1;
}

.sidebar__pipeline-btn--success {
  background: rgba(34, 197, 94, 0.15);
  border-color: #22c55e;
  color: #22c55e;
}

.sidebar__pipeline-btn--danger {
  background: rgba(239, 68, 68, 0.15);
  border-color: #ef4444;
  color: #ef4444;
}

.sidebar__pipeline-btn--warning {
  background: rgba(234, 179, 8, 0.15);
  border-color: #eab308;
  color: #eab308;
}

.sidebar__pipeline-btn--neutral {
  background: #1f2937;
  border-color: #374151;
  color: #9ca3af;
}

.sidebar__btn-spinner {
  width: 16px;
  height: 16px;
  border-radius: 999px;
  border: 2px solid rgba(191, 219, 254, 0.4);
  border-top-color: #eff6ff;
  animation: spin 0.8s linear infinite;
}

.sidebar__error {
  margin: 0;
  font-size: 0.78rem;
  color: #fecaca;
  padding: 0.3rem 0.4rem;
  background: rgba(239, 68, 68, 0.08);
  border-radius: 0.4rem;
}

/* ── Snapshot date selector ── */

.sidebar__label {
  font-size: 0.75rem;
  font-weight: 600;
  color: #9ca3af;
  margin-bottom: 0.2rem;
}

.sidebar__label-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.sidebar__reset-btn {
  border: none;
  background: transparent;
  color: #f97373;
  font-size: 0.72rem;
  cursor: pointer;
  padding: 0.1rem 0.3rem;
}

.sidebar__reset-btn:disabled {
  opacity: 0.4;
  cursor: default;
}

.sidebar__year-buttons {
  display: flex;
  gap: 0.3rem;
  flex-wrap: wrap;
}

.sidebar__year-btn {
  display: flex;
  align-items: center;
  gap: 0.25rem;
  padding: 0.3rem 0.55rem;
  border-radius: 0.4rem;
  border: 1px solid #374151;
  background: #111827;
  color: #d1d5db;
  font-size: 0.78rem;
  font-weight: 500;
  cursor: pointer;
  transition: background 0.15s, border-color 0.15s, color 0.15s;
}

.sidebar__year-btn:hover:not(:disabled) {
  background: #1f2937;
  border-color: #6366f1;
  color: #e5e7eb;
}

.sidebar__year-btn--active {
  background: rgba(99, 102, 241, 0.2);
  border-color: #6366f1;
  color: #a5b4fc;
  font-weight: 600;
}

.sidebar__year-btn--used {
  border-color: #22c55e;
  color: #22c55e;
}

.sidebar__year-btn--used:hover:not(.sidebar__year-btn--active) {
  background: rgba(34, 197, 94, 0.15);
  border-color: #16a34a;
  color: #4ade80;
}

.sidebar__year-btn--disabled {
  opacity: 0.4;
  cursor: default;
}

.sidebar__year-btn:disabled {
  cursor: default;
}

.sidebar__year-btn-check {
  font-size: 0.7rem;
}

.sidebar__select {
  width: 100%;
  padding: 0.4rem 0.6rem;
  border-radius: 0.4rem;
  border: 1px solid #374151;
  background: #111827;
  color: #e5e7eb;
  font-size: 0.78rem;
  cursor: pointer;
  appearance: auto;
}

.sidebar__select:disabled {
  opacity: 0.5;
  cursor: default;
}

.sidebar__select:focus {
  outline: none;
  border-color: #6366f1;
}

/* ── Semantic Search button ── */

.sidebar__search-btn {
  width: 100%;
  padding: 0.5rem 1rem;
  border-radius: 0.6rem;
  border: 1px solid #4f46e5;
  background: #111827;
  color: #e5e7eb;
  font-size: 0.85rem;
  cursor: pointer;
}

.sidebar__search-btn:hover {
  background: #1f2937;
  border-color: #6366f1;
}

.sidebar__hint {
  margin: 0;
  font-size: 0.75rem;
  color: #6b7280;
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}

/* ── Tabs ── */

.sidebar__tabs {
  display: flex;
  gap: 0.2rem;
  background: rgba(15, 23, 42, 0.6);
  border-radius: 0.5rem;
  padding: 0.2rem;
}

.sidebar__tab {
  flex: 1;
  padding: 0.3rem 0.4rem;
  border: none;
  border-radius: 0.35rem;
  background: transparent;
  color: #9ca3af;
  font-size: 0.72rem;
  font-weight: 500;
  cursor: pointer;
  transition: background 0.15s, color 0.15s;
}

.sidebar__tab:hover {
  color: #e5e7eb;
  background: rgba(75, 85, 99, 0.3);
}

.sidebar__tab--active {
  background: #1f2937;
  color: #e5e7eb;
  font-weight: 600;
}

.sidebar__tab-body {
  margin-top: 0.35rem;
  padding: 0.5rem 0.4rem;
  border-radius: 0.5rem;
  background: rgba(15, 23, 42, 0.4);
  border: 1px solid #1f2937;
}

/* ── Tab placeholder ── */

.tab-placeholder {
  text-align: center;
  padding: 0.75rem 0.5rem;
}

.tab-placeholder__text {
  margin: 0;
  color: #9ca3af;
  font-size: 0.85rem;
  font-weight: 500;
}

.tab-placeholder__hint {
  margin: 0.3rem 0 0;
  color: #6b7280;
  font-size: 0.72rem;
}
</style>
