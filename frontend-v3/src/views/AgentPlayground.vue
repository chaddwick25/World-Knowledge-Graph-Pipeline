/**
 * AgentPlayground — MVP demo page at /agent.
 *
 * Runs the SAME MCP tools the Goose agent calls (structuredSearch /
 * nameSearch / templateQuery), logs each tool call in the goose format,
 * then renders the result as a map overlay via the shared overlayStore
 * (the renderToolOverlay path) — so overlays drawn here also appear on
 * the main WorldKG map when you navigate back.
 *
 * On mount it auto-runs the startup demo query:
 *   ▸ structuredSearch worldkg-mcp
 *     countryCode: Belize
 *     queryTags: { amenity: cafe }
 *     topK: 1
 *   → Kat's Coffee @ (16.8534109, -88.2800907)
 */

<template>
  <div class="agent-page">
    <header class="agent-header">
      <h1 class="agent-header__title">
        Agent Playground
        <span class="agent-header__badge">MVP</span>
      </h1>
      <p class="agent-header__sub">
        Executes the same MCP tools the Goose agent calls, then draws each result
        on the map via <code>renderToolOverlay</code> → <code>overlayStore</code>.
      </p>
    </header>

    <div class="agent-body">
      <!-- Left: query form + step log -->
      <section class="agent-panel">
        <div class="agent-modes">
          <button
            v-for="m in modes"
            :key="m.key"
            type="button"
            class="agent-mode"
            :class="{ 'agent-mode--active': mode === m.key }"
            @click="mode = m.key"
          >
            {{ m.label }}
          </button>
        </div>

        <form class="agent-form" @submit.prevent="runQuery">
          <template v-if="mode === 'structured'">
            <label class="agent-form__label">Country</label>
            <input v-model="structured.countryCode" class="agent-form__input" placeholder="Belize" />
            <label class="agent-form__label">OSM tags (JSON)</label>
            <textarea
              v-model="structured.queryTags"
              class="agent-form__textarea"
              rows="2"
              placeholder='{"amenity": "cafe"}'
            ></textarea>
          </template>

          <template v-else-if="mode === 'name'">
            <label class="agent-form__label">Country</label>
            <input v-model="nameSearch.countryCode" class="agent-form__input" placeholder="Belize" />
            <label class="agent-form__label">Name (any language / script)</label>
            <textarea
              v-model="nameSearch.naturalQuery"
              class="agent-form__textarea"
              rows="2"
              placeholder="파리바게뜨  /  paris bagueete  /  Kat's Coffee"
            ></textarea>
          </template>

          <template v-else>
            <label class="agent-form__label">Geospatial question</label>
            <textarea
              v-model="templateQuery.question"
              class="agent-form__textarea"
              rows="2"
              placeholder="Which cafes are within 50km of Belize City?"
            ></textarea>
            <label class="agent-form__label">Country (optional)</label>
            <input v-model="templateQuery.countryCode" class="agent-form__input" placeholder="US" />
          </template>

          <div class="agent-form__row">
            <button type="submit" class="agent-form__submit" :disabled="running">
              {{ running ? 'Running…' : 'Run as agent' }}
            </button>
            <button
              type="button"
              class="agent-form__demo"
              :disabled="running"
              @click="runDemo"
            >
              Demo: Belize cafes
            </button>
          </div>
        </form>

        <div class="agent-log">
          <h2 class="agent-log__title">Agent steps</h2>
          <div v-if="steps.length === 0" class="agent-log__empty">
            Run a query to see the agent's tool calls.
          </div>
          <ol class="agent-log__list">
            <li v-for="(step, i) in steps" :key="i" class="agent-step">
              <div class="agent-step__tool">▸ {{ step.tool }} worldkg-mcp</div>
              <pre class="agent-step__params">{{ step.paramsText }}</pre>
              <div v-if="step.error" class="agent-step__error">✗ {{ step.error }}</div>
              <div v-else-if="step.summary" class="agent-step__result">{{ step.summary }}</div>
            </li>
          </ol>
        </div>
      </section>

      <!-- Right: map -->
      <section class="agent-map">
        <div ref="mapEl" class="agent-map__canvas"></div>
        <div class="agent-map__legend">
          <span v-for="(c, tool) in TOOL_COLORS" :key="tool" class="agent-map__legend-item">
            <i class="agent-map__swatch" :style="{ background: c }"></i>{{ tool }}
          </span>
        </div>
      </section>
    </div>
  </div>
</template>

<script>
import axios from 'axios'
import L from 'leaflet'
import 'leaflet/dist/leaflet.css'
import { useOverlayStore } from '../stores/overlayStore'

// Same tool colors as WorldKGMap.vue — overlays stay consistent across pages.
const TOOL_COLORS = {
  structuredSearch: '#3b82f6',  // blue
  nameSearch: '#ef4444',        // red
  templateQuery: '#10b981',     // green
}

export default {
  name: 'AgentPlayground',
  data() {
    return {
      TOOL_COLORS,
      modes: [
        { key: 'structured', label: 'Structured (JSON)' },
        { key: 'name', label: 'Name search' },
        { key: 'template', label: "Kuhn's Template" },
      ],
      mode: 'structured',
      running: false,
      steps: [],
      structured: { countryCode: 'Belize', queryTags: '{"amenity": "cafe"}' },
      nameSearch: { countryCode: 'Belize', naturalQuery: "Kat's Coffee" },
      templateQuery: {
        question: 'Which cafes are within 50km of Belize City?',
        countryCode: 'Belize',
      },
      overlayStore: null,
      overlayUnsub: null,
      map: null,
      overlayLayer: null,
    }
  },
  mounted() {
    this.overlayStore = useOverlayStore()
    this.initMap()
    this.renderOverlays()
    this.overlayUnsub = this.overlayStore.$subscribe(() => this.renderOverlays())
    // Visualize the startup results immediately.
    this.runDemo()
  },
  beforeUnmount() {
    if (this.overlayUnsub) {
      this.overlayUnsub()
      this.overlayUnsub = null
    }
    if (this.map) {
      this.map.remove()
      this.map = null
      this.overlayLayer = null
    }
  },
  methods: {
    initMap() {
      this.map = L.map(this.$refs.mapEl, { zoomControl: true }).setView([17.0, -88.5], 8)
      L.tileLayer(
        'https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',
        {
          attribution:
            '© <a href="https://www.openstreetmap.org/copyright">OSM</a> · <a href="https://carto.com/">CARTO</a>',
          subdomains: 'abcd',
          maxZoom: 19,
        },
      ).addTo(this.map)
      this.overlayLayer = L.layerGroup().addTo(this.map)
      setTimeout(() => this.map.invalidateSize(), 100)
    },

    // ── Agent execution (same payloads as frontend-v3/src/mcp/server-tools.ts) ──

    async runQuery() {
      if (this.running) return
      this.running = true
      const tool =
        this.mode === 'structured'
          ? 'structuredSearch'
          : this.mode === 'name'
            ? 'nameSearch'
            : 'templateQuery'
      try {
        if (this.mode === 'structured') {
          await this.runStructuredSearch({
            countryCode: this.structured.countryCode,
            queryTags: JSON.parse(this.structured.queryTags || '{}'),
            topK: 20,
          })
        } else if (this.mode === 'name') {
          await this.runNameSearch()
        } else {
          await this.runTemplateQuery()
        }
      } catch (err) {
        this.logStep(tool, {}, '', err.response?.data?.error || err.message)
      } finally {
        this.running = false
      }
    },

    async runDemo() {
      if (this.running) return
      this.running = true
      try {
        await this.runStructuredSearch({
          countryCode: 'Belize',
          queryTags: { amenity: 'cafe' },
          topK: 1,
        })
      } catch (err) {
        this.logStep(
          'structuredSearch',
          { countryCode: 'Belize', queryTags: { amenity: 'cafe' }, topK: 1 },
          '',
          err.response?.data?.error || err.message,
        )
      } finally {
        this.running = false
      }
    },

    async runStructuredSearch(params) {
      const payload = {
        country_code: params.countryCode,
        query_tags: params.queryTags,
        top_k: params.topK || 20,
      }
      const resp = await axios.post('/nca/semantic-triplet-search/', payload)
      const data = resp.data
      const top = (data.results || [])[0]
      const summary = top
        ? `Top result: ${top.tags?.name || `${top.osm_type}/${top.osm_id}`} @ ` +
          `(${top.geom.lat.toFixed(4)}, ${top.geom.lon.toFixed(4)}) — ${data.count} total`
        : `No results (${data.count})`
      this.logStep(
        'structuredSearch',
        { countryCode: params.countryCode, queryTags: params.queryTags, topK: params.topK || 20 },
        summary,
      )
      this.pushOverlay('structuredSearch', data)
      return data
    },

    async runNameSearch() {
      const payload = {
        country_code: this.nameSearch.countryCode,
        natural_query: this.nameSearch.naturalQuery,
        top_k: 20,
      }
      const resp = await axios.post('/nca/semantic-triplet-search/', payload)
      const data = resp.data
      const top = (data.results || [])[0]
      const summary = top
        ? `Top result: ${top.tags?.name || `${top.osm_type}/${top.osm_id}`} @ ` +
          `(${top.geom.lat.toFixed(4)}, ${top.geom.lon.toFixed(4)}) — ${data.count} total`
        : `No results (${data.count})`
      this.logStep(
        'nameSearch',
        { countryCode: this.nameSearch.countryCode, naturalQuery: this.nameSearch.naturalQuery, topK: 20 },
        summary,
      )
      this.pushOverlay('nameSearch', data)
      return data
    },

    async runTemplateQuery() {
      const payload = { query: this.templateQuery.question }
      if (this.templateQuery.countryCode) payload.country_code = this.templateQuery.countryCode
      const resp = await axios.post('/nca/execute-query/', payload)
      const data = resp.data
      const parsed = data.parsed || {}
      const result = data.result || {}
      const enrichment = result.enrichment
      let summary =
        `template=${parsed.template} · ${result.answer || result.error || 'no answer'}`
      if (enrichment && enrichment.actions && enrichment.actions.length) {
        summary += ` · ✦ enriched via ${enrichment.actions.join(', ')}`
      }
      this.logStep(
        'templateQuery',
        { query: this.templateQuery.question, countryCode: this.templateQuery.countryCode || undefined },
        summary,
      )
      this.pushOverlay('templateQuery', result)
      return data
    },

    logStep(tool, params, summary, error) {
      this.steps.push({
        tool,
        paramsText: JSON.stringify(params, null, 2),
        summary: summary || '',
        error,
      })
    },

    pushOverlay(tool, payload) {
      this.overlayStore.renderOverlay({ tool, kind: 'markers', result: payload })
    },

    // ── Map overlay rendering (same overlay spec as WorldKGMap.vue) ──

    renderOverlays() {
      if (!this.map || !this.overlayLayer || !this.overlayStore) return
      this.overlayLayer.clearLayers()
      const bounds = []
      for (const overlay of this.overlayStore.overlays) {
        const color = overlay.color || TOOL_COLORS[overlay.tool] || '#6366f1'
        const entities = this.normalizeEntities(overlay.result || overlay.entities || [])
        for (const e of entities) {
          const marker = L.circleMarker([e.lat, e.lon], {
            radius: 7,
            color,
            fillColor: color,
            fillOpacity: 0.8,
            weight: 2,
          })
          const popup = [`<strong>${e.name || 'entity'}</strong>`]
          if (e.wkgClass) popup.push(`<span>${e.wkgClass}</span>`)
          if (e.score != null) popup.push(`<span>score: ${Number(e.score).toFixed(3)}</span>`)
          if (e.distanceM != null) popup.push(`<span>${Math.round(e.distanceM)} m</span>`)
          marker.bindPopup(popup.join('<br/>'))
          marker.addTo(this.overlayLayer)
          bounds.push([e.lat, e.lon])
        }
        if (overlay.anchor && overlay.radius != null) {
          L.circle([overlay.anchor.lat, overlay.anchor.lon], {
            radius: overlay.radius,
            color,
            fillColor: color,
            fillOpacity: 0.12,
            weight: 2,
          }).addTo(this.overlayLayer)
          bounds.push([overlay.anchor.lat, overlay.anchor.lon])
        }
      }
      if (bounds.length) {
        this.map.fitBounds(bounds, { padding: [60, 60] })
      }
    },

    normalizeEntities(raw) {
      const source = Array.isArray(raw)
        ? raw
        : raw.results || (raw.result && raw.result.results) || []
      return source
        .map((e) => ({
          lat: e.lat != null ? e.lat : e.geom && e.geom.lat,
          lon: e.lon != null ? e.lon : e.geom && e.geom.lon,
          name:
            e.name ||
            (e.tags && e.tags.name) ||
            `${e.osm_type || 'osm'} ${e.osm_id || ''}`.trim(),
          wkgClass: e.wkg_class || e.wkgClass || null,
          score:
            e.score ??
            (e.scores && e.scores.final_score) ??
            e.diffusion_score ??
            null,
          distanceM: e.distance_m != null ? e.distance_m : e.distanceM,
        }))
        .filter((e) => e.lat != null && e.lon != null)
    },
  },
}
</script>

<style scoped>
.agent-page {
  height: 100vh;
  display: flex;
  flex-direction: column;
  background: #0f172a;
  color: #e2e8f0;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
}

.agent-header {
  padding: 0.9rem 1.25rem;
  border-bottom: 1px solid #1e293b;
}

.agent-header__title {
  margin: 0;
  font-size: 1.15rem;
  font-weight: 700;
  color: #f1f5f9;
}

.agent-header__badge {
  display: inline-block;
  margin-left: 0.5rem;
  padding: 0.1rem 0.45rem;
  border-radius: 0.4rem;
  background: #4f46e5;
  color: #e0e7ff;
  font-size: 0.65rem;
  font-weight: 700;
  vertical-align: middle;
  letter-spacing: 0.04em;
}

.agent-header__sub {
  margin: 0.3rem 0 0;
  font-size: 0.8rem;
  color: #94a3b8;
}

.agent-header__sub code {
  background: #1e293b;
  padding: 0.05rem 0.3rem;
  border-radius: 0.3rem;
  font-size: 0.75rem;
}

.agent-body {
  flex: 1;
  display: flex;
  min-height: 0;
}

.agent-panel {
  width: 420px;
  min-width: 360px;
  display: flex;
  flex-direction: column;
  border-right: 1px solid #1e293b;
  overflow-y: auto;
}

.agent-modes {
  display: flex;
  gap: 0.35rem;
  padding: 0.75rem 1rem 0;
}

.agent-mode {
  flex: 1;
  padding: 0.4rem 0.4rem;
  border: 1px solid #1e293b;
  border-radius: 0.45rem;
  background: #111c33;
  color: #94a3b8;
  font-size: 0.72rem;
  font-weight: 600;
  cursor: pointer;
}

.agent-mode--active {
  background: #312e81;
  border-color: #4f46e5;
  color: #e0e7ff;
}

.agent-form {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
  padding: 0.85rem 1rem;
  border-bottom: 1px solid #1e293b;
}

.agent-form__label {
  font-size: 0.68rem;
  font-weight: 600;
  color: #64748b;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.agent-form__input,
.agent-form__textarea {
  width: 100%;
  box-sizing: border-box;
  padding: 0.45rem 0.6rem;
  border: 1px solid #1e293b;
  border-radius: 0.45rem;
  background: #0b1424;
  color: #e2e8f0;
  font-size: 0.8rem;
  font-family: inherit;
}

.agent-form__input::placeholder,
.agent-form__textarea::placeholder {
  color: #475569;
}

.agent-form__row {
  display: flex;
  gap: 0.5rem;
  margin-top: 0.35rem;
}

.agent-form__submit {
  flex: 1;
  padding: 0.5rem;
  border: none;
  border-radius: 0.45rem;
  background: #4f46e5;
  color: #e0e7ff;
  font-size: 0.8rem;
  font-weight: 700;
  cursor: pointer;
}

.agent-form__submit:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.agent-form__demo {
  padding: 0.5rem 0.8rem;
  border: 1px solid #334155;
  border-radius: 0.45rem;
  background: #111c33;
  color: #94a3b8;
  font-size: 0.75rem;
  font-weight: 600;
  cursor: pointer;
}

.agent-form__demo:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

/* ── Step log ── */

.agent-log {
  flex: 1;
  padding: 0.85rem 1rem;
  overflow-y: auto;
}

.agent-log__title {
  margin: 0 0 0.6rem;
  font-size: 0.75rem;
  font-weight: 700;
  color: #94a3b8;
  text-transform: uppercase;
  letter-spacing: 0.06em;
}

.agent-log__empty {
  font-size: 0.8rem;
  color: #475569;
}

.agent-log__list {
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 0.7rem;
}

.agent-step {
  border: 1px solid #1e293b;
  border-radius: 0.5rem;
  padding: 0.6rem 0.75rem;
  background: #0b1424;
}

.agent-step__tool {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 0.78rem;
  font-weight: 700;
  color: #a5b4fc;
}

.agent-step__params {
  margin: 0.4rem 0 0;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 0.7rem;
  color: #64748b;
  white-space: pre-wrap;
  word-break: break-word;
}

.agent-step__result {
  margin-top: 0.4rem;
  font-size: 0.78rem;
  color: #86efac;
}

.agent-step__error {
  margin-top: 0.4rem;
  font-size: 0.78rem;
  color: #fca5a5;
}

/* ── Map ── */

.agent-map {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
  position: relative;
}

.agent-map__canvas {
  flex: 1;
  min-height: 0;
}

.agent-map__legend {
  position: absolute;
  bottom: 0.75rem;
  right: 0.75rem;
  z-index: 1000;
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
  padding: 0.5rem 0.7rem;
  border-radius: 0.5rem;
  background: rgba(2, 6, 23, 0.85);
  border: 1px solid #1e293b;
  font-size: 0.68rem;
  color: #94a3b8;
}

.agent-map__legend-item {
  display: flex;
  align-items: center;
  gap: 0.4rem;
}

.agent-map__swatch {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  display: inline-block;
}
</style>
