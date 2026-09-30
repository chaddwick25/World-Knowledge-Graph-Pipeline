<template>
  <Teleport to="body">
    <div v-if="open" class="rerun-backdrop" @click.self="$emit('close')">
      <div class="rerun-modal">
        <header class="rerun-modal__header">
          <h3 class="rerun-modal__title">Confirm Re-run</h3>
          <button class="rerun-modal__close" @click="$emit('close')">&times;</button>
        </header>
        <div class="rerun-modal__body">
          <p>
            Pipeline already completed for
            <span class="font-monospace">{{ snapshotDate }}</span>.
            Re-run and overwrite the existing results?
          </p>
        </div>
        <footer class="rerun-modal__footer">
          <button class="btn btn-sm btn-secondary" @click="$emit('close')">Cancel</button>
          <button class="btn btn-sm btn-primary" @click="$emit('confirm')">Re-run</button>
        </footer>
      </div>
    </div>
  </Teleport>
</template>

<script>
/**
 * RerunConfirmModal
 *
 * Leaf component (layer 3): confirmation dialog for re-running a
 * completed pipeline. Props in, events out, no store.
 *
 * Props:
 *   open         - Whether the modal is visible.
 *   snapshotDate - The snapshot date being re-run ("YYYY_MM_DD").
 *
 * Emits:
 *   close   - User dismissed the modal (cancel or backdrop click).
 *   confirm - User confirmed the re-run.
 */

export default {
  name: 'RerunConfirmModal',
  props: {
    open: {
      type: Boolean,
      default: false,
    },
    snapshotDate: {
      type: String,
      default: '',
    },
  },
  emits: ['close', 'confirm'],
}
</script>

<style scoped>
.rerun-backdrop {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.5);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1050;
}

.rerun-modal {
  background: var(--bs-body-bg, #1a1a2e);
  border: 1px solid var(--bs-border-color, #2a2a4a);
  border-radius: 0.5rem;
  width: 90%;
  max-width: 400px;
  box-shadow: 0 0.5rem 1rem rgba(0, 0, 0, 0.5);
}

.rerun-modal__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0.75rem 1rem;
  border-bottom: 1px solid var(--bs-border-color, #2a2a4a);
}

.rerun-modal__title {
  font-size: 1rem;
  font-weight: 600;
  margin: 0;
}

.rerun-modal__close {
  background: none;
  border: none;
  color: var(--bs-secondary, #9ca3af);
  font-size: 1.25rem;
  line-height: 1;
  cursor: pointer;
  padding: 0 0.25rem;
}

.rerun-modal__close:hover {
  color: var(--bs-body-color, #e5e7eb);
}

.rerun-modal__body {
  padding: 1rem;
  font-size: 0.875rem;
  color: var(--bs-body-color, #e5e7eb);
}

.rerun-modal__footer {
  display: flex;
  justify-content: flex-end;
  gap: 0.5rem;
  padding: 0.75rem 1rem;
  border-top: 1px solid var(--bs-border-color, #2a2a4a);
}
</style>
