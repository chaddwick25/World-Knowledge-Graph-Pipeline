/**
 * MapLabelsPanel — Map Labels tab content (v2 Phase 1a mount + Phase 1b data).
 *
 * Loads live labels from the Phase 1b endpoints into mapLabelsStore →
 * WorldKGMap.vue → labelManager.setLayers:
 *
 *   GET /api/nca/class-centroids/  → class labels at class centroids,
 *                                    sized by entity count (low-zoom layer)
 *   GET /api/nca/entities/         → entity tag names for the top classes
 *                                    (high-zoom layer)
 *
 * Phase 1c is implemented: WorldKGMap.vue's layerFilter switches the
 * layers on a zoom threshold, and both TextLayers carry
 * CollisionFilterExtension so overlapping labels are hidden.
 */

<template>
  <div class="d-flex flex-column gap-2">
    <div class="small text-secondary">
      <template v-if="countryName">
        {{ countryName }}<template v-if="snapshotDate"> · <span class="data-mono">{{ snapshotDate }}</span></template>
      </template>
      <template v-else>No country selected</template>
    </div>

    <!-- Status -->
    <div class="d-flex align-items-center gap-2 small">
      <span
        class="badge rounded-pill"
        :class="labelsAttached ? 'text-bg-success' : 'text-bg-secondary'"
      >labels {{ labelsAttached ? 'attached' : 'detached' }}</span>
      <span v-if="hasLabels" class="text-secondary">
        <span class="num">{{ classLabelCount }}</span> classes · <span class="num">{{ entityLabelCount }}</span> entities
      </span>
      <span v-else class="text-secondary">no labels</span>
    </div>

    <!-- Data controls -->
    <div class="d-flex gap-2 flex-wrap">
      <button
        class="btn btn-sm btn-outline-primary"
        :disabled="!countryCode || loadingClasses"
        @click="loadClassLabels"
      >
        {{ loadingClasses ? 'Loading classes…' : 'Load Class Labels' }}
      </button>
      <button
        class="btn btn-sm btn-outline-primary"
        :disabled="!countryCode || loadingEntities"
        @click="loadEntityLabels"
      >
        {{ loadingEntities ? 'Loading entities…' : 'Load Entity Labels' }}
      </button>
      <button
        class="btn btn-sm btn-outline-secondary"
        :disabled="!hasLabels"
        @click="clearLabels"
      >
        Clear Labels
      </button>
    </div>

    <p v-if="errorMessage" class="small text-danger mb-0">{{ errorMessage }}</p>

    <p class="small text-secondary mb-0">
      Class labels cover all WKG classes (amenities <em>and</em> GIS classes
      like Natural, Waterway, Highway) at their geometric centroid, sized by
      count. Entity labels come from <code>entities/</code> for the top
      classes, skipping unnamed features. The map frames the country on load;
      class labels show below zoom 10, entity labels at 10 and above, and
      collision filtering hides overlapping labels.
    </p>
  </div>
</template>

<script>
import { ref, computed } from 'vue'
import axios from 'axios'
import { useMapLabelsStore } from '../stores/mapLabelsStore'
import { labelManager } from '../mapLabels/labelManager'

// Entity labels are fetched for the top-N classes by count (dense at
// country scale without pulling the whole snapshot).
const ENTITY_CLASS_COUNT = 3
const ENTITY_LIMIT = 5000

export default {
  name: 'MapLabelsPanel',
  props: {
    countryName: {
      type: String,
      default: '',
    },
    countryCode: {
      type: String,
      default: '',
    },
    snapshotDate: {
      type: String,
      default: '',
    },
  },
  setup(props) {
    const store = useMapLabelsStore()

    const labelsAttached = ref(labelManager.isAttached())
    const loadingClasses = ref(false)
    const loadingEntities = ref(false)
    const errorMessage = ref('')
    const classLabelCount = computed(() => store.classLabelCount)
    const entityLabelCount = computed(() => store.entityLabelCount)
    const hasLabels = computed(() => store.hasLabels)

    async function loadClassLabels() {
      errorMessage.value = ''
      loadingClasses.value = true
      try {
        const { data } = await axios.get('/nca/class-centroids/', {
          params: {
            country_code: props.countryCode,
            snapshot_date: props.snapshotDate || undefined,
          },
        })
        store.setClassLabels(data.classes || [])
        if (!(data.classes || []).length) {
          errorMessage.value = 'No class data for this country/snapshot.'
        }
        frameLabels()
        labelsAttached.value = labelManager.isAttached()
      } catch (err) {
        errorMessage.value = err.response?.data?.error || err.message
      } finally {
        loadingClasses.value = false
      }
    }

    async function loadEntityLabels() {
      errorMessage.value = ''
      if (!store.classLabels.length) {
        await loadClassLabels()
        if (errorMessage.value) return
      }
      loadingEntities.value = true
      try {
        const topClasses = store.classLabels.slice(0, ENTITY_CLASS_COUNT)
        const entities = []
        // Dedupe by OSM identity — the same physical entity can come back
        // once per snapshot partition when snapshot_date is unset, and
        // identical stacked labels are noise regardless of the source.
        const seen = new Set()
        for (const cls of topClasses) {
          const { data } = await axios.get('/nca/entities/', {
            params: {
              class: cls.wkg_class,
              include_subclasses: 'false',
              limit: ENTITY_LIMIT,
              country_code: props.countryCode,
              snapshot_date: props.snapshotDate || undefined,
            },
          })
          for (const e of data.entities || []) {
            if (!e.geom || e.geom.lat == null || e.geom.lon == null) continue
            // Skip unnamed features — the "node 4552555" fallback spam is
            // noise, not a label.
            const name = e.tags?.name || e.tags?.['name:en'] || ''
            if (!name) continue
            const key = `${e.osm_type}:${e.osm_id}`
            if (seen.has(key)) continue
            seen.add(key)
            entities.push({
              name,
              position: [e.geom.lon, e.geom.lat],
              // Size basis: ontology depth (0=WKGObject … 2=value subclass),
              // deterministic — entities have no search score here.
              score: e.wkg_depth != null ? e.wkg_depth + 10 : 12,
            })
          }
        }
        store.setEntityLabels(entities)
        frameLabels()
        labelsAttached.value = labelManager.isAttached()
      } catch (err) {
        errorMessage.value = err.response?.data?.error || err.message
      } finally {
        loadingEntities.value = false
      }
    }

    /** Move the map to frame the loaded labels (class centroids + entities). */
    function frameLabels() {
      const latlngs = []
      for (const c of store.classLabels) {
        if (c.centroid) latlngs.push([c.centroid[1], c.centroid[0]])
      }
      for (const e of store.entityLabels) {
        if (e.position) latlngs.push([e.position[1], e.position[0]])
      }
      if (latlngs.length) labelManager.fitToBounds(latlngs)
    }

    function clearLabels() {
      store.clear()
    }

    return {
      labelsAttached,
      loadingClasses,
      loadingEntities,
      errorMessage,
      classLabelCount,
      entityLabelCount,
      hasLabels,
      loadClassLabels,
      loadEntityLabels,
      clearLabels,
    }
  },
}
</script>
