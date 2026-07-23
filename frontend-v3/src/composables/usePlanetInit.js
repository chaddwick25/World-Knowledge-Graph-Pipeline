/**
 * usePlanetInit — Shared composable for Planet Initialization state.
 *
 * Both PlanetInitPanel (sidebar controller) and PlanetInitTerminal
 * (map-area live view) share this state, so WebSocket messages and
 * polling results update both components simultaneously.
 */


// TODO: Refactor this and use better patterns

import { ref, computed, onUnmounted } from 'vue'
import axios from 'axios'

// ── Planet init step definitions ──────────────────────────
// TODO: move these steps to a API
const PLANET_STEPS = [
  { name: 'planet_init',              label: 'Initialize Planet',               pct: 100 },
  { name: 'continent_init',           label: 'Extract Continent PBFs',          pct: 100 },
  { name: 'continent_snapshots',      label: 'Historical Continent Snapshots',  pct: 100 },
  { name: 'prebuild_structure',       label: 'Pre-build Country Structure',     pct: 100 },
  { name: 'prebuild_country_paths',   label: 'Resolve Country Paths',           pct: 100 },
  { name: 'prebuild_scan_embeddings',    label: 'Scan Embeddings (Eligibility)',  pct: 100 },
  { name: 'prebuild_copy_gb_to_uk',      label: 'Copy GB TSVs to UK Naming',     pct: 100 },
  { name: 'prebuild_split_embeddings',   label: 'Split Multi-Country TSVs',      pct: 100 },
  { name: 'prebuild_merge_us_embeddings',label: 'Merge US Regional Embeddings',   pct: 100 },
  { name: 'prebuild_rescan_embeddings', label: 'Re-scan Embeddings',            pct: 100 },
  { name: 'prebuild_subgraphs',       label: 'Generate Subgraph Profiles',      pct: 100 },
  { name: 'prebuild_wikidata_ids',    label: 'Backfill Wikidata IDs',           pct: 100 },
]

function makeStepStates() {
  return PLANET_STEPS.map((s) => ({
    name: s.name,
    label: s.label,
    status: 'pending',
    message: '',
    pct: 0,
  }))
}

let _instance = null

export function usePlanetInit() {
  // Singleton pattern — all callers share the same reactive state
  if (_instance) return _instance

  // ── State ───────────────────────────────────────────────
  const isRunning = ref(false)
  const isComplete = ref(false)
  const isFailed = ref(false)
  const pipelineRunId = ref(null)
  const errorMessage = ref('')
  const steps = ref(makeStepStates())
  const logs = ref([])
  const ws = ref(null)
  const isPolling = ref(false)
  const suggestedPlanetFilePath = ref('')
  const summaryData = ref(null)
  const showSummaryModal = ref(false)
  const planetInitActive = ref(false)  // true when init has been triggered

  // ── Computed ────────────────────────────────────────────
  const completedCount = computed(() =>
    steps.value.filter((s) => s.status === 'completed').length
  )
  const totalCount = computed(() => steps.value.length)
  const progressPct = computed(() => {
    if (isComplete.value) return 100
    const done = steps.value.filter((s) => s.status === 'completed').length
    return Math.round((done / steps.value.length) * 100)
  })
  const currentStepName = computed(() => {
    const active = steps.value.find((s) => s.status === 'in_progress')
    return active ? active.label : null
  })
  const canInitialize = computed(() => !isRunning.value && !isComplete.value && !isFailed.value)
  const canRetry = computed(() => isFailed.value && !isRunning.value)

  const badgeLabel = computed(() => {
    if (isRunning.value) return 'Running'
    if (isComplete.value) return 'Complete'
    if (isFailed.value) return 'Failed'
    return 'Idle'
  })
  const badgeVariant = computed(() => {
    if (isRunning.value) return 'info'
    if (isComplete.value) return 'success'
    if (isFailed.value) return 'danger'
    return 'secondary'
  })

  // ── Actions ─────────────────────────────────────────────
  async function handleInitialize() {
    if (isRunning.value || isComplete.value) return

    // Reset state
    isRunning.value = true
    isComplete.value = false
    isFailed.value = false
    planetInitActive.value = true
    steps.value = makeStepStates()
    logs.value = []
    errorMessage.value = ''

    // Add initial log entry
    logs.value.push({
      time: new Date().toLocaleTimeString(),
      message: '🚀 Starting planet initialization...',
    })

    try {
      const { data } = await axios.post('/planet/initialize/', {
        extract_continents: true,
      })
      pipelineRunId.value = data.pipeline_run_id

      logs.value.push({
        time: new Date().toLocaleTimeString(),
        message: `Pipeline run ID: ${data.pipeline_run_id}`,
      })

      // Start polling as fallback + connect WebSocket
      connectWebSocket(data.pipeline_run_id)
      startPolling(data.pipeline_run_id)
    } catch (err) {
      isRunning.value = false
      isFailed.value = true
      errorMessage.value = err.response?.data?.error || err.message || 'Failed to start planet initialization'
      logs.value.push({
        time: new Date().toLocaleTimeString(),
        message: `❌ Error: ${errorMessage.value}`,
      })
    }
  }

  // ── Auto-trigger continent snapshots ──────────────────
  async function triggerContinentSnapshots() {
    const csStep = steps.value.find((s) => s.name === 'continent_snapshots')
    if (!csStep) return

    csStep.status = 'in_progress'
    csStep.message = 'Extracting continent snapshots…'
    csStep.pct = 50
    logs.value.push({
      time: new Date().toLocaleTimeString(),
      message: '🔄 Extracting historical continent snapshots...',
    })

    try {
      const { data } = await axios.post('/planet/extract-continent-snapshots/', { force: false })

      if (data.status === 'completed' || data.status === 'skipped') {
        csStep.status = 'completed'
        csStep.message = data.message || 'Continent snapshots complete'
        csStep.pct = 100
        logs.value.push({
          time: new Date().toLocaleTimeString(),
          message: `✅ Continent snapshots: ${data.message || 'done'}`,
        })
      } else {
        csStep.status = 'failed'
        csStep.message = data.message || 'Extraction failed'
        isFailed.value = true
        errorMessage.value = data.message || 'Continent snapshot extraction failed'
        logs.value.push({
          time: new Date().toLocaleTimeString(),
          message: `❌ Continent snapshots failed: ${data.message || ''}`,
        })
      }
    } catch (err) {
      csStep.status = 'failed'
      csStep.message = err.message || 'Failed'
      isFailed.value = true
      errorMessage.value = err.response?.data?.message || err.message || 'Failed to extract continent snapshots'
      logs.value.push({
        time: new Date().toLocaleTimeString(),
        message: `❌ Continent snapshots error: ${errorMessage.value}`,
      })
    }
  }

  function connectWebSocket(sessionId) {
    if (!sessionId) return

    // Close existing
    if (ws.value) {
      ws.value.onclose = null
      ws.value.close()
    }

    const url = `ws://localhost:8000/ws/pipeline/${sessionId}/`

    try {
      const socket = new WebSocket(url)

      socket.onopen = () => {
        logs.value.push({
          time: new Date().toLocaleTimeString(),
          message: '🔌 WebSocket connected — receiving live updates',
        })
      }

      socket.onmessage = (event) => {
        try {
          const msg = JSON.parse(event.data)
          handleWsMessage(msg)
        } catch (e) {
          console.warn('[usePlanetInit] WS parse error:', e)
        }
      }

      socket.onclose = () => {
        if (isRunning.value && !isComplete.value) {
          // WebSocket closed but pipeline still running — rely on polling
        }
      }

      socket.onerror = () => { /* onclose will fire */ }

      ws.value = socket
    } catch (e) {
      console.warn('[usePlanetInit] WS creation failed:', e)
    }
  }

  function handleWsMessage(msg) {
    switch (msg.type) {
      case 'step_update':
        applyStepUpdate(msg)
        break

      case 'pipeline_complete':
        applyComplete(msg)
        break

      case 'log':
        if (logs.value.length < 500) {
          // Avoid duplicate messages
          const last = logs.value[logs.value.length - 1]
          if (!last || last.message !== msg.message) {
            logs.value.push({
              time: new Date().toLocaleTimeString(),
              message: msg.message,
            })
          }
        }
        break
    }
  }

  function _humanStepLabel(name) {
    const def = PLANET_STEPS.find((s) => s.name === name)
    return def ? def.label : name
  }

  function applyStepUpdate(msg) {
    const step = steps.value.find((s) => s.name === msg.name)
    if (!step) return

    const prevStatus = step.status
    step.status = msg.status || 'in_progress'
    step.message = msg.message || ''
    step.pct = msg.pct || 0
    
    // Add log when status changes
    if (prevStatus !== step.status || msg.message) {
      const prefix = step.status === 'completed' ? '✅' : step.status === 'failed' ? '❌' : '🔄'
      logs.value.push({
        time: new Date().toLocaleTimeString(),
        message: `${prefix} ${_humanStepLabel(msg.name)}: ${msg.message || step.status}`,
      })
    }

    // If a step just started, mark previous steps completed
    if (step.status === 'in_progress') {
      const idx = steps.value.findIndex((s) => s.name === msg.name)
      for (let i = 0; i < idx; i++) {
        if (steps.value[i].status === 'pending') {
          steps.value[i].status = 'completed'
          steps.value[i].pct = 100
          logs.value.push({
            time: new Date().toLocaleTimeString(),
            message: `✅ ${_humanStepLabel(steps.value[i].name)}: Completed`,
          })
        }
      }
    }

    // When continent_init completes, auto-trigger continent snapshots
    if (msg.name === 'continent_init' && msg.status === 'completed') {
      triggerContinentSnapshots()
    }
  }

  async function applyComplete(msg) {
    if (msg.status === 'completed') {
      // Wait for continent snapshots to finish if still running
      const csStep = steps.value.find((s) => s.name === 'continent_snapshots')
      if (csStep && (csStep.status === 'in_progress' || csStep.status === 'pending')) {
        await new Promise((r) => setTimeout(r, 2000))
        if (csStep.status === 'in_progress' || csStep.status === 'pending') {
          return  // Still going — let snapshot call complete on its own
        }
      }

      isComplete.value = true
      fetchSummary()
      steps.value.forEach((s) => {
        if (s.status === 'in_progress' || s.status === 'pending') {
          s.status = 'completed'
          s.message = 'Completed'
          s.pct = 100
        }
      })
      logs.value.push({
        time: new Date().toLocaleTimeString(),
        message: '✅ Planet initialization complete!',
      })
    } else {
      isFailed.value = true
      errorMessage.value = msg.error || 'Pipeline failed'
      logs.value.push({
        time: new Date().toLocaleTimeString(),
        message: `❌ Failed: ${errorMessage.value}`,
      })
    }
    isRunning.value = false
    stopPolling()
    if (ws.value) {
      ws.value.onclose = null
      ws.value.close()
      ws.value = null
    }
  }

  // ── Polling (fallback when WS is down) ──────────────────
  let pollInterval = null

  function startPolling(sessionId) {
    if (isPolling.value) return
    isPolling.value = true

    pollInterval = setInterval(async () => {
      if (!isRunning.value) {
        stopPolling()
        return
      }

      try {
        const { data } = await axios.get(`/planet/status/${sessionId}/`)
        if (data.status === 'COMPLETED') {
          applyComplete({ status: 'completed', session_id: sessionId })
        } else if (data.status === 'FAILED') {
          applyComplete({ status: 'failed', session_id: sessionId, error: data.error })
        }
      } catch {
        // Ignore polling errors
      }
    }, 3000)
  }

  function stopPolling() {
    if (pollInterval) {
      clearInterval(pollInterval)
      pollInterval = null
    }
    isPolling.value = false
  }

  // ── Summary data ──
  async function fetchSummary() {
    try {
      const { data } = await axios.get('/system/summary/')
      summaryData.value = data
    } catch {
      summaryData.value = null
    }
  }

  // ── Continue / Reset ──
  function reset() {
    isRunning.value = false
    isComplete.value = false
    isFailed.value = false
    planetInitActive.value = false
    pipelineRunId.value = null
    errorMessage.value = ''
    steps.value = makeStepStates()
    logs.value = []
    summaryData.value = null
    stopPolling()
    if (ws.value) {
      ws.value.onclose = null
      ws.value.close()
      ws.value = null
    }
  }

  // ── Expose setter for suggested path ──
  function setSuggestedPlanetFilePath(path) {
    suggestedPlanetFilePath.value = path
  }

  // ── Cleanup ─────────────────────────────────────────────
  function cleanup() {
    stopPolling()
    if (ws.value) {
      ws.value.onclose = null
      ws.value.close()
      ws.value = null
    }
  }

  _instance = {
    isRunning,
    isComplete,
    isFailed,
    pipelineRunId,
    errorMessage,
    steps,
    logs,
    suggestedPlanetFilePath,
    summaryData,
    showSummaryModal,
    planetInitActive,
    completedCount,
    totalCount,
    progressPct,
    currentStepName,
    canInitialize,
    canRetry,
    badgeLabel,
    badgeVariant,
    handleInitialize,
    triggerContinentSnapshots,
    connectWebSocket,
    handleWsMessage,
    fetchSummary,
    reset,
    setSuggestedPlanetFilePath,
    cleanup,
  }

  return _instance
}
