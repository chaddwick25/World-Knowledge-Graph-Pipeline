import { defineStore } from 'pinia'
import axios from 'axios'

export const useAppStateStore = defineStore('appState', {
  state: () => ({
    /**
     * Cache of per-country state fetched from /api/app-state/<name>/
     * Keyed by normalized country name (lowercase).
     * {
     *   [countryName]: {
     *     pipeline: { status, run_id, current_stage, completed_stages, ... } | null,
     *     search: { is_ready, entity_count, has_db_embeddings },
     *     capabilities: { can_run_pipeline, can_show_search_button, ... },
     *     pipeline_steps: [{ name, label, status, message, pct, subgraphs }],
     *     fetchedAt: ISO string,
     *   }
     * }
     */
    countryStates: {},
    loading: false,
    error: null,
  }),
  getters: {
    /**
     * Get the cached state for a specific country (or null).
     */
    getCountryState: (state) => (countryName) => {
      const key = countryName?.toLowerCase().trim()
      return state.countryStates[key] || null
    },

    /**
     * Check if a country's pipeline can be started.
     * Returns `true` by default if no state is cached yet (optimistic).
     */
    canRunPipeline: (state) => (countryName) => {
      const entry = state.countryStates[countryName?.toLowerCase().trim()]
      if (!entry) return true
      return entry.capabilities?.can_run_pipeline !== false
    },

    /**
     * Check if search is ready for a country.
     */
    isSearchReady: (state) => (countryName) => {
      const entry = state.countryStates[countryName?.toLowerCase().trim()]
      if (!entry) return false
      return entry.search?.is_ready === true
    },

    /**
     * Get pipeline steps for a country (the ordered step list with status).
     */
    pipelineSteps: (state) => (countryName) => {
      const entry = state.countryStates[countryName?.toLowerCase().trim()]
      if (!entry) return []
      return entry.pipeline_steps || []
    },
  },
  actions: {
    /**
     * Fetch the application state for a specific country from the backend.
     *
     * Caches the result so subsequent calls are instant.
     * Pass `forceRefresh: true` to bypass the cache.
     *
     * Returns the country state object, or null on error.
     */
    async fetchCountryState(countryName, { forceRefresh = false } = {}) {
      if (!countryName || !countryName.trim()) return null

      const key = countryName.toLowerCase().trim()

      // Return cached value if not forced refresh
      if (!forceRefresh && this.countryStates[key]) {
        return this.countryStates[key]
      }

      this.loading = true
      this.error = null

      try {
        const encoded = encodeURIComponent(countryName.trim())
        const { data } = await axios.get(`/app-state/${encoded}/`)

        const entry = {
          pipeline: data.pipeline || null,
          search: data.search || { is_ready: false, entity_count: 0, has_db_embeddings: false },
          capabilities: data.capabilities || { can_run_pipeline: true, can_show_search_button: false, can_show_subgraphs: false, reasons_blocked: [] },
          pipeline_steps: data.pipeline_steps || [],
          fetchedAt: new Date().toISOString(),
        }

        this.countryStates[key] = entry
        return entry
      } catch (err) {
        const msg = err.response?.data?.error || err.message || 'Failed to fetch app state'
        this.error = msg
        console.error(`[appStateStore] fetchCountryState error for ${countryName}:`, msg)
        return null
      } finally {
        this.loading = false
      }
    },

    /**
     * Fetch state for multiple countries in parallel.
     * Returns a map of { countryName: entry }.
     */
    async fetchCountriesStates(countryNames, { forceRefresh = false } = {}) {
      if (!countryNames || countryNames.length === 0) return {}

      const results = await Promise.allSettled(
        countryNames.map((name) => this.fetchCountryState(name, { forceRefresh }))
      )

      const map = {}
      countryNames.forEach((name, i) => {
        if (results[i].status === 'fulfilled' && results[i].value) {
          map[name] = results[i].value
        }
      })

      return map
    },

    /**
     * Invalidate the cached state for a specific country.
     */
    clearCountryState(countryName) {
      const key = countryName?.toLowerCase().trim()
      if (key) {
        delete this.countryStates[key]
      }
    },

    /**
     * Invalidate all cached states (forces fresh fetches).
     */
    clearAll() {
      this.countryStates = {}
      this.error = null
    },
  },
})
