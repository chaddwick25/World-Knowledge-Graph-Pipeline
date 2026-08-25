/**
 * useTemplateQueryStream — EventSource lifecycle composable for the MapQA
 * streaming endpoint (GET /nca/execute-query/stream/).
 *
 * Extracted from AgentPlayground.vue's runTemplateQuery() so the embedded
 * agent mode in SemanticSearchPanel can reuse the same streaming logic
 * without duplicating the EventSource setup + event listeners.
 *
 * Owns: opening the EventSource, attaching event listeners (parsed, executed,
 * research, research_out, answer_delta, done, error), closing on cleanup.
 * Delegates result/enrichment writes to useAgentQueryStore.
 *
 * Per rule 01-frontend-patterns §1.5: one responsibility (EventSource
 * lifecycle), cleanup via cleanup() called from the component's unmounted().
 *
 * Usage in Options API (rule §1.3 — setup() only acquires + returns):
 *   setup() {
 *     const stream = useTemplateQueryStream()
 *     return { stream }
 *   }
 *   // then: this.stream.startStream({ query, countryCode, snapshotDate })
 *   // and:  unmounted() { this.stream.cleanup() }
 */
import axios from 'axios'
import { useAgentQueryStore } from '../stores/agentQueryStore'

export function useTemplateQueryStream() {
  let es = null
  const store = useAgentQueryStore()

  /**
   * Start a streaming template query.
   * @param {Object} opts
   * @param {string} opts.query - The NL geospatial question
   * @param {string} [opts.countryCode] - Optional country code
   * @param {string} [opts.snapshotDate] - Optional snapshot date
   */
  function startStream({ query, countryCode, snapshotDate }) {
    // Close any existing connection before starting a new one.
    closeStream()

    store.reset()
    store.setLoading(true)
    store.setStreamingPhase('connecting…')

    // EventSource is GET-only and can't use axios's baseURL — build the
    // full backend URL explicitly (same base axios uses).
    const base = axios.defaults.baseURL || 'http://localhost:8000/api'
    const params = new URLSearchParams({ query })
    if (countryCode) params.set('country_code', countryCode)
    if (snapshotDate) params.set('snapshot_date', snapshotDate)
    const url = `${base}/nca/execute-query/stream/?${params.toString()}`

    es = new EventSource(url)

    es.addEventListener('parsed', (e) => {
      const { parsed } = JSON.parse(e.data)
      store.setStreamingPhase(`parsed: ${parsed?.template || '?'}`)
      store.setParsedQuery(parsed)
    })

    es.addEventListener('executed', (e) => {
      const d = JSON.parse(e.data)
      store.setStreamingPhase(`executed: ${d.result_count} results`)
    })

    es.addEventListener('research', (e) => {
      const d = JSON.parse(e.data)
      store.setStreamingPhase(`researching: ${d.tool}`)
      store.addStep({ tool: d.tool, phase: 'research', running: true })
    })

    es.addEventListener('research_out', (e) => {
      const d = JSON.parse(e.data)
      const out = Array.isArray(d.output)
        ? `${d.output.length} entities`
        : 'no output'
      store.addStep({ tool: d.tool, phase: 'research_out', output: out, running: false })
    })

    es.addEventListener('answer_delta', (e) => {
      const d = JSON.parse(e.data)
      store.appendLiveAnswer(d.delta || '')
    })

    es.addEventListener('done', (e) => {
      const { result } = JSON.parse(e.data)
      store.setExecuteAnswer(result?.answer || null)
      store.setEnrichment(result?.enrichment || null)
      store.setResults(result?.results || [])
      store.setStreamingPhase('done')
      store.setLoading(false)
      // Keep liveAnswer visible after completion — it's the streamed answer.
      es?.close()
      es = null
    })

    es.addEventListener('error', (e) => {
      let msg = 'stream error'
      try {
        msg = JSON.parse(e.data).error || msg
      } catch {
        /* no data payload — network error */
      }
      store.setError(msg)
      store.setStreamingPhase('')
      store.setLoading(false)
      es?.close()
      es = null
    })
  }

  /** Close the current EventSource if open. */
  function closeStream() {
    if (es) {
      es.close()
      es = null
    }
    if (store.streamingPhase !== 'done') {
      store.setStreamingPhase('')
    }
  }

  /** Teardown — called from the component's unmounted() hook. */
  function cleanup() {
    closeStream()
  }

  return { startStream, closeStream, cleanup }
}
