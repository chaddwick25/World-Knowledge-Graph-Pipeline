<template>
  <div class="home d-flex flex-column gap-2 p-3 h-100 overflow-hidden">
    <!-- ── Header ── -->
    <header class="d-flex flex-column gap-1">
      <div class="d-flex align-items-start justify-content-between gap-2">
        <div class="d-flex flex-column gap-1">
          <h1 class="fs-4 fw-bold mb-0" style="color: var(--bs-heading-color); letter-spacing: -0.03em; text-align: left;">
            <a href="https://www.vgiscience.org/projects/worldkg.html" target="_blank" rel="noopener noreferrer" class="app-shell__brand-mark text-decoration-none">&#127758;</a>
            World KG & Geo Spatial Reasoning 
          </h1>
          <p class="fs-6 text-secondary mb-0 ms-1">
            Search places worldwide and get AI-powered answers
          </p>
        </div>
        <!-- System status badge -->
        <div class="flex-shrink-0">
          <span v-if="isCheckingSystem" class="badge rounded-pill text-bg-info">
            Checking…
          </span>
          <span v-else-if="isSystemReady" class="badge rounded-pill text-bg-success">
            System Ready
          </span>
          <span v-else class="badge rounded-pill text-bg-danger">
            System Offline
          </span>
          <!-- System Summary button (visible when ready) -->
          <button
            v-if="isSystemReady"
            class="btn btn-sm btn-outline-secondary ms-2"
            @click="showSystemSummary = true"
          >
            System Summary
          </button>
        </div>
      </div>

      <div v-if="systemError && isSystemReady" class="alert alert-danger small py-1 px-2 mb-0">{{ systemError }}</div>
    </header>

    <!-- ── Main Layout: Map + Sidebar ── -->
    <section class="home__main">
      <!-- Left: Map (hidden until system is initialized) -->
      <div class="home__map rounded overflow-hidden">
        <template v-if="isSystemReady === true">
          <WorldKGMap
            :selected-ids="selectedCountryIds"
            :search-results="searchResults"
            :query-graph="queryGraph"
            :augmented-links="augmentedLinks"
            :show-accepted-links="showAcceptedLinks"
            :show-rejected-links="showRejectedLinks"
            :visible-relations="visibleRelations"
            @countries-loaded="onCountriesLoaded"
            @country-toggled="onCountryToggled"
          />
        </template>
        <div v-else class="h-100 d-flex align-items-center justify-content-center p-3">
          <PlanetInitPanel
            :suggested-planet-file-path="suggestedPlanetFilePath"
            :system-error="systemError"
            :checking="isCheckingSystem"
            @refresh="checkSystem"
          />
        </div>
      </div>
      <!-- Right: Sidebar -->
      <aside class="d-flex flex-column gap-2 p-3 rounded-3 border overflow-auto" style="background: var(--bs-emphasis-bg); min-height: 0;">
        <!-- Planet Initialization Panel (shown only when system init failed or never ran) -->
        <PlanetInitPanel
          v-if="isSystemReady === false"
          :suggested-planet-file-path="suggestedPlanetFilePath"
          :system-error="systemError"
          :checking="isCheckingSystem"
          @refresh="checkSystem"
        />
        <!-- Selected country info (hidden until planet init is complete) -->
        <div v-if="isSystemReady" class="d-flex flex-column gap-1">
          <div class="d-flex align-items-center justify-content-between">
            <span class="small fw-semibold d-flex align-items-center gap-1">
              <span class="sidebar__dot"></span>
              Selected Country
            </span>
            <button
              v-if="hasSelection"
              class="btn btn-link btn-sm text-danger p-0"
              @click="clearSelection"
            >
              Clear
            </button>
          </div>
          <!-- Search + dropdown when nothing selected -->
          <div v-if="!singleCountry" class="d-flex flex-column gap-1">
            <input
              v-model="countrySearchQuery"
              type="text"
              class="form-control form-control-sm"
              placeholder="Search countries..."
            />
            <div v-if="filteredAvailable.length > 0" class="list-group">
              <button
                v-for="country in filteredAvailable"
                :key="'sel-' + country.id"
                class="list-group-item list-group-item-action small d-flex justify-content-between"
                @click="selectedCountryIds = [country.id]; countrySearchQuery = ''"
              >
                <span>{{ country.name }}</span>
                <span class="text-secondary">{{ country.continent }}</span>
              </button>
            </div>
            <div v-else-if="countrySearchQuery" class="small text-secondary">
              No matching countries found.
            </div>
          </div>
          <!-- Selected country card -->
          <div v-else class="card card-body p-2" style="border-color: var(--bs-primary-border-subtle);">
            <div class="fs-6 fw-semibold" style="color: var(--bs-heading-color);">{{ countryDisplayName }} - {{ continentDisplayName }}</div>
            <div v-if="isLoadingStatus" class="small text-secondary mt-1">
              Checking status…
            </div>
          </div>
        </div>
        <!-- Snapshot calendar (visible only after init) -->
        <SnapshotCalendar
          v-if="singleCountry && isSystemReady && isContinentSnapshotsReady"
          :snapshot-dates="snapshotDates"
          :completed-dates="completedDates"
          :used-dates="storeUsedDates"
          :selected-date="selectedSnapshotDate"
          :disabled="isPipelineRunning"
          :jobs="storeSnapshotJobs"
          :loading="isLoadingStatus"
          :show-run-btn="showPipelineBtn"
          :run-btn-disabled="pipelineBtnDisabled"
          :run-btn-loading="isPipelineRunning"
          :run-btn-label="pipelineTitle"
          @select-date="onSelectDate"
          @reset="onResetDates"
          @rerun="onRerunDate"
          @run="handleRunPipeline"
        />
        <!-- Pipeline progress -->
        <div class="d-flex flex-column gap-1">
          <PipelineProgressPanelV3
            v-if="singleCountry"
            :country-name="singleCountry.name"
            :session-id="pipelineSessionId"
            @pipeline-done="onPipelineDone"
          />
        </div>
        <!-- Tabbed panel (post-pipeline or search ready) -->
        <div v-if="canSearch" class="d-flex flex-column gap-1">
          <ul class="nav nav-tabs nav-fill">
            <li class="nav-item" v-for="tab in tabs" :key="tab.key">
              <button
                class="nav-link"
                :class="{ active: activeTab === tab.key }"
                @click="switchTab(tab.key)"
              >{{ tab.label }}</button>
            </li>
          </ul>
          <div class="card card-body p-2 mt-1">
            <!-- Query tab -->
            <SemanticSearchPanel
              v-if="activeTab === 'query'"
              :country-name="singleCountry.name"
              :snapshot-date="selectedSnapshotDate"
              @search-results="onSearchResults"
              @query-graph="onQueryGraph"
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
            <!-- Deck GL tab -->
            <div v-else-if="activeTab === 'deckgl'" class="d-flex align-items-center justify-content-center py-4">
              <span class="text-secondary">Coming Soon</span>
            </div>
          </div>
        </div>
      </aside>
    </section>
    <SystemSummaryModal :open="showSystemSummary" @close="showSystemSummary = false" />
    <RerunConfirmModal
      :open="showRerunConfirm"
      :snapshot-date="rerunConfirmDate"
      @close="showRerunConfirm = false"
      @confirm="confirmRerun"
    />
  </div>
</template>


<script>
import axios from 'axios'
import { usePipelineStore } from '../stores/pipelineStore'
import WorldKGMap from '../components/WorldKGMap.vue'
import PipelineProgressPanelV3 from '../components/PipelineProgressPanelV3.vue'
import SnapshotCalendar from '../components/SnapshotCalendar.vue'
import SemanticSearchPanel from '../components/SemanticSearchPanel.vue'
import PipelineMetricsPanel from '../components/PipelineMetricsPanel.vue'
import AugmentedDataPanel from '../components/AugmentedDataPanel.vue'
import PlanetInitPanel from '../components/PlanetInitPanel.vue'
import SystemSummaryModal from '../components/SystemSummaryModal.vue'
import RerunConfirmModal from '../components/RerunConfirmModal.vue'

export default {
  name: 'Home',
  components: {
    WorldKGMap,
    PipelineProgressPanelV3,
    SnapshotCalendar,
    SemanticSearchPanel,
    PipelineMetricsPanel,
    AugmentedDataPanel,
    PlanetInitPanel,
    SystemSummaryModal,
    RerunConfirmModal,
  },
  data() {
    return {
      // ── Tab definitions ──
      tabs: [
        { key: 'query', label: 'Query' },
        { key: 'metrics', label: 'Metrics' },
        { key: 'augmented', label: 'USLP' },
        { key: 'deckgl', label: 'Deck GL' },
      ],

      // ── System state ──
      isSystemReady: null,  // null = unknown, false = not ready, true = ready
      isCheckingSystem: true,
      systemError: '',
      suggestedPlanetFilePath: '',
      showSystemSummary: false,
      showRerunConfirm: false,
      rerunConfirmDate: '',

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
      queryGraph: null,

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
      completedDates: [],
      selectedSnapshotDate: null,
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
    continentDisplayName() {
      if (!this.singleCountry) return ''
      return this.singleCountry.continent
        .replace(/_/g, ' ')
        .replace(/\b\w/g, (c) => c.toUpperCase())
    },
    pipelineCanRun() {
      return this.hasSelection
        && !this.isPipelineRunning
        && this.isSystemReady
        && this.singleCountry?.is_geovectors_supported
    },
    pipelineTitle() {
      if (this.isPipelineRunning) return 'Pipeline Running…'
      if (this.pipelineDoneStatus === 'failed') return 'Pipeline Failed'
      if (this.pipelineDoneStatus === 'skipped') return 'Pipeline Skipped'
      return `Run ${this.selectedSnapshotDate || 'WorldKG Pipeline'}`
    },
    /** Show the run button only for new dates (not already completed). */
    showPipelineBtn() {
      if (!this.selectedSnapshotDate) return false
      return !this.storeUsedDates.has(this.selectedSnapshotDate)
    },
    pipelineBtnDisabled() {
      return !this.pipelineCanRun
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
    // Snapshot jobs from the store, for the current country
    storeSnapshotJobs() {
      if (!this.singleCountry) return []
      const store = usePipelineStore()
      return store.snapshotJobs[this.singleCountry.name] || []
    },
    // Used snapshot dates from the store, for the current country
    storeUsedDates() {
      if (!this.singleCountry) return new Set()
      const store = usePipelineStore()
      return store.usedSnapshotDates[this.singleCountry.name] || new Set()
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
        this.queryGraph = null
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
    // Single bootstrap ping — hydrates readiness + year selector in one go.
    this.checkSystem()
  },
  methods: {
    // ── System initialization ──
    // Planet init runs as a Docker startup step (python manage.py init_planet);
    // this pings the backend status endpoint and hydrates the page from the
    // response. Called on mount and via the PlanetInitPanel Refresh button.
    async checkSystem() {
      this.isCheckingSystem = true
      this.systemError = ''
      try {
        const { data } = await axios.get('/system/status/')
        this.isSystemReady = !!data.ready
        this.suggestedPlanetFilePath = data.suggested_planet_file_path || ''
        this.applySnapshotDates(data.snapshot_dates || [], data.default || null)
        this.completedDates = data.completed_dates || []
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
    // ── Snapshot dates (hydrated from /system/status/) ──
    applySnapshotDates(dates, defaultDate) {
      this.snapshotDates = dates
      // Default to the first available date, or the backend default
      this.selectedSnapshotDate = dates[0] || defaultDate || null
    },

    // ── Snapshot date methods ──
    onSelectDate(date) {
      this.selectedSnapshotDate = date
    },
    onRerunDate(date) {
      this.selectedSnapshotDate = date
      this.handleRunPipeline()
    },
    onResetDates() {
      if (!this.singleCountry) return
      const store = usePipelineStore()
      store.clearUsedSnapshotDates(this.singleCountry.name)
      store.resetUsedSnapshotDates(this.singleCountry.name)
      // Reset pipeline state
      this.pipelineSessionId = null
      this.pipelineDoneStatus = null
      this.isPipelineRunning = false
      this.errorMessage = ''
      // Re-select first available date
      this.selectedSnapshotDate = this.snapshotDates[0] || null
    },

    /**
     * If the selected date has a completed SnapshotJob, fetch its durable
     * results from the DB so the progress panel renders step-by-step data
     * without needing an active WebSocket connection.
     */
    async maybeFetchDurableResults() {
      if (!this.singleCountry?.iso_code || !this.selectedSnapshotDate) return
      // Don't fetch if a pipeline is actively running (WebSocket is live)
      if (this.isPipelineRunning) return
      const store = usePipelineStore()
      const usedDates = store.usedSnapshotDates[this.singleCountry.name]
      const isCompleted = usedDates && usedDates.has(this.selectedSnapshotDate)
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
    // ── Pipeline ──
    async handleRunPipeline() {
      if (!this.singleCountry || this.isPipelineRunning) return
      if (!this.singleCountry.is_geovectors_supported) {
        this.errorMessage = 'No embeddings available for this country — pipeline cannot run'
        this.pipelineDoneStatus = 'failed'
        return
      }

      // If the selected date is already completed, confirm via modal
      const isCompleted = this.storeUsedDates.has(this.selectedSnapshotDate)
      if (isCompleted) {
        this.rerunConfirmDate = this.selectedSnapshotDate
        this.showRerunConfirm = true
        return
      }

      await this._startPipelineRun(false)
    },
    confirmRerun() {
      this.showRerunConfirm = false
      this._startPipelineRun(true)
    },
    async _startPipelineRun(force) {
      this.isPipelineRunning = true
      this.pipelineDoneStatus = null
      this.errorMessage = ''
      try {
        const store = usePipelineStore()
        const sessionId = await store.startPipeline(
          this.singleCountry.name,
          this.selectedSnapshotDate,
          { force }
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
    onQueryGraph(graph) {
      this.queryGraph = graph
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
/* ── Residual CSS — cases Bootstrap utilities don't cover ── */

/* Main layout grid: 2.5fr map / 1fr sidebar. Bootstrap's grid doesn't
   support fractional fr units, so this stays as a custom rule. */
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

/* Map container needs a min-height for Leaflet to render. */
.home__map {
  min-height: 0;
}

/* Selected-country dot indicator — Bootstrap doesn't have a "dot" badge. */
.sidebar__dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 999px;
  background: var(--bs-primary);
}

/* Brand mark hover — opacity transition on the globe emoji link. */
.app-shell__brand-mark {
  transition: opacity 0.2s ease;
}

.app-shell__brand-mark:hover {
  opacity: 0.7;
}

</style>
