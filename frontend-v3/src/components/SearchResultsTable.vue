<template>
  <div v-if="rows.length > 0" class="d-flex flex-column gap-1">
    <div class="d-flex align-items-center gap-1">
      <h6 class="small fw-semibold mb-0">
        Results ({{ rows.length }}<template v-if="total > rows.length"> / {{ total }}</template>)
      </h6>
      <!-- Unnamed-entity toggle (2026-10-01): the AI answer can cite
           entities the list hides (bars without a name tag). -->
      <button
        v-if="hasNameless"
        class="btn btn-link btn-sm text-secondary p-0"
        :title="showNameless ? 'Hide unnamed results' : 'Show unnamed results'"
        :aria-pressed="showNameless"
        @click="$emit('update:showNameless', !showNameless)"
      >
        <i :class="showNameless ? 'bi bi-funnel-fill' : 'bi bi-funnel'"></i>
      </button>
    </div>
    <ol class="results-list list-group list-group-numbered" style="max-height: 360px; overflow-y: auto; font-size: 0.72rem;">
      <li
        v-for="item in rows"
        :key="`${item.osm_type}-${item.osm_id}`"
        class="list-group-item"
      >
        <a
          :href="`https://www.openstreetmap.org/${item.osm_type}/${item.osm_id}`"
          target="_blank"
          class="results-name-link"
          style="color: var(--bs-info-text-emphasis);"
          @mouseenter="$emit('name-hover', item)"
          @mouseleave="$emit('name-leave', item)"
        >
          {{ item.name || '—' }}
        </a>
        <small v-if="item.wkg_class" class="text-secondary"> · {{ item.wkg_class.replace(/^wkgs:/, '') }}</small>
      </li>
    </ol>
  </div>
</template>

<script>
export default {
  name: 'SearchResultsTable',
  props: {
    rows: { type: Array, required: true },
    total: { type: Number, default: 0 },
    hasNameless: { type: Boolean, default: false },
    showNameless: { type: Boolean, default: false },
  },
  emits: ['update:showNameless', 'name-hover', 'name-leave'],
}
</script>

<style scoped>
/* Result-list entity name links: underline on hover only. */
.results-name-link {
  text-decoration: none;
}
.results-name-link:hover {
  text-decoration: underline;
}

/* Active row (reticle entity) stays distinct on hover. */
.results-list .list-group-item:hover {
  background-color: var(--bs-tertiary-bg);
}
.results-list .list-group-item:hover > a:first-child {
  box-shadow: inset 3px 0 0 var(--bs-info);
}
</style>
