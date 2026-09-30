<template>
  <Teleport to="body">
    <div v-if="open" class="ppw-backdrop" @click.self="$emit('close')">
      <div class="ppw-modal">
        <header class="ppw-modal__header">
          <h3 class="ppw-modal__title">Pipeline Password</h3>
          <button class="ppw-modal__close" @click="$emit('close')">&times;</button>
        </header>
        <div class="ppw-modal__body">
          <p class="ppw-modal__text">
            Enter the demo password to start the pipeline for this country.
          </p>
          <input
            ref="input"
            v-model="password"
            type="password"
            class="ppw-modal__input form-control"
            placeholder="Password"
            @keyup.enter="submit"
          />
          <p v-if="error" class="ppw-modal__error small mb-0">{{ error }}</p>
        </div>
        <footer class="ppw-modal__footer">
          <button class="btn btn-sm btn-secondary" @click="$emit('close')">Cancel</button>
          <button class="btn btn-sm btn-primary" @click="submit">Run Pipeline</button>
        </footer>
      </div>
    </div>
  </Teleport>
</template>

<script>
export default {
  name: 'PipelinePasswordModal',
  props: {
    open: {
      type: Boolean,
      default: false,
    },
    error: {
      type: String,
      default: '',
    },
  },
  emits: ['close', 'confirm'],
  data() {
    return {
      password: '',
    }
  },
  watch: {
    open(newVal) {
      if (newVal) {
        this.password = ''
        this.$nextTick(() => {
          this.$refs.input?.focus()
        })
      }
    },
  },
  methods: {
    submit() {
      this.$emit('confirm', this.password.trim())
    },
  },
}
</script>

<style scoped>
.ppw-backdrop {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.5);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1060;
}

.ppw-modal {
  background: var(--bs-body-bg, #1a1a2e);
  border: 1px solid var(--bs-border-color, #2a2a4a);
  border-radius: 0.5rem;
  width: 90%;
  max-width: 400px;
  box-shadow: 0 0.5rem 1rem rgba(0, 0, 0, 0.5);
}

.ppw-modal__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0.75rem 1rem;
  border-bottom: 1px solid var(--bs-border-color, #2a2a4a);
}

.ppw-modal__title {
  font-size: 1rem;
  font-weight: 600;
  margin: 0;
}

.ppw-modal__close {
  background: none;
  border: none;
  color: var(--bs-secondary, #9ca3af);
  font-size: 1.25rem;
  line-height: 1;
  cursor: pointer;
  padding: 0 0.25rem;
}

.ppw-modal__close:hover {
  color: var(--bs-body-color, #e5e7eb);
}

.ppw-modal__body {
  padding: 1rem;
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
  font-size: 0.875rem;
  color: var(--bs-body-color, #e5e7eb);
}

.ppw-modal__text {
  margin: 0;
}

.ppw-modal__input {
  background: var(--bs-body-bg, #1a1a2e);
  color: var(--bs-body-color, #e5e7eb);
  border-color: var(--bs-border-color, #2a2a4a);
}

.ppw-modal__error {
  color: var(--bs-danger, #dc3545);
}

.ppw-modal__footer {
  display: flex;
  justify-content: flex-end;
  gap: 0.5rem;
  padding: 0.75rem 1rem;
  border-top: 1px solid var(--bs-border-color, #2a2a4a);
}
</style>
