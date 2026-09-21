<template>
  <div class="metrics-panel">
    <!-- Summary -->
    <div class="metrics-summary">
      <div class="metrics-summary__item">
        <span class="metrics-summary__label">Status</span>
        <span
          class="metrics-summary__value"
          :class="`metrics-summary__value--${status}`"
        >{{ status }}</span>
      </div>
      <div class="metrics-summary__item">
        <span class="metrics-summary__label">Steps done</span>
        <span class="metrics-summary__value">{{ completedSteps.length }}/{{ steps.length }}</span>
      </div>
      <div class="metrics-summary__item">
        <span class="metrics-summary__label">Duration</span>
        <span class="metrics-summary__value">{{ totalDuration || '\u2014' }}</span>
      </div>
    </div>

    <!-- Step timeline -->
    <div v-if="steps.length > 0" class="metric-steps">
      <div
        v-for="step in steps"
        :key="step.name"
        class="metric-step"
        :class="statusClass(step.status)"
      >
        <span class="metric-step__icon">{{ statusIcon(step.status) }}</span>
        <div class="metric-step__body">
          <div class="metric-step__header">
            <span class="metric-step__name">{{ step.label }}</span>
            <span v-if="step.status === 'completed'" class="metric-step__duration">
              {{ formatDuration(step.duration_ms) }}
            </span>
          </div>
          <p v-if="step.message" class="metric-step__message">{{ step.message }}</p>
        </div>
      </div>
    </div>

    <!-- Empty state -->
    <div v-else class="metrics-empty">
      <p v-if="status === 'idle'">No pipeline has been run yet.</p>
      <p v-else-if="status === 'running'">Pipeline is starting\u2026</p>
      <p v-else>No step data available.</p>
    </div>

    <!-- Error -->
    <p v-if="errorMessage" class="metrics-error">{{ errorMessage }}</p>
  </div>
</template>

<script>
/**
 * PipelineMetricsPanel
 *
 * Per-step performance timeline for the latest pipeline run.
 * Reads directly from pipelineStore — reactive updates via WebSocket.
 *
 * Props:
 *   countryName  - Required. Country to show metrics for.
 *   snapshotDate - Optional. Snapshot date (e.g. "2025_12_31"). The metrics
 *                  panel reads from the pipelineStore run state, which is
 *                  already scoped to the selected snapshot date by
 *                  fetchSnapshotJobResults() in the store.
 */

import { usePipelineStore } from '../stores/pipelineStore'

export default {
  name: 'PipelineMetricsPanel',
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
  setup() {
    const store = usePipelineStore()
    return { store }
  },
  computed: {
    run() {
      return this.store.runs[this.countryName] || null
    },
    steps() {
      return this.run?.steps || []
    },
    status() {
      return this.run?.status || 'idle'
    },
    errorMessage() {
      return this.run?.error || null
    },
    completedSteps() {
      return this.steps.filter((s) => s.status === 'completed')
    },
    totalDuration() {
      if (!this.run?.startedAt) return null
      const end = this.run?.completedAt || new Date().toISOString()
      const ms = new Date(end) - new Date(this.run.startedAt)
      return this.formatDuration(ms)
    },
  },
  methods: {
    formatDuration(ms) {
      if (!ms || ms < 0) return '\u2014'
      const seconds = Math.floor(ms / 1000)
      if (seconds < 60) return `${seconds}s`
      const minutes = Math.floor(seconds / 60)
      const secs = seconds % 60
      if (minutes < 60) return `${minutes}m ${secs}s`
      const hours = Math.floor(minutes / 60)
      const mins = minutes % 60
      return `${hours}h ${mins}m ${secs}s`
    },
    statusIcon(status) {
      if (status === 'completed') return '\u2713'
      if (status === 'failed') return '\u2717'
      if (status === 'skipped') return '\u2014'
      if (status === 'in_progress') return '\u25B6'
      return '\u25CB'
    },
    statusClass(status) {
      if (status === 'completed') return 'metric-step--done'
      if (status === 'failed') return 'metric-step--fail'
      if (status === 'skipped') return 'metric-step--skip'
      if (status === 'in_progress') return 'metric-step--active'
      return ''
    },
  },
}
</script>

<style scoped>
.metrics-panel {
  display: flex;
  flex-direction: column;
  gap: 0.6rem;
}

/* ── Summary cards ── */

.metrics-summary {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 0.35rem;
}

.metrics-summary__item {
  padding: 0.4rem 0.5rem;
  border-radius: 0.4rem;
  background: rgba(15, 23, 42, 0.5);
  border: 1px solid var(--bs-border-color);
  display: flex;
  flex-direction: column;
  gap: 0.15rem;
}

.metrics-summary__label {
  /* Micro-header: 10px uppercase with tracking. */
  font-size: 0.65rem;
  color: var(--bs-meta-color);
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.metrics-summary__value {
  font-size: 0.9rem;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}

.metrics-summary__value--running {
  color: var(--bs-info-text-emphasis);
}

.metrics-summary__value--completed {
  color: var(--bs-success);
}

.metrics-summary__value--failed {
  color: var(--bs-danger);
}

/* ── Step timeline ── */

.metric-steps {
  display: flex;
  flex-direction: column;
  gap: 0.15rem;
}

.metric-step {
  display: flex;
  align-items: flex-start;
  gap: 0.35rem;
  padding: 0.3rem 0.4rem;
  border-radius: 0.35rem;
  border-left: 2px solid var(--bs-border-color);
  transition: background 0.15s;
}

.metric-step--done {
  border-left-color: var(--bs-success);
}

.metric-step--active {
  border-left-color: var(--bs-info-text-emphasis);
  background: rgba(6, 182, 212, 0.05);
}

.metric-step--fail {
  border-left-color: var(--bs-danger);
  background: rgba(239, 68, 68, 0.05);
}

.metric-step--skip {
  border-left-color: var(--bs-meta-color);
  opacity: 0.6;
}

.metric-step__icon {
  font-size: 0.7rem;
  width: 1rem;
  text-align: center;
  margin-top: 0.1rem;
  flex-shrink: 0;
}

.metric-step--done .metric-step__icon { color: var(--bs-success); }
.metric-step--active .metric-step__icon { color: var(--bs-info-text-emphasis); }
.metric-step--fail .metric-step__icon { color: var(--bs-danger); }

.metric-step__body {
  flex: 1;
  min-width: 0;
}

.metric-step__header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 0.5rem;
}

.metric-step__name {
  font-size: 0.78rem;
  font-weight: 500;
}

.metric-step__duration {
  /* Data + identifiers: mono at 11px with tabular figures. */
  font-family: var(--bs-font-monospace, ui-monospace, SFMono-Regular, Menlo, Consolas, monospace);
  font-size: 0.7rem;
  font-variant-numeric: tabular-nums;
  color: var(--bs-meta-color);
  flex-shrink: 0;
}

.metric-step__message {
  margin: 0.05rem 0 0;
  font-size: 0.68rem;
  color: var(--bs-meta-color);
}

/* ── Empty / error ── */

.metrics-empty {
  padding: 0.5rem;
  text-align: center;
  color: var(--bs-meta-color);
  font-size: 0.8rem;
}

.metrics-error {
  margin: 0;
  padding: 0.3rem 0.4rem;
  border-radius: 0.35rem;
  background: var(--bs-danger-bg-subtle);
  color: var(--bs-danger-text);
  font-size: 0.75rem;
}
</style>
