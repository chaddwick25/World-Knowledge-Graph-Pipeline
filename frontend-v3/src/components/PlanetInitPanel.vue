/**
 * PlanetInitPanel — Read-only planet-initialization status card.
 *
 * Planet init runs as a Docker startup step (``python manage.py init_planet``
 * via ``docker-entrypoint.sh``); there is no in-app trigger anymore. This
 * card reports current readiness from ``GET /system/status/`` and lets the
 * user re-ping it (Refresh). Once the backend reports ready, Home.vue flips
 * to the map, which hydrates its own country data.
 *
 * Shown in both the map area (centered) and the sidebar while
 * ``isSystemReady === false``.
 */

<template>
  <div class="card planet-init-panel">
    <div class="card-body d-flex flex-column gap-2">
      <div class="d-flex align-items-center justify-content-between gap-2">
        <span class="fw-semibold small">Planet Initialization</span>
        <span
          class="badge rounded-pill"
          :class="checking ? 'text-bg-info' : 'text-bg-secondary'"
        >{{ checking ? 'Checking…' : 'Not Initialized' }}</span>
      </div>

      <p class="small text-secondary mb-0">
        Planet init runs automatically as a Docker startup step. Restart the backend
        container, or run <code class="text-body">python manage.py init_planet</code>
        to initialize.
      </p>

      <p v-if="suggestedPlanetFilePath" class="small text-secondary mb-0 text-break">
        Planet PBF: <code class="text-body">{{ suggestedPlanetFilePath }}</code>
      </p>

      <div v-if="systemError" class="alert alert-danger small py-1 px-2 mb-0">
        {{ systemError }}
      </div>

      <button
        type="button"
        class="btn btn-sm btn-outline-primary align-self-start"
        :disabled="checking"
        @click="$emit('refresh')"
      >
        {{ checking ? 'Checking…' : 'Refresh Status' }}
      </button>
    </div>
  </div>
</template>

<script>
export default {
  name: 'PlanetInitPanel',
  props: {
    suggestedPlanetFilePath: { type: String, default: '' },
    systemError: { type: String, default: '' },
    checking: { type: Boolean, default: false },
  },
  emits: ['refresh'],
}
</script>
