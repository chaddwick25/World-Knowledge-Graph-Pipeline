<template>
  <teleport to="body">
    <div v-if="isOpen" class="modal-backdrop" @click="onBackdropClick">
      <div class="modal" :class="`modal--${size}`">
        <header class="modal__header">
          <h3 class="modal__title">{{ title }}</h3>
          <button class="modal__close" type="button" @click="close">×</button>
        </header>
        <section class="modal__body">
          <slot />
        </section>
        <footer class="modal__footer">
          <slot name="footer">
            <button type="button" class="modal__btn" @click="close">Close</button>
          </slot>
        </footer>
      </div>
    </div>
  </teleport>
</template>

<script>
export default {
  name: 'BaseModal',
  props: {
    modelValue: {
      type: Boolean,
      default: false,
    },
    title: {
      type: String,
      default: '',
    },
    size: {
      type: String,
      default: 'md', // 'sm' | 'md' | 'lg'
    },
  },
  emits: ['update:modelValue', 'show'],
  computed: {
    isOpen: {
      get() {
        return this.modelValue
      },
      set(v) {
        this.$emit('update:modelValue', v)
      },
    },
  },
  methods: {
    close() {
      this.isOpen = false
    },
    onBackdropClick(e) {
      if (e.target === e.currentTarget) {
        this.close()
      }
    },
  },
}
</script>

<style scoped>
.modal-backdrop {
  position: fixed;
  inset: 0;
  background: rgba(15, 23, 42, 0.7);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 9999;
}

.modal {
  background: #020617;
  border-radius: 0.9rem;
  border: 1px solid #1f2937;
  color: #e5e7eb;
  box-shadow: 0 25px 70px rgba(15, 23, 42, 0.8);
  max-height: 90vh;
  display: flex;
  flex-direction: column;
}

.modal--sm {
  width: 420px;
}

.modal--md {
  width: 640px;
}

.modal--lg {
  width: min(1100px, 95vw);
}

.modal__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0.75rem 1rem;
  border-bottom: 1px solid #111827;
}

.modal__title {
  margin: 0;
  font-size: 1rem;
  font-weight: 600;
}

.modal__close {
  border: none;
  background: transparent;
  color: #9ca3af;
  font-size: 1.2rem;
  cursor: pointer;
}

.modal__body {
  padding: 1rem;
  overflow-y: auto;
}

.modal__footer {
  border-top: 1px solid #111827;
  padding: 0.75rem 1rem;
  display: flex;
  justify-content: flex-end;
}

.modal__btn {
  padding: 0.4rem 0.9rem;
  border-radius: 0.6rem;
  border: 1px solid #4b5563;
  background: #020617;
  color: #e5e7eb;
  font-size: 0.85rem;
  cursor: pointer;
}
</style>
