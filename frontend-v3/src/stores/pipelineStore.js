import { defineStore } from 'pinia'
import axios from 'axios'

// ── Step metadata (fetched from backend; fallbacks here) ────────
// The authoritative step lists come from AppStateService via the
// /api/app-state/<country>/ endpoint. These fallback lists are only
// used when the app-state endpoint hasn't been fetched yet.

const FALLBACK_WORLDKG_STEPS = [
  'embed_osm_entities',
  'harvest_wikidata',
  'run_igea',
  'predict_spatial_links',
  'train_gv_nle',
  'mark_search_ready',
]

const FALLBACK_TEMPORAL_STEPS = [
  'extract_region_pbf',
  'monthly_snapshots',
  'subgraph_generation',
  'geovectors_preprocess',
]

function stepLabel(name) {
  // If we have backend step definitions, use the label from there.
  // Otherwise, generate from name.
  return name.replace(/_/g, ' ').replace(/\b\w/g, (l) => l.toUpperCase())
}

// ── Helper: build initial step array ─────────────────────────
function makeSteps(pipelineType, pipelineSteps) {
  if (pipelineSteps && pipelineSteps.length > 0) {
    // Use authoritative steps from backend (via app-state endpoint)
    return pipelineSteps.map((s) => ({
      name: s.name,
      label: s.label || stepLabel(s.name),
      status: s.status || 'pending',
      message: s.message || '',
      pct: s.pct || 0,
    }))
  }
  // Fallback: use hardcoded lists
  const names = pipelineType === 'temporal' ? FALLBACK_TEMPORAL_STEPS : FALLBACK_WORLDKG_STEPS
  return names.map((name) => ({
    name,
    label: stepLabel(name),
    status: 'pending',
    message: '',
    pct: 0,
  }))
}

// ── Helper: reconstruct step states from backend state ───────
function buildStepsFromState(state, pipelineSteps) {
  if (pipelineSteps && pipelineSteps.length > 0) {
    // Use authoritative steps from backend. The backend already computed
    // status/pct for each step; we just merge the current stage info.
    const completed = new Set(state.completed_stages || [])
    const current = state.current_stage || null
    const isComplete = state.is_complete || state.status === 'completed'

    return pipelineSteps.map((s) => {
      let status = s.status || 'pending'
      let message = s.message || ''
      let pct = s.pct || 0

      if (completed.has(s.name)) {
        status = 'completed'
        message = 'Completed'
        pct = 100
      } else if (s.name === current) {
        status = 'in_progress'
        message = 'Running\u2026'
      } else if (isComplete && status !== 'completed') {
        status = 'skipped'
        message = 'Skipped'
      }

      return { ...s, status, message, pct }
    })
  }

  // Fallback: build from hardcoded lists using old logic
  const hasPhases = state.configuration && state.configuration.phases
  const pipelineType = hasPhases ? 'temporal' : 'worldkg'
  const names = pipelineType === 'temporal' ? FALLBACK_TEMPORAL_STEPS : FALLBACK_WORLDKG_STEPS

  const completed = new Set(state.completed_stages || [])
  const current = state.current_stage || null
  const isComplete = state.is_complete || state.status === 'completed'

  return names.map((name) => {
    let status = 'pending'
    let message = ''
    let pct = 0

    if (completed.has(name)) {
      status = 'completed'
      message = 'Completed'
      pct = 100
    } else if (name === current) {
      status = 'in_progress'
      message = 'Running\u2026'
    } else if (isComplete) {
      status = 'skipped'
      message = 'Skipped'
    }

    return { name, label: stepLabel(name), status, message, pct }
  })
}

// ── Store ─────────────────────────────────────────────────────

export const usePipelineStore = defineStore('pipeline', {
  state: () => ({
    /**
     * Keyed by country name.
     * {
     *   sessionId: string | null,
     *   status: 'idle' | 'running' | 'in_progress' | 'completed' | 'failed' | 'skipped',
     *   steps: [{ name, label, status, message, pct }],
     *   error: string | null,
     *   startedAt: string | null,
     *   completedAt: string | null,
     *   logs: [{ time, message }],
     *   pipelineType: 'worldkg' | 'temporal',
     *   phases: object | null,   // Temporal phases result
     * }
     */
    runs: {},

    /**
     * Tracks which snapshot years have been completed for a country.
     * Keyed by country name, value is a Set of year strings (e.g., "2024").
     * { [countryName]: Set<string> }
     */
    runsByYear: {},

    /**
     * Active WebSocket connections keyed by sessionId.
     * { [sessionId]: WebSocket | null }
     */
    wsConnections: {},

    /** True while fetching state from the backend. */
    loading: false,
  }),
  // TODO: validation happens before the function is called 
  getters: {
    /**
     * Return the active (running/in_progress) run for a country, or null.
     */
    activeRun:
      (state) =>
      (countryName) => {
        const run = state.runs[countryName]
        if (!run) return null
        if (['completed', 'failed', 'skipped', 'idle'].includes(run.status)) return null
        return run
      },

    /**
     * Return the latest run for a country (any status).
     */
    latestRun:
      (state) =>
      (countryName) => {
        return state.runs[countryName] || null
      },

    /**
     * Number of completed steps in a given run.
     */
    completedCount:
      () =>
      (run) => {
        if (!run || !run.steps) return 0
        return run.steps.filter((s) => s.status === 'completed').length
      },

    /**
     * Overall progress percentage (0–100).
     */
    progressPct:
      () =>
      (run) => {
        if (!run || !run.steps || run.steps.length === 0) return 0
        if (run.status === 'completed') return 100
        const done = run.steps.filter((s) => s.status === 'completed').length
        return Math.round((done / run.steps.length) * 100)
      },
  },

  actions: {
    // ──────────────────────────────────────────────────────────
    //  Fetch / restore state from backend
    // ──────────────────────────────────────────────────────────

    /**
     * Fetch the current pipeline state for a country from the backend
     * and populate the store.  Returns the run object (or null).
     */
    async fetchRunState(countryName) {
      // TODO: validation happens before the function is called 
      if (!countryName) return null

      try {
        this.loading = true

        // Fetch authoritative step definitions + pipeline state in parallel
        const [appStateResponse, pipelineResponse] = await Promise.allSettled([
          axios.get(`/app-state/${encodeURIComponent(countryName)}/`).catch(() => null),
          axios.get(`/worldkg-pipeline/state/${encodeURIComponent(countryName)}/`).catch(() => null),
        ])

        // Extract pipeline steps from app-state (authoritative)
        // TODO: validation happens before the function is called 
        let pipelineSteps = null
        if (appStateResponse.status === 'fulfilled' && appStateResponse.value?.data?.pipeline_steps) 
          pipelineSteps = appStateResponse.value.data.pipeline_steps
        const pipelineData = pipelineResponse.status === 'fulfilled' ? pipelineResponse.value?.data : null
        if (!pipelineData || !pipelineData.has_pipeline) {
          // No pipeline exists yet — ensure clean idle state
          if (!this.runs[countryName] || this.runs[countryName].status !== 'idle') {
            this._initRun(countryName, 'idle', 'worldkg', pipelineSteps)
          }
          // TODO: fix the bug this doesnt work properly
          // this needs to get fixed soon to we can do the temporal snapshots 
          return this.runs[countryName]
        }

        // Build run from backend response using authoritative step definitions
        const steps = buildStepsFromState(pipelineData, pipelineSteps)
        const isComplete = pipelineData.is_complete || pipelineData.status === 'completed'
        const isRunning = pipelineData.status === 'running' || pipelineData.status === 'in_progress'

        const run = {
          sessionId: pipelineData.session_id || null,
          status: isComplete ? 'completed' : isRunning ? 'running' : pipelineData.status || 'idle',
          steps,
          error:
            pipelineData.results?.error || pipelineData.pipeline_results?.error || pipelineData.error_message || null,
          startedAt: pipelineData.created_at || null,
          completedAt: pipelineData.completed_at || null,
          logs: [],
          pipelineType: pipelineData.configuration?.phases ? 'temporal' : 'worldkg',
          phases: pipelineData.results?.phases || null,
        }

        this.runs[countryName] = run

        // If the pipeline is still running, connect WebSocket
        if (isRunning && pipelineData.session_id) {
          this.connectWebSocket(pipelineData.session_id, countryName)
        }

        return run
      } catch (err) {
        console.error(`[pipelineStore] fetchRunState error for ${countryName}:`, err)
        if (!this.runs[countryName]) {
          this._initRun(countryName, 'idle', 'worldkg')
        }
        return this.runs[countryName]
      } finally {
        this.loading = false
      }
    },

    // ──────────────────────────────────────────────────────────
    //  WebSocket management
    // ──────────────────────────────────────────────────────────

    /**
     * Open (or re-open) a WebSocket connection for a given session.
     */
    connectWebSocket(sessionId, countryName) {
      if (!sessionId) return

      // Close old connection for this session if any
      this.disconnectWebSocket(sessionId)

      // Always connect to Django/Channels backend (port 8000)
      const url = `ws://localhost:8000/ws/pipeline/${sessionId}/`

      let ws
      try {
        ws = new WebSocket(url)
      } catch (e) {
        console.warn('[pipelineStore] WS creation failed:', e)
        return
      }

      ws.onopen = () => {
        /* no-op */
      }

      ws.onmessage = (event) => {
        try {
          const msg = JSON.parse(event.data)
          this._handleWsMessage(msg, countryName)
        } catch (e) {
          console.warn('[pipelineStore] WS parse error:', e)
        }
      }

      ws.onclose = () => {
        this.wsConnections[sessionId] = null
        // Auto-reconnect if the run is still active
        const run = this.runs[countryName]
        if (run && (run.status === 'running' || run.status === 'in_progress')) {
          setTimeout(() => this.connectWebSocket(sessionId, countryName), 5000)
        }
      }

      ws.onerror = () => {
        /* onclose will fire */
      }

      this.wsConnections[sessionId] = ws
    },

    /**
     * Close a WebSocket connection for a session (if open).
     */
    disconnectWebSocket(sessionId) {
      const existing = this.wsConnections[sessionId]
      if (existing) {
        existing.onclose = null // prevent reconnect logic
        existing.close()
        this.wsConnections[sessionId] = null
      }
    },

    /**
     * Close all WebSocket connections (e.g. on app unmount).
     */
    disconnectAll() {
      Object.keys(this.wsConnections).forEach((sid) => this.disconnectWebSocket(sid))
    },

    // ──────────────────────────────────────────────────────────
    //  App State (delegates to appStateStore)
    // ──────────────────────────────────────────────────────────

    /**
     * Fetch the consolidated app state for a country.
     * Delegates to the appStateStore for caching.
     * Returns the country state entry, or null on error.
     */
    async fetchAppState(countryName, { forceRefresh = false } = {}) {
      try {
        const { useAppStateStore } = await import('./appStateStore')
        const appStore = useAppStateStore()
        return await appStore.fetchCountryState(countryName, { forceRefresh })
      } catch (err) {
        console.error('[pipelineStore] fetchAppState error:', err)
        return null
      }
    },

    // ──────────────────────────────────────────────────────────
    //  Start / trigger pipelines
    // ──────────────────────────────────────────────────────────

    /**
     * Start the WorldKG pipeline for a country.
     * Returns the sessionId string on success.
     */
    async startPipeline(countryName, snapshotDate = null) {
      // Fetch step definitions from app-state before starting
      let pipelineSteps = null
      try {
        const appRes = await axios.get(`/app-state/${encodeURIComponent(countryName)}/`)
        if (appRes.data?.pipeline_steps) {
          pipelineSteps = appRes.data.pipeline_steps
        }
      } catch {
        // Non-critical; fallback to hardcoded steps
      }

      this._initRun(countryName, 'running', 'worldkg', pipelineSteps)
      this.runs[countryName].snapshotDate = snapshotDate

      // Track this year as used for this country
      if (snapshotDate) {
        const year = snapshotDate.split('_')[0]
        if (!this.runsByYear[countryName]) {
          this.runsByYear[countryName] = new Set()
        }
        this.runsByYear[countryName].add(year)
      }

      try {
        const payload = {
          country_name: countryName,
          skip_entropy_gate: true,
          skip_enrich: false,
        }
        if (snapshotDate) {
          payload.snapshot_date = snapshotDate
        }
        const { data } = await axios.post('/worldkg-pipeline-v2/start/', payload)
        const sessionId = data.pipeline_run_id
        this.runs[countryName].sessionId = sessionId
        this.runs[countryName].startedAt = new Date().toISOString()

        this.connectWebSocket(sessionId, countryName)
        return sessionId
      } catch (err) {
        const msg = err.response?.data?.error || err.message || 'Failed to start pipeline'
        this.runs[countryName].status = 'failed'
        this.runs[countryName].error = msg
        throw err
      }
    },

    /**
     * Start temporal preprocessing for a country.
     */
    async startPreprocessing(countryName) {
      // Fetch step definitions from app-state before starting
      let pipelineSteps = null
      try {
        const appRes = await axios.get(`/app-state/${encodeURIComponent(countryName)}/`)
        if (appRes.data?.pipeline_steps) {
          pipelineSteps = appRes.data.pipeline_steps
        }
      } catch {
        // Non-critical; fallback to hardcoded steps
      }

      this._initRun(countryName, 'running', 'temporal', pipelineSteps)

      try {
        const { data } = await axios.post('/country-preprocess/', {
          country_name: countryName,
        })

        const sessionId = data.session_id || null
        if (sessionId) {
          this.runs[countryName].sessionId = sessionId
          this.connectWebSocket(sessionId, countryName)
        }
        this.runs[countryName].startedAt = new Date().toISOString()

        // For preprocessing without a WebSocket session, poll for updates
        if (!sessionId) {
          this._pollPreprocessing(countryName)
        }

        return data
      } catch (err) {
        const msg = err.response?.data?.error || err.message || 'Failed to start preprocessing'
        this.runs[countryName].status = 'failed'
        this.runs[countryName].error = msg
        throw err
      }
    },

    // ──────────────────────────────────────────────────────────
    //  Cleanup helpers
    // ──────────────────────────────────────────────────────────

    clearRun(countryName) {
      const run = this.runs[countryName]
      if (run) {
        this.disconnectWebSocket(run.sessionId)
        delete this.runs[countryName]
      }
    },

    /**
     * Clear all tracked pipeline runs by year for a country.
     * Re-enables all year buttons for that country.
     */
    clearRunsByYear(countryName) {
      // Use $patch to trigger Vue reactivity when clearing
      this.runsByYear[countryName] = new Set()
      // Also clear the run itself
      this.clearRun(countryName)
    },

    // ──────────────────────────────────────────────────────────
    //  Internal helpers
    // ──────────────────────────────────────────────────────────

    _initRun(countryName, status = 'idle', pipelineType = 'worldkg', pipelineSteps = null) {
      const steps = makeSteps(pipelineType, pipelineSteps)
      this.runs[countryName] = {
        sessionId: null,
        status,
        steps,
        error: null,
        startedAt: null,
        completedAt: null,
        logs: [],
        pipelineType,
        phases: null,
        snapshotDate: null,
      }
    },

    _handleWsMessage(msg, countryName) {
      const run = this.runs[countryName]
      if (!run) return

      switch (msg.type) {
        case 'step_update':
          this._applyStepUpdate(run, msg)
          break

        case 'pipeline_complete':
          this._applyPipelineComplete(run, msg)
          this.disconnectWebSocket(run.sessionId)
          break

        case 'log':
          if (!run.logs) run.logs = []
          run.logs.push({ message: msg.message, time: new Date().toLocaleTimeString() })
          if (run.logs.length > 200) run.logs = run.logs.slice(-200)
          break
      }
    },

    _applyStepUpdate(run, msg) {
      const step = run.steps.find((s) => s.name === msg.name)
      if (step) {
        step.status = msg.status || 'in_progress'
        step.message = msg.message || ''
        step.pct = msg.pct || 0
      }

      // Auto-complete all steps that come before this one
      const idx = run.steps.findIndex((s) => s.name === msg.name)
      if (idx > 0) {
        for (let i = 0; i < idx; i++) {
          if (run.steps[i].status === 'pending') {
            run.steps[i].status = 'completed'
            run.steps[i].message = 'Completed'
            run.steps[i].pct = 100
          }
        }
      }

      // Mark the store as running
      if (run.status !== 'running' && run.status !== 'in_progress') {
        run.status = 'running'
      }
    },

    _applyPipelineComplete(run, msg) {
      if (msg.status === 'completed') {
        run.status = 'completed'
        run.steps.forEach((s) => {
          if (s.status === 'in_progress' || s.status === 'pending') {
            s.status = 'completed'
            s.message = 'Completed'
            s.pct = 100
          }
        })
      } else if (msg.status === 'skipped') {
        run.status = 'skipped'
        run.error = msg.reason || 'Pipeline skipped by pre-flight gate'
        run.steps.forEach((s) => {
          if (s.status === 'in_progress') {
            s.status = 'skipped'
            s.message = 'Skipped'
          }
        })
      } else {
        run.status = 'failed'
        run.error = msg.error || 'Pipeline failed (no details)'
      }

      run.completedAt = new Date().toISOString()
    },

    /**
     * Fallback polling for preprocessing when no WebSocket is available.
     */
    _pollPreprocessing(countryName) {
      const run = this.runs[countryName]
      if (!run || run.status !== 'running') return

      const interval = setInterval(async () => {
        const currentRun = this.runs[countryName]
        if (!currentRun || currentRun.status !== 'running') {
          clearInterval(interval)
          return
        }

        try {
          await this.fetchRunState(countryName)
          const updated = this.runs[countryName]
          if (updated && ['completed', 'failed', 'skipped'].includes(updated.status)) {
            clearInterval(interval)
          }
        } catch {
          clearInterval(interval)
        }
      }, 5000)
    },
  },
})
