<template>
  <!-- DoppelgangerSurface (2026-10-15, ISSUE-46): the doppelganger agent
       at /agents/doppelganger/:cc — ResearchPanel (interview → brief →
       DOPPELGANGER_REPORT) + WorldKGMap inside the shared shell;
       country comes from the route param. -->
  <SurfaceShell
    :cc="cc"
    title="Doppelganger"
    back-to="/"
    leaf-base="/agents/doppelganger"
  >
    <template ***REMOVED***default="{ country, snapshotDate }">
      <ResearchPanel
        v-if="country"
        :country-name="country.name"
        :snapshot-date="snapshotDate"
      />
    </template>
  </SurfaceShell>
</template>

<script>
import SurfaceShell from '../components/SurfaceShell.vue'
import ResearchPanel from '../components/ResearchPanel.vue'

export default {
  name: 'DoppelgangerSurface',
  components: { SurfaceShell, ResearchPanel },
  props: {
    cc: { type: String, required: true },
  },
}
</script>
