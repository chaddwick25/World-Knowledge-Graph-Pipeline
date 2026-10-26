/**
 * ResearchPanel — interactive research with the KE interviewer.
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
 *   countryName    - Required. Country to research within.
 *   snapshotDate   - Optional. Snapshot date string (e.g. "2025_12_31").
 *   subdivisionQid - Optional. Wikidata QID — scopes the run's results to
 *                    the subdivision polygon (2026-10-01).
 *   recipe         - Optional. Hard-selected recipe key ("doppelganger_report"
 *                    on the doppelganger surface) — sent on chat, finalize,
 *                    and stream so routing is bypassed, not guessed (ISSUE-48).
 *   setSeedOverlay - Optional. SurfaceShell slot setter; the panel feeds the
 *                    confirmed seed + candidates to the map through it.
 */
<template>
  <div class="d-flex flex-column gap-2">
    <!-- Chat: the research prompt section -->
    <div class="d-flex flex-column gap-1">
      <div
        class="border rounded small p-2 d-flex flex-column gap-1"
        style="max-height: 240px; overflow-y: auto;"
      >
        <!-- Onboarding greeting: display-only (rendered while the chat
             is empty, never enters the transcript), so the KE's context
             and the finalize extraction stay clean. -->
        <div v-if="!chatMessages.length" class="d-flex">
          <div
            class="rounded px-2 py-1 bg-secondary-subtle"
            style="max-width: 90%;"
          >
            Hello — I'm your doppelganger research assistant. Tell me
            which place to find twins for. I'll pin it on the map for
            you to confirm, then ask a couple of quick questions about
            how matches should compare.
          </div>
        </div>
        <div
          v-for="(msg, i) in chatMessages"
          :key="i"
          class="d-flex"
          :class="msg.role === 'user' ? 'justify-content-end' : ''"
        >
          <div
            class="rounded px-2 py-1"
            :class="msg.role === 'user' ? 'bg-primary text-white' : 'bg-secondary-subtle'"
            style="max-width: 90%;"
          >{{ msg.role === 'assistant' ? assistantDisplay(msg.text) : msg.text }}</div>
        </div>
        <div v-if="isChatLoading" class="text-secondary">typing…</div>
      </div>
      <div
        v-if="showExampleAsk"
        class="d-flex align-items-center gap-2 border rounded p-1 px-2"
        style="font-size: 0.72rem;"
      >
        <span class="flex-grow-1 text-truncate" :title="exampleQuestion">{{ exampleQuestion }}</span>
        <button
          type="button"
          class="btn btn-sm btn-outline-primary text-nowrap py-0 px-2"
          :class="{ 'btn-success': exampleCopied }"
          @click="copyExample()"
        >{{ exampleCopied ? 'Copied!' : 'Copy' }}</button>
      </div>
      <form class="d-flex gap-2 align-items-end" @submit.prevent="sendMessage">
        <textarea
          v-model="draft"
          rows="2"
          class="form-control form-control-sm"
          style="resize: none; min-height: 4.4rem; max-height: 12rem;"
          :disabled="isChatLoading"
          @keydown.enter.exact.prevent="sendMessage"
        ></textarea>
      </form>
    </div>
    <div
      v-if="seed.asked"
      class="border rounded small py-1 px-2"
      :class="seed.hasSeed ? 'border-success-subtle' : 'border-danger-subtle'"
    >
      <div v-if="seed.hasSeed" class="d-flex align-items-center gap-2">
        <span class="badge bg-success">Seed</span>
        <span class="fw-semibold">{{ seed.confirmed.name }}</span>
        <span class="text-secondary">{{ seedKindLabel(seed.confirmed) }}</span>
        <a
          v-if="seedWikidataUrl(seed.confirmed)"
          :href="seedWikidataUrl(seed.confirmed)"
          target="_blank"
          rel="noopener"
          class="ms-auto text-decoration-none"
        >Wikidata ↗</a>
      </div>
      <div v-else-if="!seed.candidates.length" class="text-danger">
        '{{ seed.asked }}' not found in the dataset
        <span v-if="seed.suggestions.length" class="d-block mt-1">
          Did you mean:
          <button
            v-for="suggestion in seed.suggestions"
            :key="suggestion.osm_type + ':' + suggestion.osm_id"
            type="button"
            class="btn btn-sm btn-outline-primary py-0 px-1 me-1"
            @click="restateSeed(suggestion)"
          >{{ suggestion.name }}</button>
        </span>
      </div>
      <div v-if="seedAlternatives.length && seed.confirmedSource !== 'explicit'" class="mt-1">
        <div class="text-secondary">{{ seedMenuHeader }}</div>
        <ol class="mb-0 ps-3 mt-1 d-flex flex-column gap-1">
          <li
            v-for="candidate in seedAlternatives"
            :key="candidate.osm_type + ':' + candidate.osm_id"
            class="d-flex align-items-center gap-2"
          >
            <button
              type="button"
              class="btn btn-sm py-0 px-1 text-start flex-grow-1 text-truncate"
              :class="rowClass(candidate)"
              :title="`Open ${candidate.name} on the map`"
              @click="toggleEntityInfo(candidate)"
              @mouseenter="seed.setHoveredKey(chipKey(candidate))"
              @mouseleave="seed.clearHoveredKey(chipKey(candidate))"
            >
              {{ candidate.name }} <span class="text-secondary">{{ seedKindLabel(candidate) }}</span>
            </button>
          </li>
        </ol>
      </div>
    </div>
    <div v-if="finalizing && !brief" class="small text-secondary">
      Building brief from the interview…
    </div>
    <div v-if="brief" class="alert alert-info small py-1 px-2 mb-0">
      <div class="text-secondary small mb-1">
        {{ isDoppelgangerRun ? 'Doppelganger brief' : 'Research brief' }}
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
      {{ isRunning
        ? (isDoppelgangerRun ? 'Searching doppelgangers…' : 'Researching…')
        : (isDoppelgangerRun ? 'Find doppelgangers' : 'Run research') }}
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
        <summary class="cursor-pointer">
          Execution Trace - {{ isDoppelgangerRun ? 'Doppelganger search' : 'Research questions' }} ({{ questions.length }}<template v-if="tools.length"> + {{ tools.length }} follow-up searches</template>)
        </summary>
        <div class="research-questions-flow d-flex flex-column gap-1 mt-1">
          <div
            v-for="question in questions"
            :key="question.index"
            class="border rounded small p-2"
            :class="question.error ? 'border-danger' : 'border-secondary-subtle'"
          >
            <div class="d-flex align-items-center gap-1">
              <span class="badge bg-secondary">{{ question.follow_up ? '→' : question.index + 1 }}</span>
              <span v-if="question.follow_up" class="badge bg-success">follow-up</span>
              <span v-else-if="question.round > 1" class="badge bg-warning text-dark">round {{ question.round }}</span>
              <span class="flex-grow-1">{{ question.question }}</span>
              <span v-if="question.error" class="badge bg-danger">error</span>
              <span v-else-if="question.template" class="badge bg-info text-dark">{{ question.template }}</span>
            </div>
            <div v-if="question.error" class="text-danger mt-1">{{ question.error }}</div>
            <div v-else-if="isEmptyResult(question)" class="text-secondary">
              (0 results)
            </div>
            <div v-else-if="question.answer" class="mt-1">
              <!-- White plain text, dark blue values (entity names from the
                   result digest + distance/amount tokens). -->
              <template v-for="(seg, si) in answerSegments(question)" :key="si">
                <span :class="seg.value ? 'answer-value' : ''">{{ seg.text }}</span>
              </template>
              <span v-if="question.result_count != null" class="text-secondary"> ({{ question.result_count }} results)</span>
            </div>
            <div v-else-if="isRunning" class="text-secondary mt-1">parsing…</div>
          </div>
          <!-- Follow-up tool calls (the OSM tag query / structuredSearch
               rows) live INSIDE the trace so the full decision flow is
               visible in one place (2026-10-01). -->
          <div v-if="tools.length" class="d-flex flex-column gap-1 border-top pt-1">
            <div class="small text-secondary fw-semibold">Follow-up searches</div>
            <div v-for="(tool, i) in tools" :key="i" class="border rounded small p-2 border-secondary-subtle">
              <div class="d-flex align-items-center gap-1">
                <span class="badge bg-success">{{ tool.tool }}</span>
                <code class="flex-grow-1 text-truncate" :title="JSON.stringify(tool.args)">{{ JSON.stringify(tool.args) }}</code>
              </div>
              <div v-if="toolOutputSummary(tool)" class="text-secondary mt-1">{{ toolOutputSummary(tool) }}</div>
            </div>
          </div>
        </div>
      </details>
    </div>

    <!-- Streamed final summary -->
    <div v-if="summary" class="alert alert-success small py-2 px-2 mb-0">
      <div class="text-secondary small mb-1">Summary</div>
      <div style="white-space: pre-wrap;">{{ summary }}</div>
    </div>

    <!-- ?debug — raw SSE event log (the persisted run trace, client-side) -->
    <details
      v-if="debugMode && eventLog.length"
      class="research-details small text-secondary"
      open
    >
      <summary class="cursor-pointer">
        Event log ({{ eventLog.length }})
      </summary>
      <div
        class="font-monospace border rounded p-2 mt-1 d-flex flex-column gap-1"
        style="max-height: 240px; overflow-y: auto;"
      >
        <div v-for="(entry, i) in eventLog" :key="i" class="text-truncate">
          <span class="text-secondary">{{ entry.at }}</span>
          <span class="badge bg-secondary-subtle text-dark">{{ entry.event }}</span>
          <span :title="JSON.stringify(entry.data)">{{ eventSummary(entry) }}</span>
        </div>
      </div>
    </details>
  </div>
</template>

<script>
import axios from 'axios'
import { DefaultChatTransport } from 'ai'
import { useChat } from '@ai-sdk/vue'
import { useSeedStore } from '../stores/seedStore'
import {
  NCA_RESEARCH_CHAT,
  NCA_RESEARCH_FINALIZE,
  NCA_RESEARCH_SEED_EXAMPLES,
} from '../services/apiPaths'
import {
  buildResearchParams,
  openResearchStream,
} from '../services/researchStream'
import { useEntityInfoStore } from '../stores/entityInfoStore'

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
    subdivisionQid: {
      type: String,
      default: '',
    },
    recipe: {
      type: String,
      default: '',
    },
    countryIso: {
      type: String,
      default: '',
    },
    setSeedOverlay: {
      type: Function,
      default: null,
    },
    // ISSUE-50 Part A: the panel pushes a completed run's twins to the
    // map through this setter (same pattern as setSeedOverlay).
    setTwinsOverlay: {
      type: Function,
      default: null,
    },
  },
  setup(props) {
    // Rule 1.3 hybrid: acquire the composable (and its transport) here,
    // everything else stays Options API.
    const base = axios.defaults.baseURL || ''
    const chat = useChat({
      transport: new DefaultChatTransport({
        api: `${base}${NCA_RESEARCH_CHAT}`,
        body: () => ({
          country_code: props.countryName,
          snapshot_date: props.snapshotDate,
          subdivision_qid: props.subdivisionQid || undefined,
          recipe: props.recipe || undefined,
        }),
      }),
    })
    const seed = useSeedStore()
    const entityInfo = useEntityInfoStore()
    return { chat, seed, entityInfo }
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
      // The in-flight finalize request (shared so Run awaits the actual
      // request instead of polling past it).
      finalizeInFlight: null,
      runRecipe: '',
      // The copyable example ask row: transient "Copied!" confirmation.
      exampleCopied: false,
      exampleCopyTimer: null,
      // ?debug — the SSE event stream rendered as a queryable log pane
      // (the events are already the execution trace; this just shows it).
      debugMode: new URLSearchParams(window.location.search).has('debug'),
      eventLog: [],
      // The first explicit seed pick has been announced to the KE
      // (once per interview: switches stay silent - the finalize's
      // confirmed seed is authoritative, and re-announcing made the KE
      // restart its question every switch, the 2026-10-17 spam loop).
      announcedSeed: false,
      // Real entity examples for the greeting/placeholder (the
      // seed-examples endpoint; single-consumer server data stays
      // component-local). Empty -> generic copy.
      seedExamples: [],
    }
  },
  computed: {
    chatMessages() {
      // UIMessage shape: role + parts[{type:'text', text}]. The KE's
      // [seed] question marker is protocol, not prose — stripped for
      // display (the backend keeps it in the transcript so the
      // slot gate can detect the answer turn).
      return (this.chat.messages.value || [])
        .map((msg) => ({
          role: msg.role,
          text: (msg.parts || [])
            .filter((part) => part.type === 'text')
            .map((part) => part.text || '')
            .join('')
            .replace(/\[seed\]\s*/g, ''),
        }))
        .filter((msg) => msg.text)
    },
    lastSeedPart() {
      // The most recent data-seed part across the transcript (the
      // backend re-emits with the same id, so the latest wins).
      const msgs = this.chat.messages.value || []
      for (let i = msgs.length - 1; i >= 0; i--) {
        const part = (msgs[i].parts || []).find(
          (messagePart) => messagePart.type === 'data-seed',
        )
        if (part) return part
      }
      return null
    },
    /**
     * The seed state that matters to the overlay + announcement,
     * content-addressed. The deep store watch this replaces ALSO fired
     * on hoveredKey mutations (every chip/info-box hover), and each
     * fire pushed a fresh overlay object whose map re-fit panned the
     * map under the cursor - the click-storm behind the 2026-10-17
     * Jerk Center session (announcement/KE-re-ask loop).
     */
    seedSignature() {
      const seed = this.seed
      return JSON.stringify({
        asked: seed.asked,
        confirmed: seed.confirmed
          ? `${seed.confirmed.osm_type}:${seed.confirmed.osm_id}` : null,
        candidates: (seed.candidates || [])
          .map((candidate) => `${candidate.osm_type}:${candidate.osm_id}`),
      })
    },
    seedMenuHeader() {
      // String building stays in JS (template logic rule): the header
      // reflects the picked state once a seed is confirmed.
      const seed = this.seed
      return seed.confirmed
        ? `Picked ${seed.confirmed.name} - seed from the info box`
        : 'Multiple matches - click a row to open it on the map'
    },
    /** Candidate rows excluding the confirmed seed: the switchable set.
     *  Always rendered while non-empty, so a picked seed stays
     *  changeable (2026-10-19: no re-interview needed to re-pick). */
    seedAlternatives() {
      const seed = this.seed
      const confirmedKey = seed.confirmed
        ? `${seed.confirmed.osm_type}:${seed.confirmed.osm_id}`
        : null
      return (seed.candidates || []).filter(
        (candidate) => `${candidate.osm_type}:${candidate.osm_id}` !== confirmedKey,
      )
    },
    /** A real entity name from the country's dataset for the greeting
     *  example; '' when none (generic copy takes over). */
    seedExampleName() {
      const first = this.seedExamples.find((example) => example && example.name)
      return first ? first.name : ''
    },
    /** The always-visible copyable example ask above the input. */
    exampleQuestion() {
      const name = this.seedExampleName
      return name
        ? `help me find a doppelganger for ${name}`
        : 'help me find a doppelganger for a place you know'
    },
    /** The example ask shows only while its job is undone: nothing to
     *  disambiguate and no seed set. Candidates or a confirmed seed
     *  retire it (2026-10-21). */
    showExampleAsk() {
      return !this.seed.candidates.length && !this.seed.hasSeed
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
    isDoppelgangerRun() {
      // The recipe hard-select decides it on the doppelganger surface
      // (ISSUE-48); elsewhere the Seed: line is the routing marker (the
      // finalize's verified seed) and the done payload's recipe key
      // confirms it after the run. The doppelganger vocabulary stays
      // user-facing wherever the run is one (owner direction, 2026-10-12).
      return (
        this.recipe === 'doppelganger_report'
        || (this.brief || '').includes('Seed:')
        || this.runRecipe === 'doppelganger_report'
      )
    },
    chatStatus() {
      return this.chat.status.value
    },
    chatError() {
      return this.chat.error.value
    },
  },
  watch: {
    countryIso: {
      immediate: true,
      handler(iso) {
        if (iso) {
          this.fetchSeedExamples(iso)
        } else {
          this.seedExamples = []
        }
      },
    },
    lastSeedPart(part) {
      if (part) this.applySeedPart(part)
    },
    // Seed state that changes the overlay or needs announcing. Hover
    // state (hoveredKey) is deliberately NOT watched: it must never
    // push the overlay or re-fit the map.
    seedSignature() {
      this.pushSeedOverlay()
      this.announceExplicitSeed()
    },
    chatStatus(status) {
      // Rebuild the brief after each completed KE reply (structured
      // extraction; the KE never emits a brief itself).
      if (
        status === 'ready'
        && this.chatMessages.some((msg) => msg.role === 'assistant')
        && !this.finalizing
        && !this.isRunning
      ) {
        this.finalizeBrief()
      }
    },
    chatError(err) {
      if (err) console.error('[Research] chat error:', err, '@', Date.now())
    },
  },
  methods: {
    sendMessage() {
      const text = this.draft.trim()
      if (!text || this.isChatLoading) return
      this.draft = ''
      this.chat.sendMessage({ text })
    },
    /** Real entity examples for the greeting/placeholder. Fail-soft:
     *  an empty list renders the generic copy. */
    async fetchSeedExamples(iso) {
      if (!iso) return // invariant: callers check before calling
      try {
        const { data } = await axios.get(
          NCA_RESEARCH_SEED_EXAMPLES,
          { params: { country_code: iso } },
        )
        this.seedExamples = data.examples || []
      } catch (e) {
        this.seedExamples = []
      }
    },
    // ── Live seed (data-seed part → seedStore → map overlay) ──────────
    applySeedPart(part) {
      this.seed.applySeedPart(part.data || {})
      this.pushSeedOverlay()
    },
    /**
     * Seed rows → the map overlay through the SurfaceShell slot setter.
     * The flat seed shape ({lat, lon}) adapts to the marker shape
     * ({geom: {lat, lon}}) here, at the panel boundary; seed_kind
     * discriminates confirmed-pin vs candidate-marker styling.
     */
    pushSeedOverlay() {
      if (!this.setSeedOverlay) return
      const seed = this.seed
      const toMarker = (row, kind) => ({
        osm_id: row.osm_id,
        osm_type: row.osm_type,
        name: row.name,
        tags: row.tags || {},
        wkg_class: row.wkg_class || null,
        geom: { lat: row.lat, lon: row.lon },
        seed_kind: kind,
      })
      // All candidates stay clickable after a pick (2026-10-19: the
      // user can change the seed by clicking again, no re-interview
      // needed). The confirmed entity is excluded from the amber set.
      const confirmedKey = seed.confirmed
        ? `${seed.confirmed.osm_type}:${seed.confirmed.osm_id}`
        : null
      const candidates = (seed.candidates || [])
        .filter((candidate) => candidate.lat != null
          && `${candidate.osm_type}:${candidate.osm_id}` !== confirmedKey)
        .map((candidate) => toMarker(candidate, 'candidate'))
      const confirmed = seed.hasSeed ? toMarker(seed.confirmed, 'confirmed') : null
      this.setSeedOverlay({ confirmed, candidates })
    },
    /**
     * The completed run's twins -> the map overlay through the
     * SurfaceShell slot setter (ISSUE-50 Part A). The authoritative
     * source is the done event's result.tool_calls similarEntities
     * output; rows adapt to the marker shape here, at the panel
     * boundary, like pushSeedOverlay. No twins (or no setter) clears
     * the overlay.
     */
    pushTwinOverlay(result) {
      if (!this.setTwinsOverlay) return
      const seen = new Set()
      const twins = []
      const addRows = (rows) => {
        for (const row of (rows || [])) {
          if (!row || row.lat == null || row.lon == null) continue
          const key = `${row.osm_type || ''}:${row.osm_id || ''}`
          if (seen.has(key)) continue
          seen.add(key)
          twins.push({
            osm_id: row.osm_id,
            osm_type: row.osm_type,
            name: row.name || (row.tags || {}).name || '',
            tags: row.tags || {},
            wkg_class: row.wkg_class || null,
            similarity: row.similarity,
            // The pre-plan twin rows carry the calibrated `score` field
            // (EvidenceScoringService); accept it first, then the raw
            // service fields.
            twin_score: row.score != null ? row.score
              : (row.twin_score != null ? row.twin_score : null),
            geom: { lat: row.lat, lon: row.lon },
          })
        }
      }
      for (const toolCall of (result?.tool_calls || [])) {
        if (toolCall.tool !== 'similarEntities') continue
        const out = toolCall.output
        // Actual shape: a flat list of scored twin rows. Dict shape
        // ({results: [...]}) is the fallback for other tool paths.
        if (Array.isArray(out)) addRows(out)
        else addRows(out?.results)
      }
      const seedStore = this.seed
      const seed = (seedStore.hasSeed && seedStore.confirmed.lat != null)
        ? { geom: { lat: seedStore.confirmed.lat, lon: seedStore.confirmed.lon } }
        : null
      if (!twins.length) {
        this.setTwinsOverlay(null)
        return
      }
      this.setTwinsOverlay({ seed, twins })
    },
    /** Copy the example ask to the clipboard with a brief confirmation
     *  (same pattern as the sample-question rows). */
    copyExample() {
      const text = this.exampleQuestion
      navigator.clipboard?.writeText(text).catch(() => {})
      this.exampleCopied = true
      clearTimeout(this.exampleCopyTimer)
      this.exampleCopyTimer = setTimeout(() => {
        this.exampleCopied = false
      }, 1500)
    },
    assistantDisplay(text) {
      const names = [this.seed.asked, this.seed.confirmed?.name]
        .filter(Boolean)
      for (const name of names) {
        const re = new RegExp(
          `^${name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\s*[,;:.!?-]?\\s*`,
          'i',
        )
        const stripped = (text || '').replace(re, '')
        if (stripped !== text) return stripped
      }
      return text || ''
    },
    /** The entity-kind word for the chip: first descriptive tag value. */
    seedKindLabel(row) {
      const tags = row.tags || {}
      return tags.amenity || tags.shop || tags.tourism || tags.leisure
        || tags.natural || ''
    },
    seedWikidataUrl(row) {
      const qid = (row.tags || {}).wikidata
      return qid ? `https://www.wikidata.org/wiki/${qid}` : ''
    },
    /** One-click restate: the suggestion chip fills the draft and sends,
     * so the [seed] gate re-resolves against the corrected name. */
    restateSeed(suggestion) {
      this.draft = suggestion.name || ''
      this.sendMessage()
    },
    chipKey(candidate) {
      return `${candidate.osm_type}:${candidate.osm_id}`
    },
    /** The candidate rows OPEN/HIDE the entity's info box on the map
     *  (2026-10-19): seeding happens only inside the info box. */
    toggleEntityInfo(candidate) {
      const key = this.chipKey(candidate)
      const current = this.entityInfo.entity
      const currentKey = current
        ? `${current.osm_type}:${current.osm_id}`
        : null
      if (currentKey === key) {
        this.entityInfo.clearEntity(key)
        return
      }
      this.entityInfo.setEntity({
        osm_id: candidate.osm_id,
        osm_type: candidate.osm_type,
        name: candidate.name,
        tags: candidate.tags || {},
        wkg_class: candidate.wkg_class || null,
        lat: candidate.lat,
        lon: candidate.lon,
        geom: { lat: candidate.lat, lon: candidate.lon },
      }, 'panel')
    },
    /** Row state: solid when this entity's info box is open on the map
     *  or the info box hovers it (shared hoveredKey), outline otherwise. */
    rowClass(candidate) {
      const key = this.chipKey(candidate)
      const current = this.entityInfo.entity
      const open = current
        && `${current.osm_type}:${current.osm_id}` === key
      return (open || key === this.seed.hoveredKey)
        ? 'btn-outline-primary'
        : 'btn-outline-secondary'
    },
    /**
     * The FIRST explicit pick (map marker, info-box button, list chip)
     * is announced to the KE as a "Seed:" user message - the existing
     * handshake format (ISSUE-48 follow-up). Once per interview:
     * switches stay silent (the finalize's confirmed seed is
     * authoritative; re-announcing made the KE restart its question on
     * every switch - the 2026-10-17 spam loop). The resolution path
     * never announces (the user's answer is already in the transcript).
     */
    announceExplicitSeed() {
      const seed = this.seed
      if (this.announcedSeed) return
      if (seed.confirmedSource !== 'explicit' || !seed.confirmed) return
      if (this.isChatLoading) return
      this.announcedSeed = true
      const kind = this.seedKindLabel(seed.confirmed)
      this.chat.sendMessage({
        text: `Seed: ${seed.confirmed.name}` + (kind ? ` (${kind})` : ''),
      })
    },
    async finalizeBrief() {
      if (this.finalizing && this.finalizeInFlight) {
        // A finalize is already in flight (the watch fired on the KE's
        // last reply): pressing Run during it awaits THE REQUEST, not a
        // fixed poll. The old 8s poll gave up while a 10-14s finalize
        // was still going and Run showed "could not build the brief"
        // on a request that then succeeded (the 2026-10-17 Jerk Center
        // session; the race the deferred hardening ticket described).
        return await this.finalizeInFlight
      }
      const msgs = this.chatMessages.map((msg) => ({ role: msg.role, content: msg.text }))
      if (!msgs.length) return ''
      this.finalizing = true
      this.finalizeInFlight = this.requestBrief(msgs)
      try {
        return await this.finalizeInFlight
      } finally {
        this.finalizing = false
        this.finalizeInFlight = null
      }
    },
    async requestBrief(msgs) {
      const payload = { messages: msgs }
      if (this.countryName) payload.country_code = this.countryName
      if (this.snapshotDate) payload.snapshot_date = this.snapshotDate
      if (this.subdivisionQid) payload.subdivision_qid = this.subdivisionQid
      if (this.recipe) payload.recipe = this.recipe
      // The live-confirmed seed is authoritative: finalize skips
      // re-geocoding and renders the Seed line from it (ISSUE-48).
      if (this.seed.hasSeed) payload.seed = this.seed.finalizePayload()
      try {
        const { data } = await axios.post(
          NCA_RESEARCH_FINALIZE,
          payload,
          // Above the backend's realistic chat_json latency on a busy
          // 4070: the 15s timeout aborted requests the server would
          // have answered.
          { timeout: 20000 },
        )
        this.finalizedBrief = data.brief || ''
        this.briefSource = data.source || ''
      } catch (e) {
        console.error('[Research] finalize error:', e.response?.data?.error || e.message, '@', Date.now())
      }
      return this.finalizedBrief || ''
    },
    async runResearch() {
      if (this.isRunning) return
      let prompt = this.brief || (await this.finalizeBrief())
      // An interview's last user message is an ANSWER fragment ("all",
      // "geosemantic data"), not a research prompt: running on it invents
      // a generic census that ignores everything the interview
      // established (the 2026-10-12 Redbones run: the brief was missing
      // at Run time, this fallback fired, and the loop produced a
      // FOOD_REPORT census with no subject). The lastUserPrompt fallback
      // is valid only when there is no interview to lose: a single user
      // message IS the prompt.
      const userMessages = this.chatMessages.filter(
        (msg) => msg.role === 'user',
      )
      if (!prompt && userMessages.length > 1) {
        this.error = 'Could not build the research brief from the interview. Press Run again to retry; if it persists, rephrase the request.'
        return
      }
      prompt = prompt || this.lastUserPrompt
      if (!prompt) return
      this.resetRunState()
      this.isRunning = true
      this.runStartAt = Date.now()

      openResearchStream(this, buildResearchParams({
        prompt,
        countryName: this.countryName,
        snapshotDate: this.snapshotDate,
        subdivisionQid: this.subdivisionQid,
        recipe: this.recipe,
      }))
    },
    resetRunState() {
      this.questions = []
      this.tools = []
      this.summary = ''
      this.error = ''
      this.runRecipe = ''
      this.eventLog = []
      if (this.setTwinsOverlay) this.setTwinsOverlay(null)
      this.closeStream()
    },
    /**
     * Append an SSE event to the ?debug event log. One compact row per
     * event — the same records the backend persists on ResearchRun.
     */
    recordEvent(event, data) {
      if (!this.debugMode) return
      this.eventLog.push({
        event,
        at: new Date().toISOString().slice(11, 23),
        data,
      })
    },
    /** One-line summary of a logged event for the debug pane. */
    eventSummary(entry) {
      const data = entry.data || {}
      switch (entry.event) {
        case 'plan': return `${(data.questions || []).length} questions`
        case 'question': return `#${data.index} ${data.template || ''} count=${data.result_count ?? '?'}`
        case 'follow_up': return `${data.kind || ''} ${data.question || ''}`.trim()
        case 'replan': return data.question || ''
        case 'round': return `round ${data.round}`
        case 'tool': return `${data.tool} ${JSON.stringify(data.args || {})}`
        case 'tool_out': return `${data.tool} ${JSON.stringify(data.output || {}).slice(0, 120)}`
        case 'summary_delta': return JSON.stringify(data.delta || '')
        case 'done': return `result: ${Object.keys(data.result || {}).join(', ')}`
        case 'error': return `readyState=${data.readyState}`
        default: return JSON.stringify(data).slice(0, 160)
      }
    },
    collapseQuestions() {
      const el = this.$refs.questionsDetails
      if (el) el.open = false
    },
    /**
     * Compact summary of a follow-up tool's output: for structuredSearch
     * (the OSM tag query) "N results — top names…"; otherwise the raw
     * output truncated. Empty string when there is no output yet.
     */
    toolOutputSummary(tool) {
      const out = tool.output
      if (!out) return ''
      if (typeof out === 'object' && out.count != null) {
        const names = (out.results || [])
          .map((result) => result.tags?.name || result.name)
          .filter(Boolean)
          .slice(0, 3)
        const total = out.results?.length || out.count
        const suffix = names.length
          ? ' — ' + names.join(', ') + (total > names.length ? '…' : '')
          : ''
        return `${out.count} results${suffix}`
      }
      return JSON.stringify(out).slice(0, 300)
    },
    /**
     * True when the executor found nothing ("No results found." with a
     * zero count) — rendered as just "(0 results)", no redundant text.
     */
    isEmptyResult(question) {
      return question.result_count === 0
        && /^no results found\.?/i.test((question.answer || '').trim())
    },
    /**
     * Split an answer into plain/white and value/dark-blue segments.
     * Values: entity names from the result digest (the "− Name (dist)"
     * lines) plus distance/amount tokens ("53.02 km", "(851 m)"). Pure —
     * safe to call from the template.
     */
    answerSegments(question) {
      const answer = question.answer || ''
      if (!answer) return []
      const matches = []

      // Entity names from the digest, longest first so "San Pedro" wins
      // over a shorter overlapping token.
      const names = (question.digest || '')
        .split('\n')
        .map((line) => line.match(/^-\s+(.+?)\s+\(\d/))
        .filter(Boolean)
        .map((match) => match[1].trim())
        .filter(Boolean)
        .sort((a, b) => b.length - a.length)
      for (const name of names) {
        const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
        const re = new RegExp(`\\b${escaped}\\b`, 'gi')
        let hit
        while ((hit = re.exec(answer)) !== null) {
          matches.push({ start: hit.index, end: hit.index + hit[0].length })
          if (hit.index === re.lastIndex) re.lastIndex++ // guard zero-width
        }
      }

      // Distance/amount tokens: "53.02 km", "0.85 km", "(851 m)".
      const unitRe = /\b\d+(?:\.\d+)?\s*(?:km|m|meters?|kilometers?)\b/gi
      let unitHit
      while ((unitHit = unitRe.exec(answer)) !== null) {
        matches.push({ start: unitHit.index, end: unitHit.index + unitHit[0].length })
      }

      // Merge overlaps, sort by position, then emit alternating segments.
      matches.sort((a, b) => a.start - b.start || a.end - b.end)
      const merged = []
      for (const match of matches) {
        const last = merged[merged.length - 1]
        if (last && match.start <= last.end) {
          last.end = Math.max(last.end, match.end)
        } else {
          merged.push({ start: match.start, end: match.end })
        }
      }
      const segments = []
      let cursor = 0
      for (const match of merged) {
        if (match.start > cursor) {
          segments.push({ text: answer.slice(cursor, match.start), value: false })
        }
        segments.push({ text: answer.slice(match.start, match.end), value: true })
        cursor = match.end
      }
      if (cursor < answer.length) {
        segments.push({ text: answer.slice(cursor), value: false })
      }
      return segments
    },
    closeStream() {
      if (this.eventSource) {
        this.eventSource.close()
        this.eventSource = null
      }
    },
  },
  beforeUnmount() {
    clearTimeout(this.exampleCopyTimer)
    this.closeStream()
    // The seed state is surface-scoped: leaving the panel clears it so
    // the next interview starts clean (the store is Pinia-global).
    this.seed.reset()
  },
}
</script>

<style scoped>
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

.research-questions-flow {
  border-left: 2px solid var(--bs-secondary-border-subtle);
  padding-left: 0.75rem;
  /* Question + answer plain text in white; values (entity names,
     distances) get the dark blue via .answer-value. Errors stay red. */
  color: #fff;
}
.research-details[open] .research-questions-flow {
  border-left-color: var(--bs-info-border-subtle);
}
/* The numbered index badge keeps the dark blue (Bootstrap's .badge
   sets its own text color, so override it here). */
.research-questions-flow .badge.bg-secondary {
  color: var(--bs-info-text-emphasis);
}
.answer-value {
  color: var(--bs-info-text-emphasis);
  font-weight: 500;
}
</style>
