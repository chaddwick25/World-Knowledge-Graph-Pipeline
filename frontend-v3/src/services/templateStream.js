// ── Template-query SSE stream (extracted from SemanticSearchPanel.vue,
//    2026-10-02) ─────────────────────────────────────────────────────────────
// Streams the executor over GET /api/nca/execute-query/stream/. Each SSE
// event is a row in SSE_EVENT_HANDLERS: (self, data, es) → state mutations.
// `self` is the component instance — handlers mutate its data and call its
// methods; `es` is the EventSource (handlers may inspect readyState).

import axios from 'axios'
import { normalizeResult } from '../mapViz/queryGraphBuild'

// ── Per-event handlers ──────────────────────────────────────────────────────

const SSE_EVENT_HANDLERS = {
  parsed(self, d) {
    self.parsedQuery = d.parsed || null
    console.log('[SSE] parsed @', Date.now())
  },

  executed(self, d) {
    // {template, result_count, trace} — render the trace/anchors early.
    self.executeTrace = d.trace || []
    console.log('[SSE] executed @', Date.now(), 'template:', d.template, 'count:', d.result_count)
  },

  answer(self, d) {
    // Deterministic factor-join answer (non-AI) — shown immediately;
    // the LLM enrichment (answer_delta) replaces it when ready.
    console.log('[SSE] answer @', Date.now(), 'len:', (d.answer || '').length, 'text:', (d.answer || '').slice(0, 60))
    if (d.answer) {
      self.executeAnswer = d.answer
      self.dataAnswerAt = Date.now()
      self.dataAnswerElapsed = (Date.now() - self.queryStartAt) / 1000
      self.aiAnswerAt = null
    }
  },

  context(self, d) {
    // Deterministic entity context is in — show an enriching indicator.
    self.enrichingContext = d.context || null
    console.log('[SSE] context @', Date.now())
  },

  answer_delta(self, d) {
    const delta = d.delta
    if (delta) self.enrichedAnswer += delta
    ensureAiAnswerVisible(self)
    console.log('[SSE] answer_delta @', Date.now(), 'deltaLen:', (delta || '').length, 'total:', self.enrichedAnswer.length)
  },

  done(self, d) {
    const result = d.result || {}
    console.log('[SSE] done @', Date.now(), 'enriched:', !!result.enrichment, 'answerLen:', (result.answer || '').length)
    // Data answer stays pinned; the AI answer settles to the final
    // enriched text when present.
    if (result.enrichment?.enriched_answer) {
      self.enrichedAnswer = result.enrichment.enriched_answer
    } else if (!self.executeAnswer) {
      self.executeAnswer = result.answer || null
    }
    ensureAiAnswerVisible(self)
    // Final AI elapsed — the moment the enriched answer was complete.
    self.aiAnswerElapsed = (Date.now() - self.queryStartAt) / 1000
    self.executeTrace = result.trace || self.executeTrace
    if (Array.isArray(result.results)) {
      // Full result set — the Top K control caps display/markers.
      self.results = result.results.map((r) => normalizeResult(r))
    }
    if (result.error) self.error = result.error
    self.searched = true
    self.loading = false
    self.publishResults()
    closeTemplateStream(self)
  },
}

/**
 * Open the streaming executor: GET /api/nca/execute-query/stream/.
 * EventSource is GET-only, so the URL is built from
 * axios.defaults.baseURL (http://localhost:8000/api from main.js) —
 * a relative /nca/... path would hit Vite with no proxy.
 */
export function openTemplateStream(self) {
  const params = new URLSearchParams({ query: self.templateQuery.trim() })
  if (self.countryName) params.set('country_code', self.countryName)
  if (self.snapshotDate) params.set('snapshot_date', self.snapshotDate)

  self.queryStartAt = Date.now()
  const base = axios.defaults.baseURL || ''
  const url = `${base}/nca/execute-query/stream/?${params}`
  // TEMP diagnosis signals — remove after the answer-first timing is confirmed.
  console.log('[SSE] opening stream', url, '@', Date.now())
  const es = new EventSource(url)
  self.eventSource = es

  for (const [event, handler] of Object.entries(SSE_EVENT_HANDLERS)) {
    es.addEventListener(event, (e) => handler(self, JSON.parse(e.data), es))
  }

  es.addEventListener('error', () => {
    // Fires on connection failure OR when the server closes the stream.
    // After a clean `done` this is a no-op; otherwise surface an error.
    console.log('[SSE] error @', Date.now(), 'readyState:', es.readyState, 'searched:', self.searched)
    clearAiAnswerTimer(self)
    if (es.readyState === EventSource.CLOSED && !self.searched) {
      self.error = self.error || 'Stream error'
      self.loading = false
    }
    closeTemplateStream(self)
  })
}

export function closeTemplateStream(self) {
  // NOTE: do not clear the AI-reveal timer here — done() closes the
  // stream, but the buffered AI answer must still appear after the
  // data answer's solo window (the timer fires ~900ms later).
  self.eventSource?.close()
  self.eventSource = null
}

/** Reveal the AI answer region once the data answer has had its
 * minimum solo display window; tokens buffer in the meantime.
 *
 * This controls ONLY when the AI box becomes visible — it must never
 * touch aiAnswerElapsed. That value is set once, at the `done` event
 * (the true enrichment completion time). Stamping it here made the
 * displayed AI latency equal to dataAnswer + 900ms whenever the
 * enrichment finished inside the solo window — the "always 0.9s
 * behind" artifact. */
export function ensureAiAnswerVisible(self) {
  if (self.aiAnswerAt) {
    console.log('[SSE] ensureAi: already visible @', Date.now())
    return
  }
  const elapsed = self.dataAnswerAt
    ? Date.now() - self.dataAnswerAt
    : Number.POSITIVE_INFINITY
  console.log('[SSE] ensureAi @', Date.now(), 'elapsed:', elapsed, 'min:', self.minDataAnswerMs, 'enrichedLen:', self.enrichedAnswer.length)
  if (elapsed >= self.minDataAnswerMs) {
    self.aiAnswerAt = Date.now()
    return
  }
  clearAiAnswerTimer(self)
  self.aiAnswerTimer = setTimeout(() => {
    self.aiAnswerAt = Date.now()
    console.log('[SSE] ensureAi: timer fired @', Date.now())
  }, self.minDataAnswerMs - elapsed)
}

export function clearAiAnswerTimer(self) {
  if (self.aiAnswerTimer) {
    clearTimeout(self.aiAnswerTimer)
    self.aiAnswerTimer = null
  }
}
