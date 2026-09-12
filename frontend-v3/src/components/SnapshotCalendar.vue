<template>
  <div class="d-flex flex-column gap-1">
    <!-- Date picker -->
    <div class="d-flex align-items-center justify-content-between">
      <label class="form-label small text-secondary mb-0">Snapshot Date</label>
      <button
        v-if="canReset"
        class="btn btn-link btn-sm text-danger p-0"
        :disabled="disabled"
        @click="$emit('reset')"
      >
        Reset
      </button>
    </div>

    <!-- Loading state -->
    <div v-if="loading" class="small text-secondary">
      <span class="placeholder-glow">
        <span class="placeholder col-7"></span>
      </span>
    </div>

    <!-- Error state -->
    <div v-else-if="error" class="alert alert-danger small py-1 px-2 mb-0">
      {{ error }}
      <button class="btn btn-link btn-sm p-0 ms-1" @click="$emit('retry')">Retry</button>
    </div>

    <!-- Empty state -->
    <div v-else-if="!hasJobs" class="small text-secondary">
      No processed jobs yet. Run the pipeline to see results here.
    </div>

    <!-- Date picker + job buttons -->
    <template v-else>
      <input
        type="date"
        class="form-control form-control-sm"
        :value="selectedDateInput"
        :min="minDate"
        :max="maxDate"
        :disabled="disabled"
        @input="onDateInput($event)"
      />

      <!-- Pipeline run button (only for new dates) -->
      <button
        v-if="showRunBtn"
        class="pipeline__run-btn w-100 d-flex align-items-center justify-content-center gap-2"
        :disabled="runBtnDisabled"
        @click="$emit('run')"
      >
        <span v-if="runBtnLoading" class="spinner-border spinner-border-sm" role="status" aria-hidden="true"></span>
        <span>{{ runBtnLabel }}</span>
      </button>

      <!-- Processed-jobs collapsible list -->
      <details v-if="jobButtons.length > 0" class="processed-jobs">
        <summary class="processed-jobs__summary small text-secondary fw-semibold d-flex align-items-center gap-1">
          <i class="bi bi-chevron-right processed-jobs__chevron"></i>
          Processed Jobs ({{ jobButtons.length }})
        </summary>
        <div class="d-flex flex-column gap-1 mt-1">
          <div
            v-for="job in jobButtons"
            :key="job.date"
            class="processed-jobs__item d-flex align-items-center justify-content-between gap-2"
            :class="{ 'processed-jobs__item--active': job.isActive }"
            @click="$emit('select-date', job.date)"
          >
            <span class="d-flex align-items-center gap-1">
              <span class="font-monospace small">{{ job.date }}</span>
              <span
                class="badge rounded-pill ms-1"
                :class="jobBadgeClass(job.status)"
              >{{ jobStatusLabel(job.status) }}</span>
            </span>
            <button
              v-if="job.status === 'completed' || job.status === 'failed'"
              type="button"
              class="btn btn-link btn-sm p-0 text-secondary rerun-icon-btn"
              title="Re-run this snapshot"
              @click.stop="$emit('rerun', job.date)"
            >
              <i class="bi bi-arrow-repeat"></i>
            </button>
          </div>
        </div>
      </details>
    </template>
  </div>
</template>

<script>
/**
 * SnapshotCalendar
 *
 * Layer 2 unit component: replaces the year-button selector with a date
 * picker and a processed-jobs button list. Props-first, no store access
 * (rule 01-frontend-patterns, section 1.4b).
 *
 * Props:
 *   snapshotDates   - Available snapshot dates from settings (["2025_12_31", ...]).
 *   completedDates   - Global completed dates from /system/status/.
 *   usedDates        - Set of full dates with completed/running jobs (store-derived).
 *   selectedDate     - Currently selected date ("YYYY_MM_DD").
 *   disabled         - True while a pipeline is running.
 *   jobs             - Raw SnapshotJob rows from the store.
 *   loading          - True while parent is fetching job data.
 *   error            - Error message string, or null.
 *   showRunBtn       - Whether the green Run button should show.
 *   runBtnDisabled   - Whether the Run button is disabled.
 *   runBtnLoading    - Whether the Run button shows a spinner.
 *   runBtnLabel      - Label text for the Run button.
 *
 * Emits:
 *   select-date      - User selected a date (date: "YYYY_MM_DD").
 *   reset            - User clicked Reset.
 *   retry            - User clicked Retry after an error.
 *   rerun            - User clicked re-run on a completed/failed job (date: "YYYY_MM_DD").
 *   run              - User clicked the Run button.
 */

export default {
  name: 'SnapshotCalendar',
  components: {},
  props: {
    snapshotDates: {
      type: Array,
      default: () => [],
    },
    completedDates: {
      type: Array,
      default: () => [],
    },
    usedDates: {
      type: Set,
      default: () => new Set(),
    },
    selectedDate: {
      type: String,
      default: null,
    },
    disabled: {
      type: Boolean,
      default: false,
    },
    jobs: {
      type: Array,
      default: () => [],
    },
    loading: {
      type: Boolean,
      default: false,
    },
    error: {
      type: String,
      default: null,
    },
    /** Whether the pipeline run button should show (new date, not completed). */
    showRunBtn: {
      type: Boolean,
      default: false,
    },
    /** Whether the run button is disabled. */
    runBtnDisabled: {
      type: Boolean,
      default: false,
    },
    /** Whether the run button shows a loading spinner. */
    runBtnLoading: {
      type: Boolean,
      default: false,
    },
    /** Label for the run button. */
    runBtnLabel: {
      type: String,
      default: 'Run Pipeline',
    },
  },
  emits: ['select-date', 'reset', 'retry', 'rerun', 'run'],
  computed: {
    /**
     * Union of all available dates: settings range, global completed,
     * and per-country job dates. Sorted descending.
     */
    availableDates() {
      const all = new Set([
        ...this.snapshotDates,
        ...this.completedDates,
        ...this.jobs.map((j) => j.snapshot_date),
      ])
      return Array.from(all).sort((a, b) => b.localeCompare(a))
    },
    /**
     * Per-country job buttons (completed or failed only), sorted newest-first.
     * Config-driven: data only, no handlers. Clicks emit up.
     */
    jobButtons() {
      return this.jobs
        .filter((j) => j.status === 'completed' || j.status === 'failed' || j.status === 'running')
        .toSorted((a, b) => b.snapshot_date.localeCompare(a.snapshot_date))
        .map((j) => ({
          date: j.snapshot_date,
          status: j.status,
          entities: j.total_entities || 0,
          aligned: j.total_aligned || 0,
          isActive: j.snapshot_date === this.selectedDate,
        }))
    },
    hasJobs() {
      return this.jobButtons.length > 0 || this.availableDates.length > 0
    },
    canReset() {
      return this.usedDates.size > 0 && !this.disabled
    },
    /**
     * Convert "YYYY_MM_DD" to "YYYY-MM-DD" for <input type="date">.
     */
    selectedDateInput() {
      if (!this.selectedDate) return ''
      return this.selectedDate.replace(/_/g, '-')
    },
    minDate() {
      if (this.availableDates.length === 0) return ''
      return this.availableDates[this.availableDates.length - 1].replace(/_/g, '-')
    },
    maxDate() {
      if (this.availableDates.length === 0) return ''
      return this.availableDates[0].replace(/_/g, '-')
    },
  },
  methods: {
    jobBadgeClass(status) {
      if (status === 'completed') return 'text-bg-success'
      if (status === 'failed') return 'text-bg-danger'
      if (status === 'running') return 'text-bg-info'
      return 'text-bg-secondary'
    },
    jobStatusLabel(status) {
      if (status === 'completed') return 'Complete'
      if (status === 'failed') return 'Failed'
      if (status === 'running') return 'Running'
      return status
    },
    /**
     * Convert the date input value back to "YYYY_MM_DD" and emit.
     */
    onDateInput(event) {
      const value = event.target.value
      if (!value) return
      const normalized = value.replace(/-/g, '_')
      this.$emit('select-date', normalized)
    },
  },
}
</script>

<style scoped>
/* Bootstrap covers the bulk; scoped CSS only for the placeholder glow. */
.placeholder-glow .placeholder {
  display: inline-block;
  width: 100%;
  height: 1.5rem;
  border-radius: 0.25rem;
  background: currentColor;
  opacity: 0.3;
  animation: placeholder-glow 1.5s ease-in-out infinite;
}

@keyframes placeholder-glow {
  0%, 100% { opacity: 0.3; }
  50% { opacity: 0.5; }
}

/* Inline re-run icon inside list items. */
.rerun-icon-btn {
  line-height: 1;
  font-size: 0.85rem;
  text-decoration: none;
}

.rerun-icon-btn:hover {
  color: var(--bs-primary) !important;
}

/* Collapsible processed-jobs list (native <details>). */
.processed-jobs__summary {
  list-style: none;
  cursor: pointer;
  user-select: none;
}
.processed-jobs__summary::-webkit-details-marker {
  display: none;
}
.processed-jobs__chevron {
  font-size: 0.65rem;
  transition: transform 0.15s;
}
.processed-jobs[open] .processed-jobs__chevron {
  transform: rotate(90deg);
}

.processed-jobs__item {
  padding: 0.25rem 0.4rem;
  border-radius: 0.3rem;
  cursor: pointer;
  transition: background 0.1s;
}
.processed-jobs__item:hover {
  background: rgba(255, 255, 255, 0.05);
}
.processed-jobs__item--active {
  background: rgba(var(--bs-primary-rgb), 0.15);
}

/* Pipeline run button — green theme bg, white text. Only shown for
   new (not-yet-completed) snapshot dates. */
.pipeline__run-btn {
  padding: 0.3rem 0.6rem;
  border-radius: 0.5rem;
  border: 1px solid var(--bs-success, #22c55e);
  background: var(--bs-success, #22c55e);
  color: #fff;
  font-size: 0.82rem;
  font-weight: 500;
  cursor: pointer;
  white-space: nowrap;
  transition: background 0.15s, border-color 0.15s;
}

.pipeline__run-btn:hover:not(:disabled) {
  background: var(--bs-success-text-emphasis, #4ade80);
  border-color: var(--bs-success-text-emphasis, #4ade80);
}

.pipeline__run-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
</style>
