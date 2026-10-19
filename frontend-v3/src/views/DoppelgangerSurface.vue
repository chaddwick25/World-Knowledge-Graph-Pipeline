<template>
  <!-- DoppelgangerSurface (2026-10-15, ISSUE-46): the doppelganger agent
       at /agents/doppelganger/:cc — ResearchPanel (interview → brief →
       DOPPELGANGER_REPORT) + WorldKGMap inside the shared shell;
       country comes from the route param. ISSUE-48: the recipe is
       hard-selected (every run here is a doppelganger run) and the
       panel feeds the live seed overlay to the shared map. -->
  <SurfaceShell
    :cc="cc"
    title="Doppelganger"
    back-to="/"
    leaf-base="/agents/doppelganger"
  >
    <template ***REMOVED***default="{ country, snapshotDate, setSeedOverlay, setTwinsOverlay }">
      <ResearchPanel
        v-if="country"
        :country-name="country.name"
        :country-iso="country.iso_code"
        :snapshot-date="snapshotDate"
        recipe="doppelganger_report"
        :set-seed-overlay="setSeedOverlay"
        :set-twins-overlay="setTwinsOverlay"
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
