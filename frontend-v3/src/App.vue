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
}
</script>

<style scoped>
.app-shell {
  display: flex;
  flex-direction: column;
  height: 100vh;
  background: ***REMOVED***0f172a;
  color: ***REMOVED***e5e7eb;
  overflow: hidden;
}

.app-shell__main {
  flex: 1;
  overflow: hidden;
}
</style>
