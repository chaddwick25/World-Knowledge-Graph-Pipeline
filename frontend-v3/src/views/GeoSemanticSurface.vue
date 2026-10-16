<template>
  <!-- GeoSemanticSurface (2026-10-15, ISSUE-46): the geo-semantic tool
       at /tools/geo-semantic/:cc — SemanticSearchPanel + WorldKGMap
       inside the shared shell; country comes from the route param. -->
  <SurfaceShell
    :cc="cc"
    title="Geo-Semantic Search"
    back-to="/"
    leaf-base="/tools/geo-semantic"
  >
    <template ***REMOVED***default="{ country, snapshotDate, setSearchResults, setQueryGraph, setTemplateVisualization }">
      <SemanticSearchPanel
        v-if="country"
        :country-name="country.name"
        :country-code="country.iso_code"
        :snapshot-date="snapshotDate"
        @search-results="setSearchResults"
        @query-graph="setQueryGraph"
        @template-visualization="setTemplateVisualization"
      />
    </template>
  </SurfaceShell>
</template>

<script>
import SurfaceShell from '../components/SurfaceShell.vue'
import SemanticSearchPanel from '../components/SemanticSearchPanel.vue'

export default {
  name: 'GeoSemanticSurface',
  components: { SurfaceShell, SemanticSearchPanel },
  props: {
    cc: { type: String, required: true },
  },
}
</script>
