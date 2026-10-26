// ── Research-loop SSE stream (extracted from ResearchPanel.vue,
// Streams a research run over GET /api/nca/research/stream/. Each SSE
// event is a row in RESEARCH_EVENT_HANDLERS: (self, data, stream) → state
// mutations. `self` is the component instance — handlers mutate its data
// and call its methods; `stream` is the EventSource (the error handler
// inspects readyState). Same shape as services/templateStream.js.

import axios from 'axios'
import { NCA_RESEARCH_STREAM } from './apiPaths'

// ── Per-event handlers ──────────────────────────────────────────────────

const RESEARCH_EVENT_HANDLERS = {
  plan(self, data) {
    self.recordEvent('plan', data)
    self.questions = (data.questions || []).map((item, index) => ({
      index,
      question: item.question,
      why: item.why || '',
      template: null,
      answer: '',
      digest: '',
      result_count: null,
      error: '',
    }))
  },

  question(self, data) {
    self.recordEvent('question', data)
    const i = data.index
    if (self.questions[i]) {
      self.questions[i].template = data.template || null
      self.questions[i].answer = data.answer || ''
      self.questions[i].digest = data.digest || ''
      self.questions[i].result_count = data.result_count != null ? data.result_count : null
      self.questions[i].error = data.error || ''
    } else {
      // Round-2 continuation rows (indexes continue past round 1).
      self.questions.push({
        index: i,
        question: data.question || '',
        template: data.template || null,
        answer: data.answer || '',
        digest: data.digest || '',
        result_count: data.result_count != null ? data.result_count : null,
        error: data.error || '',
        round: data.round || 1,
      })
    }
  },

  follow_up(self, data) {
    self.recordEvent('follow_up', data)
    // Visible gap-repair re-ask: probe / class-swap / llm_replan rows
    // append to the trace so the user sees the follow-up questions.
    self.questions.push({
      index: self.questions.length,
      question: data.question || '',
      template: data.template || null,
      answer: data.answer || '',
      digest: '',
      result_count: data.result_count != null ? data.result_count : null,
      error: data.error || '',
      follow_up: true,
      repair_of: data.repair_of,
    })
  },

  tool(self, data) {
    self.recordEvent('tool', data)
    self.tools.push({ tool: data.tool, args: data.args || {}, output: null })
  },

  tool_out(self, data) {
    self.recordEvent('tool_out', data)
    const lastTool = self.tools[self.tools.length - 1]
    if (lastTool && lastTool.tool === data.tool) lastTool.output = data.output
  },

  replan(self, data) {
    self.recordEvent('replan', data)
  },

  round(self, data) {
    self.recordEvent('round', data)
  },

  summary_delta(self, data) {
    self.recordEvent('summary_delta', data)
    const delta = data.delta
    if (delta) {
      if (!self.summary) {
        self.collapseQuestions()
      }
      self.summary += delta
    }
  },

  done(self, data) {
    self.isRunning = false
    self.closeStream()
    self.recordEvent('done', data)
    const result = data.result || {}
    self.runRecipe = result.recipe || ''
    self.runStartAt = null
    if (result.summary && !self.summary) {
      // Summary arrived without a stream (edge case) — collapse too.
      self.summary = result.summary
      self.collapseQuestions()
    }
    if (result.errors && result.errors.length && !self.error) {
      self.error = `Errors: ${result.errors.map((failure) => failure.error || failure.question).join('; ')}`
    }
    self.pushTwinOverlay(result)
  },

  error(self, _data, stream) {
    self.recordEvent('error', { readyState: stream.readyState })
    if (stream.readyState === EventSource.CLOSED && self.isRunning) {
      self.isRunning = false
      self.error = self.error || 'Research stream error'
    }
  },
}

// ── Params + open ───────────────────────────────────────────────────────

/** URL params for the stream URL — the one place the branches hide. */
export function buildResearchParams({
  prompt,
  countryName,
  snapshotDate,
  subdivisionQid,
  recipe,
}) {
  const params = new URLSearchParams({ prompt })
  if (countryName) params.set('country_code', countryName)
  if (snapshotDate) params.set('snapshot_date', snapshotDate)
  if (subdivisionQid) params.set('subdivision_qid', subdivisionQid)
  if (recipe) params.set('recipe', recipe)
  return params
}

/**
 * Open the research stream and wire every handler in the table.
 * The EventSource lands on `self.eventSource` so the component's
 * closeStream() / resetRunState() lifecycle keeps working.
 */
export function openResearchStream(self, params) {
  const base = axios.defaults.baseURL || ''
  const stream = new EventSource(`${base}${NCA_RESEARCH_STREAM}?${params}`)
  self.eventSource = stream
  for (const [event, handler] of Object.entries(RESEARCH_EVENT_HANDLERS)) {
    stream.addEventListener(event, (message) =>
      handler(self, message.data ? JSON.parse(message.data) : {}, stream),
    )
  }
  return stream
}
