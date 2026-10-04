<template>
  <!-- Results table. Layer-3 leaf: props in, events out. showNameless lives
       in the parent (it drives the displayResults filter) — toggled via
       v-model:show-nameless. showScores/formatTags are local presentation. -->
  <div v-if="rows.length > 0" class="d-flex flex-column gap-1">
    <div class="d-flex align-items-center justify-content-between">
      <div class="d-flex align-items-center gap-1">
        <h6 class="small fw-semibold mb-0">
          Results ({{ rows.length }}<template v-if="total > rows.length"> / {{ total }}</template>)
        </h6>
        <!-- Unnamed-entity toggle (2026-10-01): the AI answer can cite
             entities the table hides (bars without a name tag, filtered
             2026-09-28). Visible whenever nameless rows EXIST — not
             only while they are hidden — so the filter can be
             re-applied after showing them. -->
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
      <button
        class="btn btn-link btn-sm text-secondary p-0"
        @click="showScores = !showScores"
      >
        {{ showScores ? 'Hide' : 'Show' }} scores
      </button>
    </div>
    <div class="table-responsive" style="max-height: 360px; overflow-y: auto;">
      <table class="table table-sm table-borderless results-table mb-0" style="font-size: 0.72rem;">
        <thead class="table-dark">
          <tr>
            <th>Name</th>
            <th>Tags</th>
            <th>Class</th>
            <template v-if="showScores">
              <th v-for="col in scoreColumns" :key="col.key" class="num">{{ col.label }}</th>
            </template>
            <th class="num">Final</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in rows" :key="`${item.osm_type}-${item.osm_id}`">
            <td>
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
            </td>
            <td><small>{{ formatTags(item.tags) }}</small></td>
            <td>{{ (item.wkg_class || '—').replace(/^wkgs:/, '') }}</td>
            <template v-if="showScores">
              <td v-for="col in scoreColumns" :key="col.key" class="num">{{ item.scores?.[col.key]?.toFixed(3) || '—' }}</td>
            </template>
            <td class="num">
              <strong>{{ item.scores?.final_score?.toFixed(3) || '—' }}</strong>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
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
    scoreColumns: { type: Array, default: () => [] },
  },
  emits: ['update:showNameless', 'name-hover', 'name-leave'],
  data() {
    return {
      showScores: false,
    }
  },
  methods: {
    formatTags(tags) {
      if (!tags) return ''
      const entries = Object.entries(tags).slice(0, 3)
      const str = entries.map(([k, v]) => `${k}=${v}`).join(', ')
      return entries.length < Object.keys(tags).length ? str + '…' : str
    },
  },
}
</script>

<style scoped>
/* Moved from SemanticSearchPanel.vue — scoped CSS does not cross into
   child components, so the table styles live with the table. */

/* Result-table entity name links: underline on hover only (the plain
   Bootstrap link default is always-underlined; its text-decoration-none
   utility carries !important and would beat a hover rule, hence the
   dedicated class). */
.results-name-link {
  text-decoration: none;
}
.results-name-link:hover {
  text-decoration: underline;
}

/* Results table: active row (reticle entity) stays distinct on hover;
   balanced cell padding keeps the scan line level. */
.results-table tbody tr:hover {
  background-color: var(--bs-tertiary-bg);
}
.results-table tbody tr:hover td:first-child {
  box-shadow: inset 3px 0 0 var(--bs-info);
}
.results-table td {
  padding-top: 0.5rem;
  padding-bottom: 0.5rem;
  padding-left: 0.625rem;
  padding-right: 0.625rem;
  vertical-align: middle;
}
</style>
