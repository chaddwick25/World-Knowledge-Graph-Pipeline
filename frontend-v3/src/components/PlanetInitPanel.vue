<script>
/**
 * PlanetInitPanel — Sidebar controller for Planet Initialization.
 *
 * This is the lightweight sidebar component. It delegates all state
 * to the shared usePlanetInit composable, which also powers the
 * live terminal in the map area (PlanetInitTerminal.vue).
 *
 * Shows:
 *   - Status badge + Initialize/Retry button
 *   - Compact step list
 *   - Summary + Continue button when complete
 *   - System Summary modal triggered on continue
 */

import { computed, onUnmounted } from 'vue'
import { usePlanetInit } from '../composables/usePlanetInit'
import SystemSummaryModal from './SystemSummaryModal.vue'

export default {
  name: 'PlanetInitPanel',
  components: { SystemSummaryModal },
  props: {
    suggestedPlanetFilePath: { type: String, default: '' },
  },
  emits: ['system-ready'],
  setup(props, { emit }) {
    const init = usePlanetInit()

    // Sync suggested planet file path into the composable
    if (props.suggestedPlanetFilePath) {
      init.setSuggestedPlanetFilePath(props.suggestedPlanetFilePath)
    }

    // Step icon helper
    function iconClass(stepStatus) {
      if (stepStatus === 'completed') return 'step-icon step-icon--success'
      if (stepStatus === 'in_progress') return 'step-icon step-icon--running'
      if (stepStatus === 'failed') return 'step-icon step-icon--failed'
      if (stepStatus === 'skipped') return 'step-icon step-icon--skipped'
      return 'step-icon step-icon--pending'
    }

    function handleContinue() {
      emit('system-ready')
      init.showSummaryModal.value = true
    }

    // Cleanup is handled by the composable singleton

    return {
      init,
      iconClass,
      handleContinue,
    }
  },
}
</script>

<template>
  <section
    class="planet-init"
    :class="{
      'planet-init--running': init.isRunning.value,
      'planet-init--complete': init.isComplete.value,
      'planet-init--failed': init.isFailed.value,
    }"
  >
    <!-- ── Header ──────────────────────────────────────── -->
    <header class="planet-init__header">
      <div class="planet-init__header-left">
        <span class="planet-init__title">Planet Initialization</span>
        <span v-if="init.isRunning.value" class="planet-init__step-count">
          {{ init.completedCount.value }}/{{ init.totalCount.value }} steps
        </span>
      </div>
      <div class="planet-init__header-right">
        <span
          class="planet-init__badge"
          :class="`planet-init__badge--${init.badgeVariant.value}`"
        >{{ init.badgeLabel.value }}</span>

        <!-- Initialize button (idle state) -->
        <button
          v-if="init.canInitialize.value"
          class="planet-init__btn"
          :disabled="init.isRunning.value"
          @click="init.handleInitialize"
        >
          <span v-if="init.isRunning.value" class="planet-init__spinner"></span>
          <span>{{ 'Initialize Planet' }}</span>
        </button>

        <!-- Retry button (failed state) -->
        <button
          v-if="init.isFailed.value"
          class="planet-init__btn planet-init__btn--danger"
          @click="init.handleInitialize"
        >
          Retry
        </button>

        <!-- Re-initialize (complete state) -->
        <button
          v-if="init.isComplete.value"
          class="planet-init__btn"
          @click="init.handleInitialize"
        >
          Re-initialize
        </button>
      </div>
    </header>

    <!-- ── Quick status summary ────────────────────────── -->
    <p v-if="init.isRunning.value && init.currentStepName.value" class="planet-init__status-line">
      {{ init.currentStepName.value }}
    </p>

    <!-- ── Pre-populated planet path (idle state) ──────── -->
    <p v-if="suggestedPlanetFilePath && !init.planetInitActive.value" class="planet-init__path-hint">
      Planet PBF: <code>{{ suggestedPlanetFilePath }}</code>
    </p>

    <!-- ── Error message ──────────────────────────────── -->
    <p
      v-if="init.errorMessage.value && init.isFailed.value"
      class="planet-init__error"
    >{{ init.errorMessage.value }}</p>

    <!-- ── Success state (show Continue button) ────────── -->
    <div v-if="init.isComplete.value" class="planet-init__success-block">
      <p class="planet-init__success-text">
        &#10003; Planet initialized successfully.
      </p>
      <button class="planet-init__btn planet-init__btn--continue" @click="handleContinue">
        Continue to Map &rarr;
      </button>
    </div>

    <!-- ── System Summary Modal ── -->
    <SystemSummaryModal
      :open="init.showSummaryModal.value"
      @close="init.showSummaryModal.value = false"
    />
  </section>
</template>

<style scoped>
/* ── Container ───────────────────────────────── */
.planet-init {
  margin: 0.75rem 0;
  padding: 0.75rem 1rem;
  border-radius: 0.9rem;
  background: #020617;
  border: 1px solid #111827;
  font-size: 0.82rem;
  transition: border-color 0.3s;
}

.planet-init--running {
  border-color: #1e3a5f;
}

.planet-init--complete {
  border-color: rgba(34, 197, 94, 0.3);
}

.planet-init--failed {
  border-color: #450a0a;
}

/* ── Header ───────────────────────────────────── */
.planet-init__header {
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  align-items: center;
  gap: 0.5rem;
}

.planet-init__header-left {
  display: flex;
  align-items: baseline;
  gap: 0.5rem;
  min-width: 0;
}

.planet-init__title {
  font-weight: 700;
  font-size: 0.95rem;
  color: #f3f4f6;
}

.planet-init__step-count {
  font-size: 0.75rem;
  color: #9ca3af;
  white-space: nowrap;
}

.planet-init__status-line {
  margin: 0.25rem 0 0;
  font-size: 0.72rem;
  color: #60a5fa;
  font-weight: 500;
}

.planet-init__path-hint {
  margin: 0.35rem 0 0;
  font-size: 0.72rem;
  color: #525252;
}

.planet-init__path-hint code {
  color: #9ca3af;
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, 'Liberation Mono', 'Courier New', monospace;
  font-size: 0.68rem;
  word-break: break-all;
}

.planet-init__header-right {
  display: flex;
  align-items: center;
  gap: 0.35rem;
  flex-shrink: 0;
}

/* ── Badge ──────────────────────────────────────── */
.planet-init__badge {
  padding: 0.1rem 0.5rem;
  border-radius: 999px;
  font-size: 0.72rem;
  border: 1px solid transparent;
  white-space: nowrap;
}

.planet-init__badge--success {
  background: rgba(34, 197, 94, 0.15);
  border-color: #22c55e;
  color: #22c55e;
}

.planet-init__badge--danger {
  background: rgba(239, 68, 68, 0.15);
  border-color: #ef4444;
  color: #ef4444;
}

.planet-init__badge--info {
  background: rgba(59, 130, 246, 0.15);
  border-color: #60a5fa;
  color: #60a5fa;
}

.planet-init__badge--secondary {
  background: rgba(75, 85, 99, 0.2);
  border-color: #4b5563;
  color: #9ca3af;
}

/* ── Button ──────────────────────────────────────── */
.planet-init__btn {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  padding: 0.3rem 0.7rem;
  border-radius: 0.5rem;
  border: 1px solid #4f46e5;
  background: #111827;
  color: #e5e7eb;
  font-size: 0.78rem;
  font-weight: 600;
  cursor: pointer;
  white-space: nowrap;
  transition: background 0.15s, border-color 0.15s, opacity 0.15s;
}

.planet-init__btn:hover:not(:disabled) {
  background: #1f2937;
  border-color: #6366f1;
}

.planet-init__btn:disabled {
  opacity: 0.5;
  cursor: default;
}

.planet-init__btn--danger {
  border-color: #dc2626;
}

.planet-init__btn--danger:hover {
  background: rgba(220, 38, 38, 0.15);
}

.planet-init__spinner {
  display: inline-block;
  width: 14px;
  height: 14px;
  border-radius: 999px;
  border: 2px solid rgba(191, 219, 254, 0.4);
  border-top-color: #eff6ff;
  animation: spin 0.8s linear infinite;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

/* ── Success block ──────────────────────────────── */
.planet-init__success-block {
  margin-top: 0.5rem;
  padding: 0.5rem 0.6rem;
  border-radius: 0.5rem;
  background: rgba(34, 197, 94, 0.06);
  border: 1px solid rgba(34, 197, 94, 0.2);
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 0.5rem;
}

.planet-init__success-text {
  margin: 0;
  font-size: 0.78rem;
  color: #bbf7d0;
  width: 100%;
}

.planet-init__btn--continue {
  padding: 0.5rem 1.2rem;
  font-size: 0.85rem;
  font-weight: 700;
  background: #4f46e5;
  border-color: #6366f1;
  color: #fff;
  border-radius: 0.6rem;
}

.planet-init__btn--continue:hover {
  background: #6366f1;
  border-color: #818cf8;
}

/* ── Error ───────────────────────────────────────── */
.planet-init__error {
  margin: 0.35rem 0 0;
  font-size: 0.78rem;
  color: #fecaca;
  padding: 0.3rem 0.4rem;
  background: rgba(239, 68, 68, 0.08);
  border-radius: 0.4rem;
}
</style>