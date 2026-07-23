<script>
/**
 * PlanetInitTerminal — Live initialization terminal for the map area.
 *
 * Takes its state from the shared usePlanetInit composable.
 * Shows:
 *   - A live log feed (terminal-style)
 *   - A progress bar
 *   - A compact step indicator
 *   - When complete: a "Continue" button to dismiss and reveal the map
 */

import { ref, computed, watch, nextTick, onMounted } from 'vue'
import { usePlanetInit } from '../composables/usePlanetInit'

export default {
  name: 'PlanetInitTerminal',
  emits: ['continue'],
  setup(props, { emit }) {
    const init = usePlanetInit()
    const logContainer = ref(null)

    // Auto-scroll log to bottom when new entries arrive
    watch(
      () => init.logs.value.length,
      async () => {
        await nextTick()
        if (logContainer.value) {
          logContainer.value.scrollTop = logContainer.value.scrollHeight
        }
      }
    )

    // Step icon helper
    function iconClass(stepStatus) {
      if (stepStatus === 'completed') return 'step-icon step-icon--success'
      if (stepStatus === 'in_progress') return 'step-icon step-icon--running'
      if (stepStatus === 'failed') return 'step-icon step-icon--failed'
      if (stepStatus === 'skipped') return 'step-icon step-icon--skipped'
      return 'step-icon step-icon--pending'
    }

    function handleContinue() {
      emit('continue')
    }

    return {
      init,
      logContainer,
      iconClass,
      handleContinue,
    }
  },
}
</script>

<template>
  <!-- TODO: wire(hide/show) this to the project states -->
  <div
    class="init-terminal"
    :class="{
      'init-terminal--running': init.isRunning.value,
      'init-terminal--complete': init.isComplete.value,
      'init-terminal--failed': init.isFailed.value,
    }"
  >
    <!-- ── Header ──────────────────────────────────────── -->
    <div class="init-terminal__header">
      <div class="init-terminal__brand">
        <span class="init-terminal__icon">&#127758;</span>
        <div>
          <span class="init-terminal__title">Planet Initialization</span>
          <span
            v-if="!init.planetInitActive.value"
            class="init-terminal__subtitle"
          >
            Initialize the planet using the panel on the right to enable
            country selection and the WorldKG pipeline.
          </span>
        </div>
      </div>
      <span
        class="init-terminal__badge"
        :class="`init-terminal__badge--${init.badgeVariant.value}`"
      >
        {{ init.badgeLabel.value }}
      </span>
    </div>

    <!-- ── Suggested planet path (idle state) ──────────── -->
    <p
      v-if="init.suggestedPlanetFilePath.value && !init.planetInitActive.value"
      class="init-terminal__path-hint"
    >
      Planet PBF: <code>{{ init.suggestedPlanetFilePath.value }}</code>
    </p>

    <!-- ── Progress section (visible once running) ─────── -->
    <template v-if="init.planetInitActive.value">
      <!-- Progress bar -->
      <div class="init-terminal__progress-row">
        <div class="init-terminal__progress-track">
          <div
            class="init-terminal__progress-fill"
            :style="{ width: init.progressPct.value + '%' }"
          ></div>
        </div>
        <span class="init-terminal__progress-pct">
          {{ init.progressPct.value }}%
        </span>
      </div>

      <!-- Step count -->
      <div class="init-terminal__step-count">
        Step {{ init.completedCount.value }}/{{ init.totalCount.value }}
        <template v-if="init.currentStepName.value">
          <span class="init-terminal__step-name">&mdash; {{ init.currentStepName.value }}</span>
        </template>
      </div>

      <!-- ── Live log (terminal style) ─────────────────── -->
      <div ref="logContainer" class="init-terminal__log">
        <div
          v-for="(log, i) in init.logs.value"
          :key="i"
          class="init-terminal__log-line"
        >
          <span class="init-terminal__log-time">{{ log.time }}</span>
          <span class="init-terminal__log-text">{{ log.message }}</span>
        </div>
        <div v-if="init.isRunning.value" class="init-terminal__log-cursor">
          <span class="init-terminal__cursor-blink">&#9632;</span>
        </div>
      </div>

      <!-- ── Steps panel (collapsible) ─────────────────── -->
      <details class="init-terminal__steps-details">
        <summary class="init-terminal__steps-summary">
          Steps ({{ init.completedCount.value }}/{{ init.totalCount.value }})
          <span v-if="init.isRunning.value && init.currentStepName.value" class="init-terminal__current-hint">
            &mdash; {{ init.currentStepName.value }}
          </span>
        </summary>
        <ul class="init-terminal__steps">
          <li
            v-for="step in init.steps.value"
            :key="step.name"
            class="init-terminal__step"
            :class="{ 'init-terminal__step--active': step.status === 'in_progress' }"
          >
            <span :class="iconClass(step.status)"></span>
            <div class="init-terminal__step-body">
              <div class="init-terminal__step-header">
                <span
                  class="init-terminal__step-label"
                  :class="{
                    'init-terminal__step-label--done': step.status === 'completed',
                    'init-terminal__step-label--active': step.status === 'in_progress',
                  }"
                >{{ step.label }}</span>
                <span class="init-terminal__step-status-icon">
                  <template v-if="step.status === 'completed'">&#10003;</template>
                  <template v-else-if="step.status === 'failed'">&#10007;</template>
                  <template v-else-if="step.status === 'skipped'">&mdash;</template>
                  <template v-else-if="step.status === 'in_progress'">
                    <span class="init-terminal__spinner-sm"></span>
                  </template>
                </span>
              </div>
              <p v-if="step.message" class="init-terminal__step-message">{{ step.message }}</p>
            </div>
          </li>
        </ul>
      </details>

      <!-- ── Error display ─────────────────────────────── -->
      <div
        v-if="init.errorMessage.value && init.isFailed.value"
        class="init-terminal__error"
      >
        {{ init.errorMessage.value }}
      </div>

      <!-- ── Summary + Continue (when complete) ────────── -->
      <div v-if="init.isComplete.value" class="init-terminal__complete">
        <div class="init-terminal__summary-cards">
          <div class="init-terminal__summary-card" v-if="init.summaryData.value">
            <span class="init-terminal__summary-card-label">Planet</span>
            <span class="init-terminal__summary-card-value">
              {{ init.summaryData.value.planet?.size_gb || '?' }} GB
            </span>
          </div>
          <div class="init-terminal__summary-card" v-if="init.summaryData.value">
            <span class="init-terminal__summary-card-label">Countries</span>
            <span class="init-terminal__summary-card-value">
              {{ init.summaryData.value.embeddings?.with_embeddings || 0 }}
              /{{ init.summaryData.value.embeddings?.total_countries || 0 }}
            </span>
          </div>
          <div class="init-terminal__summary-card" v-if="init.summaryData.value">
            <span class="init-terminal__summary-card-label">Cold Storage</span>
            <span class="init-terminal__summary-card-value">
              {{ init.summaryData.value.storage?.cold?.size_gb || 0 }} GB
            </span>
          </div>
          <div class="init-terminal__summary-card" v-if="init.summaryData.value">
            <span class="init-terminal__summary-card-label">Hot Storage</span>
            <span class="init-terminal__summary-card-value">
              {{ init.summaryData.value.storage?.hot?.size_gb || 0 }} GB
            </span>
          </div>
        </div>

        <div class="init-terminal__continue-row">
          <p class="init-terminal__continue-hint">
            All {{ init.summaryData.value?.embeddings?.with_embeddings || 185 }}
            countries are ready for the pipeline.
            Click continue to access the map.
          </p>
          <button
            class="init-terminal__continue-btn"
            @click="handleContinue"
          >
            Continue to Map &rarr;
          </button>
        </div>
      </div>
    </template>

    <!-- ── Idle state (before starting) ──────────────── -->
    <template v-else>
      <div class="init-terminal__idle">
        <div class="init-terminal__idle-icon">&#127758;</div>
        <h3 class="init-terminal__idle-title">Planet Not Initialized</h3>
        <p class="init-terminal__idle-text">
          No planet has been initialized. Use the <strong>Planet Initialization</strong>
          panel on the right to start.
        </p>
        <p v-if="init.suggestedPlanetFilePath.value" class="init-terminal__idle-path">
          Planet PBF: <code>{{ init.suggestedPlanetFilePath.value }}</code>
        </p>
      </div>
    </template>
  </div>
</template>

<style scoped>
/* ── Container ────────────────────────────────── */
.init-terminal {
  min-height: 520px;
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
  padding: 1.5rem;
  border-radius: 12px;
  background: #0a0f1e;
  border: 1px solid #1f2937;
  transition: border-color 0.3s, box-shadow 0.3s;
}

.init-terminal--running {
  border-color: #1e3a5f;
  box-shadow: 0 0 24px rgba(59, 130, 246, 0.06);
}

.init-terminal--complete {
  border-color: rgba(34, 197, 94, 0.3);
}

.init-terminal--failed {
  border-color: #450a0a;
}

/* ── Header ────────────────────────────────────── */
.init-terminal__header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 1rem;
}

.init-terminal__brand {
  display: flex;
  align-items: flex-start;
  gap: 0.75rem;
}

.init-terminal__icon {
  font-size: 2rem;
  opacity: 0.5;
  line-height: 1;
}

.init-terminal__title {
  font-size: 1.2rem;
  font-weight: 700;
  color: #f3f4f6;
  display: block;
}

.init-terminal__subtitle {
  font-size: 0.82rem;
  color: #6b7280;
  display: block;
  margin-top: 0.2rem;
  max-width: 400px;
  line-height: 1.5;
}

.init-terminal__badge {
  padding: 0.15rem 0.6rem;
  border-radius: 999px;
  font-size: 0.72rem;
  border: 1px solid transparent;
  white-space: nowrap;
  flex-shrink: 0;
}

.init-terminal__badge--success {
  background: rgba(34, 197, 94, 0.15);
  border-color: #22c55e;
  color: #22c55e;
}

.init-terminal__badge--danger {
  background: rgba(239, 68, 68, 0.15);
  border-color: #ef4444;
  color: #ef4444;
}

.init-terminal__badge--info {
  background: rgba(59, 130, 246, 0.15);
  border-color: #60a5fa;
  color: #60a5fa;
}

.init-terminal__badge--secondary {
  background: rgba(75, 85, 99, 0.2);
  border-color: #4b5563;
  color: #9ca3af;
}

/* ── Path hint ──────────────────────────────────── */
.init-terminal__path-hint {
  margin: 0;
  font-size: 0.75rem;
  color: #525252;
}

.init-terminal__path-hint code {
  color: #9ca3af;
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, 'Liberation Mono',
    'Courier New', monospace;
  font-size: 0.7rem;
  word-break: break-all;
}

/* ── Progress ───────────────────────────────────── */
.init-terminal__progress-row {
  display: flex;
  align-items: center;
  gap: 0.6rem;
}

.init-terminal__progress-track {
  flex: 1;
  height: 6px;
  background: #1f2937;
  border-radius: 999px;
  overflow: hidden;
}

.init-terminal__progress-fill {
  height: 100%;
  background: linear-gradient(90deg, #3b82f6, #60a5fa);
  border-radius: 999px;
  transition: width 0.5s ease;
}

.init-terminal__progress-pct {
  font-size: 0.8rem;
  font-weight: 700;
  color: #60a5fa;
  min-width: 2.5rem;
  text-align: right;
}

.init-terminal__step-count {
  font-size: 0.75rem;
  color: #9ca3af;
}

.init-terminal__step-name {
  color: #60a5fa;
}

/* ── Live log (terminal style) ───────────────────── */
.init-terminal__log {
  max-height: 260px;
  overflow-y: auto;
  padding: 0.5rem 0.6rem;
  background: rgba(0, 0, 0, 0.45);
  border-radius: 0.5rem;
  border: 1px solid #1e293b;
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, 'Liberation Mono',
    'Courier New', monospace;
  font-size: 0.72rem;
  line-height: 1.6;
}

.init-terminal__log-line {
  display: flex;
  gap: 0.5rem;
  padding: 0.05rem 0;
}

.init-terminal__log-time {
  color: #525252;
  flex-shrink: 0;
  user-select: none;
}

.init-terminal__log-text {
  color: #d4d4d8;
  word-break: break-word;
  white-space: pre-wrap;
}

.init-terminal__log-cursor {
  padding: 0.05rem 0;
}

.init-terminal__cursor-blink {
  color: #22c55e;
  animation: blink 1s step-end infinite;
}

@keyframes blink {
  0%, 100% { opacity: 1; }
  50% { opacity: 0; }
}

/* ── Steps panel ─────────────────────────────────── */
.init-terminal__steps-details {
  margin-top: 0.25rem;
}

.init-terminal__steps-summary {
  font-size: 0.75rem;
  color: #9ca3af;
  cursor: pointer;
  user-select: none;
  padding: 0.2rem 0;
}

.init-terminal__steps-summary::-webkit-details-marker {
  color: #4b5563;
}

.init-terminal__current-hint {
  color: #60a5fa;
  font-weight: 500;
}

.init-terminal__steps {
  list-style: none;
  margin: 0.25rem 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 0.05rem;
}

.init-terminal__step {
  display: flex;
  align-items: flex-start;
  gap: 0.4rem;
  padding: 0.25rem 0.35rem;
  border-radius: 0.3rem;
  transition: background 0.2s;
}

.init-terminal__step--active {
  background: rgba(59, 130, 246, 0.06);
}

/* Step icons */
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

.init-terminal__step-body {
  flex: 1;
  min-width: 0;
}

.init-terminal__step-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 0.5rem;
}

.init-terminal__step-label {
  font-weight: 500;
  font-size: 0.78rem;
  color: #d1d5db;
}

.init-terminal__step-label--active {
  color: #60a5fa;
}

.init-terminal__step-label--done {
  color: #22c55e;
}

.init-terminal__step-status-icon {
  font-size: 0.75rem;
  flex-shrink: 0;
  width: 1rem;
  text-align: center;
}

.init-terminal__step-message {
  margin: 0.05rem 0 0;
  font-size: 0.68rem;
  color: #6b7280;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.init-terminal__spinner-sm {
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

/* ── Error ───────────────────────────────────────── */
.init-terminal__error {
  margin-top: 0.25rem;
  font-size: 0.8rem;
  color: #fecaca;
  padding: 0.4rem 0.6rem;
  background: rgba(239, 68, 68, 0.08);
  border: 1px solid rgba(239, 68, 68, 0.15);
  border-radius: 0.4rem;
}

/* ── Complete section ─────────────────────────────── */
.init-terminal__complete {
  margin-top: 0.5rem;
  padding: 1rem;
  border-radius: 0.6rem;
  background: rgba(34, 197, 94, 0.06);
  border: 1px solid rgba(34, 197, 94, 0.2);
}

.init-terminal__summary-cards {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(100px, 1fr));
  gap: 0.5rem;
  margin-bottom: 1rem;
}

.init-terminal__summary-card {
  display: flex;
  flex-direction: column;
  padding: 0.5rem;
  border-radius: 0.4rem;
  background: rgba(15, 23, 42, 0.6);
  border: 1px solid #1e293b;
}

.init-terminal__summary-card-label {
  font-size: 0.65rem;
  color: #6b7280;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.init-terminal__summary-card-value {
  font-size: 1.1rem;
  font-weight: 700;
  color: #f3f4f6;
  margin-top: 0.1rem;
}

.init-terminal__continue-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
  flex-wrap: wrap;
}

.init-terminal__continue-hint {
  margin: 0;
  font-size: 0.78rem;
  color: #9ca3af;
  max-width: 360px;
  line-height: 1.5;
}

.init-terminal__continue-btn {
  padding: 0.6rem 1.5rem;
  font-size: 0.9rem;
  font-weight: 700;
  background: #4f46e5;
  border: 1px solid #6366f1;
  color: #fff;
  border-radius: 0.6rem;
  cursor: pointer;
  transition: background 0.15s, border-color 0.15s;
  white-space: nowrap;
}

.init-terminal__continue-btn:hover {
  background: #6366f1;
  border-color: #818cf8;
}

/* ── Idle state ───────────────────────────────────── */
.init-terminal__idle {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 0.5rem;
  text-align: center;
  padding: 2rem;
}

.init-terminal__idle-icon {
  font-size: 3rem;
  opacity: 0.3;
}

.init-terminal__idle-title {
  margin: 0;
  color: #9ca3af;
  font-size: 1.2rem;
  font-weight: 600;
}

.init-terminal__idle-text {
  margin: 0;
  color: #6b7280;
  font-size: 0.85rem;
  max-width: 360px;
  line-height: 1.5;
}

.init-terminal__idle-path {
  margin: 0.25rem 0 0;
  font-size: 0.75rem;
  color: #525252;
}

.init-terminal__idle-path code {
  color: #9ca3af;
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, 'Liberation Mono',
    'Courier New', monospace;
  font-size: 0.7rem;
  word-break: break-all;
}
</style>
