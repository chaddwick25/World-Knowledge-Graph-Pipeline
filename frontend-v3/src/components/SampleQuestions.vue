<template>
  <!-- Sample questions (OSM RAG mode learning aid; the chevron collapses
       the list). Layer-3 leaf: props in, copy state is local presentation. -->
  <div class="d-flex flex-column gap-1 border-top pt-2">
    <button
      type="button"
      class="btn btn-sm btn-link text-decoration-none p-0 text-secondary d-flex align-items-center"
      style="width: fit-content;"
      :aria-expanded="open"
      aria-label="Toggle sample questions"
      @click="open = !open"
    >
      <i :class="open ? 'bi bi-chevron-up' : 'bi bi-chevron-down'"></i>
      <!-- Scope label — only while the questions are showing. -->
      <span v-if="open" class="form-label small text-secondary mb-0 ms-1">
        Sample questions for {{ scope }}
      </span>
    </button>
    <div v-if="open" class="d-flex flex-column gap-1">
      <div
        v-for="(q, idx) in questions"
        :key="`${q.question}-${idx}`"
        class="d-flex align-items-center gap-2 border rounded p-1 px-2"
        style="font-size: 0.72rem;"
      >
        <span class="flex-grow-1 text-truncate" :title="q.question">{{ q.question }}</span>
        <i
          v-if="templateInfo(q.template)"
          class="bi bi-info-circle flex-shrink-0 sq-info-icon"
          :title="templateInfo(q.template)"
        ></i>
        <button
          class="btn btn-sm btn-outline-primary text-nowrap py-0 px-2"
          :class="{ 'btn-success': copiedQuestion === q.question }"
          @click="copyQuestion(q.question)"
        >{{ copiedQuestion === q.question ? 'Copied!' : 'Copy' }}</button>
      </div>
    </div>
  </div>
</template>

<script>
// Layman descriptions per trained template — the stored `template` field
// ('FILTER-AGGREGATE-MEASURE (#1)') maps to the question type it teaches.
const TEMPLATE_INFO = {
  'FILTER-AGGREGATE-MEASURE (#1)': 'What is near a place',
  'OBJECT-FIELD-MEASURE (#2)': 'How far apart two places are',
  'GEOCODE-BATCH-COMPARE (#4)': 'The nearest, or which is closer',
  'LOCATION-BEARING-CLASSIFY (#5)': 'What lies in a direction',
  'PLACE-ATTRIBUTE-QUERY (#8)': 'What is around a place',
}

export default {
  name: 'SampleQuestions',
  props: {
    questions: { type: Array, required: true },
    scope: { type: String, default: '' },
  },
  data() {
    return {
      open: true,
      copiedQuestion: null,
      copyTimer: null,
    }
  },
  unmounted() {
    clearTimeout(this.copyTimer)
  },
  methods: {
    /** Layman one-liner for the question's template type; null for
     *  unknown/empty templates (curated rows predate the field). */
    templateInfo(template) {
      return TEMPLATE_INFO[template] || null
    },
    /** Copy a sample question to the clipboard with a brief confirmation. */
    copyQuestion(question) {
      navigator.clipboard?.writeText(question).catch(() => {})
      this.copiedQuestion = question
      clearTimeout(this.copyTimer)
      this.copyTimer = setTimeout(() => {
        this.copiedQuestion = null
      }, 1500)
    },
  },
}
</script>

<style scoped>
/* Info icon: idle at secondary gray, hovers to the app's info-blue accent
   (same --bs-info used for name links and the active-row marker bar). */
.sq-info-icon {
  font-size: 0.95rem;
  color: var(--bs-secondary);
  cursor: help;
  transition: color 0.15s ease, transform 0.15s ease;
}
.sq-info-icon:hover {
  color: var(--bs-info);
  transform: scale(1.25);
}
</style>
