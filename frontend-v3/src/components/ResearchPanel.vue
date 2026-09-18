/**
 * ResearchPanel — interactive research with the KE interviewer
 * (RESEARCH_ORCHESTRATOR_MVP_PLAN.md).
 *
 * The "research prompt" section is a live chat with the KE (Knowledge
 * Engineer) on the interactive LLM (the 4070). The KE asks clarifying
 * questions and ends its interview with a BRIEF: line; the panel extracts
 * that brief and runs the research loop (decompose → deterministic
 * executor → grounded summary) over SSE from GET /api/nca/research/stream/.
 * After the summary, the user can keep chatting to revise the brief and
 * re-run (second pass).
 *
 * Chat transport: Vercel AI SDK (@ai-sdk/vue useChat + DefaultChatTransport)
 * against POST /api/nca/research/chat/, which speaks the AI SDK v7
 * UI-message-stream protocol.
 *
 * Props:
 *   countryName  - Required. Country to research within.
 *   snapshotDate - Optional. Snapshot date string (e.g. "2025_12_31").
 */
<template>
  <div class="d-flex flex-column gap-2">
    <!-- Chat: the research prompt section -->
    <div class="d-flex flex-column gap-1">
      <label class="form-label small text-secondary mb-0">
        Research prompt (interview with the research assistant)
      </label>
      <div
        class="border rounded small p-2 d-flex flex-column gap-1"
        style="max-height: 240px; overflow-y: auto;"
      >
        <div
          v-for="(m, i) in chatMessages"
          :key="i"
          class="d-flex"
          :class="m.role === 'user' ? 'justify-content-end' : ''"
        >
          <div
            class="rounded px-2 py-1"
            :class="m.role === 'user' ? 'bg-primary text-white' : 'bg-secondary-subtle'"
            style="max-width: 90%;"
          >{{ m.text }}</div>
        </div>
        <div v-if="isChatLoading" class="text-secondary">typing…</div>
      </div>
      <form class="d-flex gap-2" @submit.prevent="sendMessage">
        <input
          v-model="draft"
          type="text"
          class="form-control form-control-sm"
          placeholder="e.g. plan a 2-day trip to Belize City"
          :disabled="isChatLoading"
        />
        <button
          type="submit"
          class="btn btn-primary btn-sm text-nowrap"
          :disabled="isChatLoading || !draft.trim()"
        >Send</button>
      </form>
    </div>

    <!-- Extracted brief + run -->
    <div v-if="finalizing && !brief" class="small text-secondary">
      Building brief from the interview…
    </div>
    <div v-if="brief" class="alert alert-info small py-1 px-2 mb-0">
      <div class="text-secondary small mb-1">
        Research brief
        <span v-if="briefSource === 'fallback'" class="text-secondary">(from your first message)</span>
      </div>
      <div class="font-monospace">{{ brief }}</div>
    </div>
    <button
      type="button"
      class="btn btn-primary btn-sm"
      :disabled="isRunning || (!brief && !lastUserPrompt)"
      @click="runResearch"
    >
      <span v-if="isRunning" class="spinner-border spinner-border-sm me-1" role="status" aria-hidden="true"></span>
      {{ isRunning ? 'Researching…' : 'Run research' }}
    </button>

    <div v-if="error" class="alert alert-danger small py-1 px-2 mb-0">
      {{ error }}
    </div>

    <!-- Decomposed questions + per-question answers (collapsible — same
         details/summary pattern as the Execution trace in
         SemanticSearchPanel). Open while the run streams so live progress
         is visible; auto-collapses when the summary starts. -->
    <div v-if="questions.length" class="d-flex flex-column gap-1">
      <details ref="questionsDetails" class="research-details small text-secondary" open>
        <summary class="cursor-pointer">Execution Trace - Research questions ({{ questions.length }})</summary>
        <div class="d-flex flex-column gap-1 mt-1">
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
      </details>
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
import { DefaultChatTransport } from 'ai'
import { useChat } from '@ai-sdk/vue'

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
  setup(props) {
    // Rule 1.3 hybrid: acquire the composable (and its transport) here,
    // everything else stays Options API.
    const base = axios.defaults.baseURL || ''
    const chat = useChat({
      transport: new DefaultChatTransport({
        api: `${base}/nca/research/chat/`,
        body: () => ({
          country_code: props.countryName,
          snapshot_date: props.snapshotDate,
        }),
      }),
    })
    return { chat }
  },
  data() {
    return {
      draft: '',
      isRunning: false,
      questions: [],
      tools: [],
      summary: '',
      error: '',
      eventSource: null,
      runStartAt: null,
      finalizedBrief: '',
      briefSource: '',
      finalizing: false,
    }
  },
  computed: {
    chatMessages() {
      // UIMessage shape: role + parts[{type:'text', text}].
      return (this.chat.messages.value || [])
        .map((m) => ({
          role: m.role,
          text: (m.parts || [])
            .filter((p) => p.type === 'text')
            .map((p) => p.text || '')
            .join(''),
        }))
        .filter((m) => m.text)
    },
    isChatLoading() {
      const status = this.chat.status.value
      return status === 'streaming' || status === 'generating'
    },
    brief() {
      // The structured brief from POST /api/nca/research/finalize/ —
      // extracted server-side (chat_json fields + deterministic render),
      // so the KE's free-form BRIEF runaway-list failure cannot recur.
      return this.finalizedBrief
    },
    lastUserPrompt() {
      for (let i = this.chatMessages.length - 1; i >= 0; i--) {
        if (this.chatMessages[i].role === 'user') return this.chatMessages[i].text
      }
      return ''
    },
    lastChatMessageText() {
      const msgs = this.chatMessages
      return msgs.length ? msgs[msgs.length - 1].text : ''
    },
    chatStatus() {
      return this.chat.status.value
    },
    chatError() {
      return this.chat.error.value
    },
  },
  // Diagnostic signals — helpful while testing the interview + run live;
  // trim once the flows are stable (same convention as SemanticSearchPanel).
  watch: {
    chatStatus(status) {
      console.log('[Research] chat status:', status, '@', Date.now())
      // Rebuild the brief after each completed KE reply (structured
      // extraction; the KE never emits a brief itself).
      if (
        status === 'ready'
        && this.chatMessages.some((m) => m.role === 'assistant')
        && !this.finalizing
        && !this.isRunning
      ) {
        this.finalizeBrief()
      }
    },
    chatError(err) {
      if (err) console.error('[Research] chat error:', err, '@', Date.now())
    },
    lastChatMessageText(text) {
      console.log('[Research] chat message:', text.slice(0, 200), '@', Date.now())
    },
    brief(val) {
      console.log(val
        ? `[Research] brief ready (${val.length} chars, source: ${this.briefSource})`
        : '[Research] brief cleared', '@', Date.now())
    },
  },
  mounted() {
    const base = axios.defaults.baseURL || ''
    console.log('[Research] mounted, chat api:', `${base}/nca/research/chat/`, '@', Date.now())
  },
  methods: {
    sendMessage() {
      const text = this.draft.trim()
      if (!text || this.isChatLoading) return
      console.log('[Research] send:', text.slice(0, 120), '@', Date.now())
      this.draft = ''
      this.chat.sendMessage({ text })
    },
    async finalizeBrief() {
      if (this.finalizing) return
      const msgs = this.chatMessages.map((m) => ({ role: m.role, content: m.text }))
      if (!msgs.length) return
      this.finalizing = true
      console.log('[Research] finalize start', '@', Date.now())
      try {
        const payload = { messages: msgs }
        if (this.countryName) payload.country_code = this.countryName
        if (this.snapshotDate) payload.snapshot_date = this.snapshotDate
        const { data } = await axios.post(
          `${axios.defaults.baseURL || ''}/nca/research/finalize/`,
          payload,
        )
        this.finalizedBrief = data.brief || ''
        this.briefSource = data.source || ''
        console.log('[Research] finalize:', data.source, (data.brief || '').slice(0, 120), '@', Date.now())
      } catch (e) {
        console.error('[Research] finalize error:', e.response?.data?.error || e.message, '@', Date.now())
      } finally {
        this.finalizing = false
      }
    },
    async runResearch() {
      if (this.isRunning) return
      let prompt = this.brief
      if (!prompt) {
        await this.finalizeBrief()
        prompt = this.brief
      }
      prompt = prompt || this.lastUserPrompt
      if (!prompt) return
      this.resetRunState()
      this.isRunning = true
      this.runStartAt = Date.now()
      const source = this.brief ? 'brief' : 'lastUserPrompt'
      console.log('[Research] run start, source:', source, 'prompt:', prompt.slice(0, 120), '@', Date.now())

      const params = new URLSearchParams({ prompt })
      if (this.countryName) params.set('country_code', this.countryName)
      if (this.snapshotDate) params.set('snapshot_date', this.snapshotDate)

      const base = axios.defaults.baseURL || ''
      const url = `${base}/nca/research/stream/?${params}`
      console.log('[Research] stream url:', url, '@', Date.now())
      const es = new EventSource(url)
      this.eventSource = es

      es.addEventListener('plan', (e) => {
        const d = JSON.parse(e.data)
        console.log('[Research] plan:', (d.questions || []).length, 'questions', '@', Date.now())
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
        console.log('[Research] question:', i, d.template, 'count:', d.result_count, d.error || '', '@', Date.now())
        if (this.questions[i]) {
          this.questions[i].template = d.template || null
          this.questions[i].answer = d.answer || ''
          this.questions[i].result_count = d.result_count != null ? d.result_count : null
          this.questions[i].error = d.error || ''
        }
      })

      es.addEventListener('tool', (e) => {
        const d = JSON.parse(e.data)
        console.log('[Research] tool call:', d.tool, JSON.stringify(d.args || {}), '@', Date.now())
        this.tools.push({ tool: d.tool, args: d.args || {}, output: null })
      })

      es.addEventListener('tool_out', (e) => {
        const d = JSON.parse(e.data)
        console.log('[Research] tool output:', d.tool, '@', Date.now())
        const t = this.tools[this.tools.length - 1]
        if (t && t.tool === d.tool) t.output = d.output
      })

      es.addEventListener('summary_delta', (e) => {
        const delta = JSON.parse(e.data).delta
        if (delta) {
          if (!this.summary) {
            console.log('[Research] summary stream started', '@', Date.now())
            this.collapseQuestions()
          }
          this.summary += delta
        }
      })

      es.addEventListener('done', (e) => {
        this.isRunning = false
        this.closeStream()
        const d = JSON.parse(e.data)
        const result = d.result || {}
        const elapsed = this.runStartAt ? ((Date.now() - this.runStartAt) / 1000).toFixed(1) + 's' : '?'
        console.log('[Research] done, summary len:', (result.summary || this.summary || '').length, 'elapsed:', elapsed, 'errors:', (result.errors || []).length, '@', Date.now())
        this.runStartAt = null
        if (result.summary && !this.summary) {
          // Summary arrived without a stream (edge case) — collapse too.
          this.summary = result.summary
          this.collapseQuestions()
        }
        if (result.errors && result.errors.length && !this.error) {
          this.error = `Errors: ${result.errors.map((x) => x.error || x.question).join('; ')}`
        }
      })

      es.addEventListener('error', () => {
        console.log('[Research] stream error, readyState:', es.readyState, 'running:', this.isRunning, '@', Date.now())
        if (es.readyState === EventSource.CLOSED && this.isRunning) {
          this.isRunning = false
          this.error = this.error || 'Research stream error'
        }
      })
    },
    resetRunState() {
      this.questions = []
      this.tools = []
      this.summary = ''
      this.error = ''
      this.closeStream()
    },
    collapseQuestions() {
      const el = this.$refs.questionsDetails
      if (el) el.open = false
    },
    closeStream() {
      if (this.eventSource) {
        console.log('[Research] closing stream', '@', Date.now())
        this.eventSource.close()
        this.eventSource = null
      }
    },
  },
  beforeUnmount() {
    console.log('[Research] unmount', '@', Date.now())
    this.closeStream()
  },
}
</script>

<style scoped>
/* Minimal residual CSS — Bootstrap utilities cover the rest.
   cursor-pointer for <summary> (Bootstrap provides no utility),
   plus the questions caret: red while closed (attention), gray when
   open — mirrors SemanticSearchPanel's Execution trace pattern. */
.cursor-pointer {
  cursor: pointer;
}

.research-details > summary::marker {
  color: var(--bs-danger);
}
.research-details > summary::-webkit-details-marker {
  color: var(--bs-danger);
}
.research-details[open] > summary::marker {
  color: var(--bs-secondary);
}
.research-details[open] > summary::-webkit-details-marker {
  color: var(--bs-secondary);
}
</style>
