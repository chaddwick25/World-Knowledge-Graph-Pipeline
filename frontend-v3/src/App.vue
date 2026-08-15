<template>
  <div class="app-shell">
    <main class="app-shell__main">
      <router-view />
    </main>
    <QueryConfirmationModal />
  </div>
</template>

<script>
import QueryConfirmationModal from './components/QueryConfirmationModal.vue'

export default {
  name: 'AppShell',
  components: { QueryConfirmationModal },
  methods: {
    isActive(path) {
      return this.$route.path === path
    },
    go(path) {
      if (this.$route.path !== path) {
        this.$router.push(path)
      }
    },
  },
}
</script>

<style scoped>
.app-shell {
  display: flex;
  flex-direction: column;
  height: 100vh;
  background: #0f172a;
  color: #e5e7eb;
  overflow: hidden;
}

.app-shell__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
  padding: 0.75rem 1.5rem;
  border-bottom: 1px solid #111827;
  background: rgba(15, 23, 42, 0.98);
}

.app-shell__brand {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  cursor: pointer;
}

.app-shell__brand-mark {
  font-size: 1.1rem;
  text-decoration: none;
  transition: opacity 0.2s ease;
}

.app-shell__brand-mark:hover {
  opacity: 0.7;
}

.app-shell__title {
  font-size: 1.1rem;
  font-weight: 600;
}

.app-shell__nav {
  display: flex;
  gap: 0.5rem;
}

.app-shell__nav-link {
  padding: 0.3rem 0.75rem;
  border-radius: 999px;
  border: 1px solid transparent;
  background: transparent;
  color: #e5e7eb;
  font-size: 0.85rem;
  cursor: pointer;
}

.app-shell__nav-link--active {
  border-color: #4f46e5;
  background: #111827;
}

.app-shell__main {
  flex: 1;
  overflow: hidden;
}
</style>
