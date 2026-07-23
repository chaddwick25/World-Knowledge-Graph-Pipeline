<script>
/**
 * PipelineProgressPanelV3
 *
 * Displays pipeline progress for a single country.
 * Supports both WorldKG (15 steps) and Temporal preprocessing (4 phases).
 * Reads from the Pinia pipelineStore.
 *
 * Props:
 *   countryName  - Required. Country to track.
 *   pipelineType - 'worldkg' (default) or 'temporal'
 *
 * Events:
 *   pipeline-done  - { status, sessionId, error }
 */

import { computed, watch, onMounted } from 'vue'
import { usePipelineStore } from '../stores/pipelineStore'

export default {
  name: 'PipelineProgressPanelV3',
  props: {
    countryName: {
      type: String,
      required: true,
    },
    pipelineType: {
      type: String,
      default: 'worldkg', // 'worldkg' | 'temporal'
    },
    /**
     * Optional session ID to connect WebSocket immediately
     * (used when the pipeline was already started externally).
     */
    sessionId: {
      type: String,
      default: null,
    },
  },
  emits: ['pipeline-done'],
  setup(props, { emit }) {
    const store = usePipelineStore()

    // ── Reactive run state ──
    const run = computed(() => store.runs[props.countryName] || null)
    const status = computed(() => run.value?.status || 'idle')
    const steps = computed(() => run.value?.steps || [])
    const completedCount = computed(() => store.completedCount(run.value))
    const totalCount = computed(() => steps.value.length)
    const progressPct = computed(() => store.progressPct(run.value))
    const errorMessage = computed(() => run.value?.error || null)
    const logs = computed(() => run.value?.logs || [])

    const isRunning = computed(() => status.value === 'running' || status.value === 'in_progress')
    const isComplete = computed(() => status.value === 'completed')
    const isFailed = computed(() => status.value === 'failed')
    const isSkipped = computed(() => status.value === 'skipped')
    const isIdle = computed(() => status.value === 'idle')

    // ── Derived labels ──
    const badgeLabel = computed(() => {
      if (isRunning.value) return 'Running'
      if (isComplete.value) return 'Complete'
      if (isFailed.value) return 'Failed'
      if (isSkipped.value) return 'Skipped'
      return 'Idle'
    })

    const badgeVariant = computed(() => {
      if (isRunning.value) return 'info'
      if (isComplete.value) return 'success'
      if (isFailed.value) return 'danger'
      if (isSkipped.value) return 'warning'
      return 'secondary'
    })

    const currentStepName = computed(() => {
      const active = steps.value.find((s) => s.status === 'in_progress')
      return active ? active.name : null
    })

    const pipelineTitle = computed(() => {
      if (props.pipelineType === 'temporal') return 'Preprocessing'
      return 'WorldKG Pipeline'
    })

    // ── Watch for status changes → emit ──
    watch(status, (newStatus) => {
      if (['completed', 'failed', 'skipped'].includes(newStatus)) {
        emit('pipeline-done', {
          status: newStatus,
          sessionId: run.value?.sessionId || null,
          error: errorMessage.value,
        })
      }
    })

    // ── On mount: fetch existing state / connect WS ──
    onMounted(async () => {
      // Only fetch if no run exists yet (or we need to restore)
      const existing = store.runs[props.countryName]
      if (!existing || existing.status === 'idle') {
        await store.fetchRunState(props.countryName)
      }

      // Connect WebSocket if we have a session ID
      const currentRun = store.runs[props.countryName]
      if (props.sessionId) {
        store.connectWebSocket(props.sessionId, props.countryName)
        if (currentRun) currentRun.sessionId = props.sessionId
      } else if (currentRun?.sessionId && isRunning.value) {
        store.connectWebSocket(currentRun.sessionId, props.countryName)
      }
    })

    // ── Actions ──
    async function handleRun() {
      if (props.pipelineType === 'temporal') {
        await store.startPreprocessing(props.countryName)
      } else {
        await store.startPipeline(props.countryName)
      }
    }

    function handleRetry() {
      handleRun()
    }

    // ── Step icon helper ──
    function iconClass(stepStatus) {
      if (stepStatus === 'completed') return 'step-icon step-icon--success'
      if (stepStatus === 'in_progress') return 'step-icon step-icon--running'
      if (stepStatus === 'failed') return 'step-icon step-icon--failed'
      if (stepStatus === 'skipped') return 'step-icon step-icon--skipped'
      return 'step-icon step-icon--pending'
    }

    function stepLabelClass(step) {
      if (step.status === 'in_progress') return 'step-label step-label--active'
      if (step.status === 'completed') return 'step-label step-label--done'
      return 'step-label'
    }

    return {
      run,
      steps,
      status,
      completedCount,
      totalCount,
      progressPct,
      errorMessage,
      logs,
      isRunning,
      isComplete,
      isFailed,
      isSkipped,
      isIdle,
      badgeLabel,
      badgeVariant,
      currentStepName,
      pipelineTitle,
      handleRun,
      handleRetry,
      iconClass,
      stepLabelClass,
    }
  },
}
</script>

<template>
  <section
    v-if="run || status !== 'idle' || logs.length > 0"
    class="pipeline"
    :class="{ 'pipeline--running': isRunning, 'pipeline--failed': isFailed }"
  >
    <!-- ── Header ─────────────────────────────────────────── -->
    <header class="pipeline__header">
      <div class="pipeline__header-left">
        <span class="pipeline__title">{{ pipelineTitle }} — {{ countryName }}</span>
        <span v-if="isRunning" class="pipeline__step-count">
          Step {{ completedCount }}/{{ totalCount }}
        </span>
      </div>
      <div class="pipeline__header-right">
        <span
          class="pipeline__badge"
          :class="`pipeline__badge--${badgeVariant}`"
        >{{ badgeLabel }}</span>

        <!-- Action button: Run / Re-run / Retry -->
        <button
          v-if="isIdle || isComplete || isSkipped"
          class="pipeline__action-btn"
          @click="handleRun"
        >
          <template v-if="isIdle">
            {{ pipelineType === 'temporal' ? 'Start Preprocessing' : 'Run Pipeline' }}
          </template>
          <template v-else>Re-run Pipeline</template>
        </button>

        <button
          v-if="isFailed"
          class="pipeline__action-btn pipeline__action-btn--danger"
          @click="handleRetry"
        >
          Retry
        </button>
      </div>
    </header>

    <!-- ── Progress bar (only when running) ──────────────── -->
    <div v-if="isRunning" class="pipeline__progress">
      <div class="pipeline__progress-bar" :style="{ width: progressPct + '%' }"></div>
    </div>

    <!-- ── Steps list ───────────────────────────────────── -->
    <details
      v-if="steps.length > 0"
      class="pipeline__steps-details"
      :open="isRunning || isFailed"
    >
      <summary class="pipeline__steps-summary">
        Steps ({{ completedCount }}/{{ totalCount }})
        <span v-if="currentStepName && isRunning" class="pipeline__current-hint">
          — {{ currentStepName.replace(/_/g, ' ') }}
        </span>
      </summary>

      <ul class="pipeline__steps">
        <li
          v-for="step in steps"
          :key="step.name"
          class="pipeline__step"
          :class="{ 'pipeline__step--active': step.status === 'in_progress' }"
        >
          <span :class="iconClass(step.status)"></span>

          <div class="pipeline__step-body">
            <div class="pipeline__step-header">
              <span :class="stepLabelClass(step)">{{ step.label }}</span>
              <span class="pipeline__step-status-icon">
                <template v-if="step.status === 'completed'">&#10003;</template>
                <template v-else-if="step.status === 'failed'">&#10007;</template>
                <template v-else-if="step.status === 'skipped'">&mdash;</template>
                <template v-else-if="step.status === 'in_progress'">
                  <span class="pipeline__spinner"></span>
                </template>
              </span>
            </div>
            <p v-if="step.message" class="pipeline__step-message">{{ step.message }}</p>
          </div>
        </li>
      </ul>
    </details>

    <!-- ── Log panel ────────────────────────────────────── -->
    <details v-if="logs.length > 0" class="pipeline__log-details">
      <summary class="pipeline__log-summary">
        Logs ({{ logs.length }})
      </summary>
      <div class="pipeline__log-container">
        <div
          v-for="(log, i) in logs"
          :key="i"
          class="pipeline__log-line"
        >
          <span class="pipeline__log-time">{{ log.time }}</span>
          <span class="pipeline__log-text">{{ log.message }}</span>
        </div>
      </div>
    </details>

    <!-- ── Error / skip message ─────────────────────────── -->
    <p
      v-if="errorMessage && (isFailed || isSkipped)"
      class="pipeline__error"
    >{{ errorMessage }}</p>
  </section>

  <!-- ── Empty state: idle with run button ───────────────── -->
  <section
    v-else-if="countryName"
    class="pipeline pipeline--idle"
  >
    <header class="pipeline__header">
      <div class="pipeline__header-left">
        <span class="pipeline__title">{{ pipelineTitle }} — {{ countryName }}</span>
      </div>
      <div class="pipeline__header-right">
        <button class="pipeline__action-btn" @click="handleRun">
          {{ pipelineType === 'temporal' ? 'Start Preprocessing' : 'Run Pipeline' }}
        </button>
      </div>
    </header>
  </section>
</template>

<style scoped>
.pipeline {
  margin-top: 0.75rem;
  padding: 0.6rem 0.75rem;
  border-radius: 0.9rem;
  background: #020617;
  border: 1px solid #111827;
  font-size: 0.82rem;
}

.pipeline--running {
  border-color: #1e3a5f;
}

.pipeline--failed {
  border-color: #450a0a;
}

/* ── Header ─────────────────────────────────── */

.pipeline__header {
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  align-items: center;
  gap: 0.5rem;
}

.pipeline__header-left {
  display: flex;
  align-items: baseline;
  gap: 0.5rem;
  min-width: 0;
}

.pipeline__title {
  font-weight: 600;
  white-space: nowrap;
}

.pipeline__step-count {
  font-size: 0.75rem;
  color: #9ca3af;
}

.pipeline__header-right {
  display: flex;
  align-items: center;
  gap: 0.35rem;
  flex-shrink: 0;
}

.pipeline__badge {
  padding: 0.1rem 0.5rem;
  border-radius: 999px;
  font-size: 0.72rem;
  border: 1px solid transparent;
  white-space: nowrap;
}

.pipeline__badge--success {
  background: rgba(34, 197, 94, 0.15);
  border-color: #22c55e;
  color: #22c55e;
}

.pipeline__badge--danger {
  background: rgba(239, 68, 68, 0.15);
  border-color: #ef4444;
  color: #ef4444;
}

.pipeline__badge--info {
  background: rgba(59, 130, 246, 0.15);
  border-color: #60a5fa;
  color: #60a5fa;
}

.pipeline__badge--secondary {
  background: rgba(75, 85, 99, 0.2);
  border-color: #4b5563;
  color: #9ca3af;
}

.pipeline__badge--warning {
  background: rgba(234, 179, 8, 0.15);
  border-color: #eab308;
  color: #eab308;
}

.pipeline__action-btn {
  padding: 0.25rem 0.6rem;
  border-radius: 0.5rem;
  border: 1px solid #4f46e5;
  background: #111827;
  color: #e5e7eb;
  font-size: 0.78rem;
  cursor: pointer;
  white-space: nowrap;
  transition: background 0.15s, border-color 0.15s;
}

.pipeline__action-btn:hover {
  background: #1f2937;
  border-color: #6366f1;
}

.pipeline__action-btn--danger {
  border-color: #dc2626;
}

.pipeline__action-btn--danger:hover {
  background: rgba(220, 38, 38, 0.15);
}

/* ── Progress bar ────────────────────────────── */

.pipeline__progress {
  margin-top: 0.4rem;
  height: 4px;
  background: #1f2937;
  border-radius: 999px;
  overflow: hidden;
}

.pipeline__progress-bar {
  height: 100%;
  background: linear-gradient(90deg, #3b82f6, #60a5fa);
  border-radius: 999px;
  transition: width 0.5s ease;
}

/* ── Steps list ──────────────────────────────── */

.pipeline__steps-details {
  margin-top: 0.4rem;
}

.pipeline__steps-summary {
  font-size: 0.75rem;
  color: #9ca3af;
  cursor: pointer;
  user-select: none;
  padding: 0.15rem 0;
}

.pipeline__steps-summary::-webkit-details-marker {
  color: #4b5563;
}

.pipeline__current-hint {
  color: #60a5fa;
  font-weight: 500;
}

.pipeline__steps {
  list-style: none;
  margin: 0.25rem 0 0;
  padding: 0;
}

.pipeline__step {
  display: flex;
  align-items: flex-start;
  gap: 0.4rem;
  padding: 0.2rem 0;
  border-radius: 0.3rem;
  transition: background 0.2s;
}

.pipeline__step--active {
  background: rgba(59, 130, 246, 0.06);
}

.step-icon {
  width: 8px;
  height: 8px;
  border-radius: 999px;
  margin-top: 0.35rem;
  flex-shrink: 0;
}

.step-icon--pending {
  background: #374151;
}

.step-icon--running {
  background: #38bdf8;
  box-shadow: 0 0 4px rgba(56, 189, 248, 0.6);
  animation: pulse-glow 1.5s infinite;
}

@keyframes pulse-glow {
  0%, 100% { box-shadow: 0 0 4px rgba(56, 189, 248, 0.4); }
  50% { box-shadow: 0 0 8px rgba(56, 189, 248, 0.8); }
}

.step-icon--success {
  background: #22c55e;
}

.step-icon--failed {
  background: #ef4444;
}

.step-icon--skipped {
  background: #6b7280;
}

.pipeline__step-body {
  flex: 1;
  min-width: 0;
}

.pipeline__step-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 0.5rem;
}

.step-label {
  font-weight: 500;
  font-size: 0.8rem;
}

.step-label--active {
  color: #60a5fa;
}

.step-label--done {
  color: #22c55e;
}

.pipeline__step-status-icon {
  font-size: 0.75rem;
  flex-shrink: 0;
  width: 1rem;
  text-align: center;
}

.pipeline__spinner {
  display: inline-block;
  width: 10px;
  height: 10px;
  border: 2px solid #60a5fa;
  border-top-color: transparent;
  border-radius: 999px;
  animation: spin 0.8s linear infinite;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

.pipeline__step-message {
  margin: 0.05rem 0 0;
  font-size: 0.7rem;
  color: #6b7280;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* ── Log panel ───────────────────────────────── */

.pipeline__log-details {
  margin-top: 0.3rem;
}

.pipeline__log-summary {
  font-size: 0.72rem;
  color: #9ca3af;
  cursor: pointer;
  user-select: none;
  padding: 0.15rem 0;
}

.pipeline__log-summary::-webkit-details-marker {
  color: #4b5563;
}

.pipeline__log-container {
  margin-top: 0.2rem;
  max-height: 160px;
  overflow-y: auto;
  padding: 0.25rem 0.4rem;
  background: rgba(0, 0, 0, 0.3);
  border-radius: 0.4rem;
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, 'Liberation Mono',
    'Courier New', monospace;
  font-size: 0.68rem;
}

.pipeline__log-line {
  padding: 0.1rem 0;
  display: flex;
  gap: 0.4rem;
}

.pipeline__log-time {
  color: #525252;
  flex-shrink: 0;
}

.pipeline__log-text {
  color: #a3a3a3;
  word-break: break-word;
}

/* ── Error ────────────────────────────────────── */

.pipeline__error {
  margin-top: 0.35rem;
  font-size: 0.78rem;
  color: #fecaca;
  padding: 0.3rem 0.4rem;
  background: rgba(239, 68, 68, 0.08);
  border-radius: 0.4rem;
}

/* ── Idle state ───────────────────────────────── */

.pipeline--idle {
  border-color: #1f2937;
  opacity: 0.85;
}
</style>
