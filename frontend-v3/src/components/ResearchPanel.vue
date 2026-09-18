/**
 * ResearchPanel — batch research orchestrator (RESEARCH_ORCHESTRATOR_MVP_PLAN.md).
 *
 * One big prompt ("plan a 2-day trip to Belize City") → the orchestrator LLM
 * decomposes it into parser-ready questions, each answered deterministically
 * by the MapQA executor, then assembles a final summary. Progress streams
 * over SSE from GET /api/nca/research/stream/.
 *
 * Props:
 *   countryName  - Required. Country to research within.
 *   snapshotDate - Optional. Snapshot date string (e.g. "2025_12_31").
 */
<template>
  <div class="d-flex flex-column gap-2">
    <form class="d-flex flex-column gap-2" @submit.prevent="runResearch">
      <label class="form-label small text-secondary mb-0">
        Research prompt (decomposed into parser-ready questions)
      </label>
      <textarea
        v-model="prompt"
        class="form-control form-control-sm font-monospace"
        rows="3"
        placeholder="Plan a 2-day trip to Belize City"
      ></textarea>
      <button
        type="submit"
        class="btn btn-primary btn-sm"
        :disabled="isRunning || !prompt.trim()"
      >
        <span v-if="isRunning" class="spinner-border spinner-border-sm me-1" role="status" aria-hidden="true"></span>
        {{ isRunning ? 'Researching…' : 'Run research' }}
      </button>
    </form>

    <div v-if="error" class="alert alert-danger small py-1 px-2 mb-0">
      {{ error }}
    </div>

    <!-- Decomposed questions + per-question answers -->
    <div v-if="questions.length" class="d-flex flex-column gap-1">
      <div class="small text-secondary">Research questions</div>
      <div
        v-for="q in questions"
        :key="q.index"
        class="border rounded small p-2"
        :class="q.error ? 'border-danger' : 'border-secondary-subtle'"
      >
        <div class="d-flex align-items-center gap-1">
          <span class="badge bg-secondary">{{ q.index + 1 }}</span>
          <span class="flex-grow-1">{{ q.question }}</span>
          <span v-if="q.error" class="badge bg-danger">error</span>
          <span v-else-if="q.template" class="badge bg-info text-dark">{{ q.template }}</span>
        </div>
        <div v-if="q.error" class="text-danger mt-1">{{ q.error }}</div>
        <div v-else-if="q.answer" class="text-secondary mt-1">
          {{ q.answer }}
          <span v-if="q.result_count != null">({{ q.result_count }} results)</span>
        </div>
        <div v-else-if="isRunning" class="text-secondary mt-1">parsing…</div>
      </div>
    </div>

    <!-- Follow-up tool calls -->
    <div v-if="tools.length" class="d-flex flex-column gap-1">
      <div class="small text-secondary">Follow-up searches</div>
      <div v-for="(t, i) in tools" :key="i" class="border rounded small p-2 border-secondary-subtle">
        <span class="badge bg-success">{{ t.tool }}</span>
        <code class="ms-1">{{ JSON.stringify(t.args) }}</code>
        <div v-if="t.output" class="text-secondary mt-1">{{ JSON.stringify(t.output).slice(0, 300) }}</div>
      </div>
    </div>

    <!-- Streamed final summary -->
    <div v-if="summary" class="alert alert-success small py-2 px-2 mb-0">
      <div class="text-secondary small mb-1">Summary</div>
      <div style="white-space: pre-wrap;">{{ summary }}</div>
    </div>
  </div>
</template>

<script>
import axios from 'axios'

export default {
  name: 'ResearchPanel',
  props: {
    countryName: {
      type: String,
      required: true,
    },
    snapshotDate: {
      type: String,
      default: null,
    },
  },
  data() {
    return {
      prompt: '',
      isRunning: false,
      questions: [],
      tools: [],
      summary: '',
      error: '',
      eventSource: null,
    }
  },
  methods: {
    runResearch() {
      if (!this.prompt.trim() || this.isRunning) return
      this.resetState()
      this.isRunning = true

      const params = new URLSearchParams({ prompt: this.prompt.trim() })
      if (this.countryName) params.set('country_code', this.countryName)
      if (this.snapshotDate) params.set('snapshot_date', this.snapshotDate)

      const base = axios.defaults.baseURL || ''
      const url = `${base}/nca/research/stream/?${params}`
      const es = new EventSource(url)
      this.eventSource = es

      es.addEventListener('plan', (e) => {
        const d = JSON.parse(e.data)
        this.questions = (d.questions || []).map((q, index) => ({
          index,
          question: q.question,
          why: q.why || '',
          template: null,
          answer: '',
          result_count: null,
          error: '',
        }))
      })

      es.addEventListener('question', (e) => {
        const d = JSON.parse(e.data)
        const i = d.index
        if (this.questions[i]) {
          this.questions[i].template = d.template || null
          this.questions[i].answer = d.answer || ''
          this.questions[i].result_count = d.result_count != null ? d.result_count : null
          this.questions[i].error = d.error || ''
        }
      })

      es.addEventListener('tool', (e) => {
        const d = JSON.parse(e.data)
        this.tools.push({ tool: d.tool, args: d.args || {}, output: null })
      })

      es.addEventListener('tool_out', (e) => {
        const d = JSON.parse(e.data)
        const t = this.tools[this.tools.length - 1]
        if (t && t.tool === d.tool) t.output = d.output
      })

      es.addEventListener('summary_delta', (e) => {
        const delta = JSON.parse(e.data).delta
        if (delta) this.summary += delta
      })

      es.addEventListener('done', (e) => {
        this.isRunning = false
        this.closeStream()
        const d = JSON.parse(e.data)
        const result = d.result || {}
        if (result.summary && !this.summary) this.summary = result.summary
        if (result.errors && result.errors.length && !this.error) {
          this.error = `Errors: ${result.errors.map((x) => x.error || x.question).join('; ')}`
        }
      })

      es.addEventListener('error', () => {
        if (es.readyState === EventSource.CLOSED && this.isRunning) {
          this.isRunning = false
          this.error = this.error || 'Research stream error'
        }
      })
    },
    resetState() {
      this.questions = []
      this.tools = []
      this.summary = ''
      this.error = ''
      this.closeStream()
    },
    closeStream() {
      if (this.eventSource) {
        this.eventSource.close()
        this.eventSource = null
      }
    },
  },
  beforeUnmount() {
    this.closeStream()
  },
}
</script>
