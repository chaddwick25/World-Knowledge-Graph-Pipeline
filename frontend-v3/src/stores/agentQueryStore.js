/**
 * useAgentQueryStore — shared template-query state across the Agent/Human
 * toolset toggle in Home.vue.
 *
 * Both the human Query tab's SemanticSearchPanel and the agent mode's
 * SemanticSearchPanel read from / write to this store, so toggling
 * agent ↔ human preserves the template query text and results.
 *
 * Options Store syntax per rule 01-frontend-patterns §1.4. State is the
 * source of truth, getters derive, actions mutate. Components read getters
 * and call actions; they do not reach into store.$state directly.
 */
import { defineStore } from 'pinia'

export const useAgentQueryStore = defineStore('agentQuery', {
  state: () => ({
    // The NL question text — shared across agent/human modes.
    templateQuery: '',
    // MapQA parser output: { template, confidence, concepts }
    parsedQuery: null,
    // Deterministic answer string from the executor.
    executeAnswer: null,
    // Enrichment object from the LLM research loop:
    //   { primary_answer, actions, action_outputs, enriched_answer }
    enrichment: null,
    // Executor result entities (for map markers via search-results emit).
    results: [],
    // Executor execution trace (geocode steps carry the query anchors'
    // coordinates — used by the anchor/entity map visualization).
    trace: [],
    // Streaming phase label for the live "agent thinking" UX.
    // '' | 'connecting…' | 'parsed: …' | 'executed: …' | 'researching: …' | 'done'
    streamingPhase: '',
    // Partial answer accumulated during token streaming.
    liveAnswer: '',
    // Step log entries (agent mode only — research tool calls).
    steps: [],
    loading: false,
    error: null,
  }),

  getters: {
    hasResults: (state) => state.results.length > 0,
    isStreaming: (state) =>
      state.streamingPhase !== '' && state.streamingPhase !== 'done',
    hasEnrichment: (state) => state.enrichment?.enriched_answer != null,
    hasError: (state) => state.error != null,
  },

  actions: {
    setTemplateQuery(q) {
      this.templateQuery = q
    },
    setParsedQuery(p) {
      this.parsedQuery = p
    },
    setExecuteAnswer(a) {
      this.executeAnswer = a
    },
    setEnrichment(e) {
      this.enrichment = e
    },
    setResults(r) {
      this.results = Array.isArray(r) ? r : []
    },
    setTrace(t) {
      this.trace = Array.isArray(t) ? t : []
    },
    setStreamingPhase(p) {
      this.streamingPhase = p
    },
    appendLiveAnswer(delta) {
      this.liveAnswer += delta || ''
    },
    addStep(step) {
      this.steps.push(step)
    },
    clearSteps() {
      this.steps = []
    },
    setLoading(b) {
      this.loading = b
    },
    setError(e) {
      this.error = e
    },
    /** Reset query results + streaming state. Preserves templateQuery text. */
    reset() {
      this.parsedQuery = null
      this.executeAnswer = null
      this.enrichment = null
      this.results = []
      this.trace = []
      this.streamingPhase = ''
      this.liveAnswer = ''
      this.steps = []
      this.loading = false
      this.error = null
    },
  },
})
