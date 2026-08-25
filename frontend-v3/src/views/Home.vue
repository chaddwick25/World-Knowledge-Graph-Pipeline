<template>
  <div class="home d-flex flex-column gap-2 p-3 h-100 overflow-hidden">
    <!-- ── Header ── -->
    <header class="d-flex flex-column gap-1">
      <div class="d-flex align-items-start justify-content-between gap-2">
        <div class="d-flex flex-column gap-1">
          <h1 class="fs-4 fw-bold mb-0" style="color: var(--bs-heading-color); letter-spacing: -0.03em; text-align: left;">
            <a href="https://www.vgiscience.org/projects/worldkg.html" target="_blank" rel="noopener noreferrer" class="app-shell__brand-mark text-decoration-none">&#127758;</a>
            World Knowledge Graph Pipeline
          </h1>
          <p class="fs-6 text-secondary mb-0 ms-1">
            OSM and Wikidata KG that produces semantic tags, location embeddings, geo-spatial links and enriched entity classes
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
            :hide-selected-border="toolMode === 'agent'"
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
            <div class="fs-6 fw-semibold" style="color: var(--bs-heading-color);">{{ countryDisplayName }}</div>
            <div class="small text-secondary text-capitalize">{{ singleCountry.continent }}</div>

            <div v-if="isLoadingStatus" class="small text-secondary mt-1">
              Checking status…
            </div>
            <div v-else-if="countryStatus" class="d-flex flex-wrap gap-1 mt-1">
              <span
                v-if="countryStatus.is_db_processed"
                class="badge text-bg-success"
              >
                Preprocessed
              </span>
              <span
                v-if="countryStatus.has_pickle"
                class="badge text-bg-success"
              >
                Has Pickle
              </span>
              <span
                v-if="!countryStatus.is_db_processed"
                class="badge text-bg-secondary"
              >
                Not Preprocessed
              </span>
            </div>
          </div>
        </div>

        <!-- Year selector (visible only after init) -->
        <div v-if="singleCountry && isSystemReady && isContinentSnapshotsReady" class="d-flex flex-column gap-1">
          <div class="d-flex align-items-center justify-content-between">
            <label class="form-label small text-secondary mb-0">Snapshot Year</label>
            <button
              v-if="hasAnyYearsUsed()"
              class="btn btn-link btn-sm text-danger p-0"
              @click="resetYearSelections"
              :disabled="isPipelineRunning"
            >
              Reset
            </button>
          </div>
          <div class="d-flex flex-wrap gap-1">
            <button
              v-for="year in snapshotYears"
              :key="year"
              class="btn btn-sm btn-outline-secondary sidebar__year-btn"
              :class="{
                'sidebar__year-btn--active': isYearActive(year),
                'sidebar__year-btn--used': isYearUsed(year) && !isYearActive(year),
                'sidebar__year-btn--disabled': yearDisabled
              }"
              :disabled="yearDisabled"
              @click="selectYear(year)"
              :title="isYearUsed(year) ? `Re-run pipeline for ${year}` : `Use snapshot from ${year}`"
            >
              <span>{{ year }}</span>
              <span v-if="isYearUsed(year) && !isYearActive(year)">&#10003;</span>
            </button>
          </div>
        </div>

        <!-- Pipeline trigger (hidden until init) -->
        <div v-if="isSystemReady" class="d-flex flex-column gap-1">
          <button
            class="btn w-100 d-flex align-items-center justify-content-center gap-2"
            :class="pipelineBtnBootstrapClass"
            :disabled="pipelineBtnDisabled"
            @click="handleRunPipeline"
          >
            <span v-if="isPipelineRunning" class="spinner-border spinner-border-sm" role="status" aria-hidden="true"></span>
            <span>{{ pipelineTitle }}</span>
          </button>

          <div v-if="errorMessage" class="alert alert-danger small py-1 px-2 mb-0">{{ errorMessage }}</div>
        </div>

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
          <!-- Agent / Human toggle -->
          <div class="d-flex justify-content-end gap-1 mb-1">
            <button
              type="button"
              class="btn btn-sm d-flex align-items-center gap-1"
              :class="toolMode === 'agent' ? 'btn-primary' : 'btn-outline-secondary'"
              @click="setToolMode('agent')"
            >
              <img :src="agentIconUrl" alt="" width="16" height="16" />
              Agent
            </button>
            <button
              type="button"
              class="btn btn-sm d-flex align-items-center gap-1"
              :class="toolMode === 'human' ? 'btn-primary' : 'btn-outline-secondary'"
              @click="setToolMode('human')"
            >
              <img :src="humanIconUrl" alt="" width="16" height="16" />
              Human
            </button>
          </div>

          <!-- Human mode: existing 4 tabs -->
          <template v-if="toolMode === 'human'">
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

              <!-- Spatial Layers tab -->
              <SpatialMetricsPanel
                v-else-if="activeTab === 'spatial'"
                :country-name="singleCountry.name"
                :snapshot-date="selectedSnapshotDate"
              />
            </div>
          </template>

          <!-- Agent mode: SemanticSearchPanel with agentMode -->
          <SemanticSearchPanel
            v-else
            :country-name="singleCountry.name"
            :snapshot-date="selectedSnapshotDate"
            :agent-mode="true"
            @search-results="onSearchResults"
            @query-graph="onQueryGraph"
          />
        </div>
      </aside>
    </section>

    <SystemSummaryModal :open="showSystemSummary" @close="showSystemSummary = false" />
  </div>
</template>


<script>
import axios from 'axios'
import { usePipelineStore } from '../stores/pipelineStore'
import WorldKGMap from '../components/WorldKGMap.vue'
import PipelineProgressPanelV3 from '../components/PipelineProgressPanelV3.vue'
import SemanticSearchPanel from '../components/SemanticSearchPanel.vue'
import PipelineMetricsPanel from '../components/PipelineMetricsPanel.vue'
import AugmentedDataPanel from '../components/AugmentedDataPanel.vue'
import SpatialMetricsPanel from '../components/SpatialMetricsPanel.vue'
import PlanetInitPanel from '../components/PlanetInitPanel.vue'
import SystemSummaryModal from '../components/SystemSummaryModal.vue'

import agentIconUrl from '../assets/ai_agent.png'
import humanIconUrl from '../assets/human_user.svg'

const TOOL_MODE_STORAGE_KEY = 'worldkg:toolMode'

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
    SystemSummaryModal,
  },
  data() {
    return {
      // ── Icons for the Agent/Human toggle ──
      agentIconUrl,
      humanIconUrl,

      // ── Toolset mode (Agent vs Human) ──
      toolMode: 'human',

      // ── Tab definitions (human mode) ──
      tabs: [
        { key: 'query', label: 'Query' },
        { key: 'metrics', label: 'Metrics' },
        { key: 'augmented', label: 'USLP' },
        { key: 'spatial', label: 'Spatial' },
      ],

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
    pipelineBtnBootstrapClass() {
      const variant = this.pipelineBtnVariant
      if (variant === 'primary') return 'btn-primary'
      if (variant === 'success') return 'btn-success'
      if (variant === 'danger') return 'btn-danger'
      if (variant === 'warning') return 'btn-warning'
      return 'btn-secondary'
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
    this.restoreToolMode()
    // Single bootstrap ping — hydrates readiness + year selector in one go.
    this.checkSystem()
  },
  methods: {
    // ── Toolset mode (Agent/Human) ──
    setToolMode(mode) {
      this.toolMode = mode
      try {
        localStorage.setItem(TOOL_MODE_STORAGE_KEY, mode)
      } catch {
        // localStorage may be unavailable (private browsing) — fail silently
      }
    },
    restoreToolMode() {
      try {
        const saved = localStorage.getItem(TOOL_MODE_STORAGE_KEY)
        if (saved === 'agent' || saved === 'human') {
          this.toolMode = saved
        }
      } catch {
        // fail silently
      }
    },

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
      // Derive unique years from the dates, sorted newest-first
      this.snapshotYears = Array.from(
        new Set(
          dates
            .map((d) => parseInt(d.split('_')[0], 10))
            .filter((y) => !isNaN(y))
        )
      ).sort((a, b) => b - a)
      // Default to the first available date, or the backend default
      this.selectedSnapshotDate = dates[0] || defaultDate || null
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

/* Year button states — Bootstrap's .active uses --bs-primary but the
   "used" (already-processed) green border is custom. */
.sidebar__year-btn--active {
  background: var(--bs-primary-bg-subtle);
  border-color: var(--bs-primary);
  color: var(--bs-primary-text-emphasis);
  font-weight: 600;
}

.sidebar__year-btn--used {
  border-color: var(--bs-success);
  color: var(--bs-success);
}

.sidebar__year-btn--used:hover:not(.sidebar__year-btn--active) {
  background: var(--bs-success-bg-subtle);
  border-color: var(--bs-success);
}

.sidebar__year-btn--disabled {
  opacity: 0.4;
  cursor: default;
}

/* Brand mark hover — opacity transition on the globe emoji link. */
.app-shell__brand-mark {
  transition: opacity 0.2s ease;
}

.app-shell__brand-mark:hover {
  opacity: 0.7;
}
</style>
