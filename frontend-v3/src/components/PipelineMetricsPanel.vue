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

import { computed } from 'vue'
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
  setup(props) {
    const store = usePipelineStore()

    const run = computed(() => store.runs[props.countryName] || null)
    const steps = computed(() => run.value?.steps || [])
    const status = computed(() => run.value?.status || 'idle')
    const errorMessage = computed(() => run.value?.error || null)

    const completedSteps = computed(() =>
      steps.value.filter((s) => s.status === 'completed')
    )

    const totalDuration = computed(() => {
      if (!run.value?.startedAt) return null
      const end = run.value?.completedAt || new Date().toISOString()
      const ms = new Date(end) - new Date(run.value.startedAt)
      return formatDuration(ms)
    })

    function formatDuration(ms) {
      if (!ms || ms < 0) return '\u2014'
      const seconds = Math.floor(ms / 1000)
      if (seconds < 60) return `${seconds}s`
      const minutes = Math.floor(seconds / 60)
      const secs = seconds % 60
      if (minutes < 60) return `${minutes}m ${secs}s`
      const hours = Math.floor(minutes / 60)
      const mins = minutes % 60
      return `${hours}h ${mins}m ${secs}s`
    }

    function statusIcon(status) {
      if (status === 'completed') return '\u2713'
      if (status === 'failed') return '\u2717'
      if (status === 'skipped') return '\u2014'
      if (status === 'in_progress') return '\u25B6'
      return '\u25CB'
    }

    function statusClass(status) {
      if (status === 'completed') return 'metric-step--done'
      if (status === 'failed') return 'metric-step--fail'
      if (status === 'skipped') return 'metric-step--skip'
      if (status === 'in_progress') return 'metric-step--active'
      return ''
    }

    return {
      steps,
      status,
      errorMessage,
      completedSteps,
      totalDuration,
      formatDuration,
      statusIcon,
      statusClass,
      snapshotDate: props.snapshotDate,
    }
  },
}
</script>

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
  border: 1px solid #1f2937;
  display: flex;
  flex-direction: column;
  gap: 0.15rem;
}

.metrics-summary__label {
  font-size: 0.65rem;
  color: #6b7280;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.metrics-summary__value {
  font-size: 0.9rem;
  font-weight: 600;
}

.metrics-summary__value--running {
  color: #60a5fa;
}

.metrics-summary__value--completed {
  color: #22c55e;
}

.metrics-summary__value--failed {
  color: #ef4444;
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
  border-left: 2px solid #1f2937;
  transition: background 0.15s;
}

.metric-step--done {
  border-left-color: #22c55e;
}

.metric-step--active {
  border-left-color: #60a5fa;
  background: rgba(59, 130, 246, 0.05);
}

.metric-step--fail {
  border-left-color: #ef4444;
  background: rgba(239, 68, 68, 0.05);
}

.metric-step--skip {
  border-left-color: #6b7280;
  opacity: 0.6;
}

.metric-step__icon {
  font-size: 0.7rem;
  width: 1rem;
  text-align: center;
  margin-top: 0.1rem;
  flex-shrink: 0;
}

.metric-step--done .metric-step__icon { color: #22c55e; }
.metric-step--active .metric-step__icon { color: #60a5fa; }
.metric-step--fail .metric-step__icon { color: #ef4444; }

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
  font-size: 0.7rem;
  color: #6b7280;
  flex-shrink: 0;
}

.metric-step__message {
  margin: 0.05rem 0 0;
  font-size: 0.68rem;
  color: #6b7280;
}

/* ── Empty / error ── */

.metrics-empty {
  padding: 0.5rem;
  text-align: center;
  color: #6b7280;
  font-size: 0.8rem;
}

.metrics-error {
  margin: 0;
  padding: 0.3rem 0.4rem;
  border-radius: 0.35rem;
  background: rgba(239, 68, 68, 0.08);
  color: #fecaca;
  font-size: 0.75rem;
}
</style>
