/**
 * useMapOverlayLayers — WorldKGMap overlay layer controllers.
 *
 * Owns the four overlay layer groups (query graph, agent overlay, augmented
 * accepted/rejected links) and their render functions. Extracted from
 * WorldKGMap.vue (monolith split, Phase 5): the layers were module-level
 * singletons there — this composable scopes them per map instance.
 *
 * Usage (Options API):
 *   created()  { this.overlayLayers = useMapOverlayLayers() }
 *   initMap()  { this.overlayLayers.attach(mapInstance) }
 *   unmounted(){ this.overlayLayers.detach() }
 */

import L from 'leaflet'

// ── Relation type color map ──────────────────────────────────────────────
// Harmonious palette (Tailwind 400-500 range) that complements the app's
// dark theme (#0f172a backgrounds, #6366f1 indigo accent).
// Accepted links use solid lines at full opacity; rejected links use
// dashed lines at reduced opacity — both in the relation's color.
export const RELATION_COLORS = {
  addrCity:        '#8b5cf6',  // violet-500
  addrPlace:       '#10b981',  // emerald-500
  addrNeighbour:   '#06b6d4',  // cyan-500
  addrSuburb:      '#84cc16',  // lime-500
  addrDistrict:    '#3b82f6',  // blue-500
  addrState:       '#ef4444',  // red-500
  addrProvince:    '#ec4899',  // pink-500
  addrHamlet:      '#f59e0b',  // amber-500
}
export const DEFAULT_RELATION_COLOR = '#6b7280'  // gray-500 for unknown relations

export function getRelationColor(relation) {
  return RELATION_COLORS[relation] || DEFAULT_RELATION_COLOR
}

// ── Agent overlay tool colors (docs/plans/MCP_AGENT_MVP_PLAN.md §9.2) ──
const TOOL_COLORS = {
  structuredSearch: '#3b82f6',  // blue
  nameSearch: '#ef4444',        // red
  templateQuery: '#10b981',     // green
}

// ── Query-graph (anchor/entity) markers ─────────────────────────────────
// Bootstrap Icons in a colored circular badge. Inline styles because
// Leaflet-injected divIcon HTML is outside the component's scoped DOM.
function makeQueryIcon({ bg, color, iconClass, size }) {
  const s = size || 26
  return L.divIcon({
    className: 'worldkg-query-icon',
    html:
      `<div style="width:${s}px;height:${s}px;border-radius:50%;background:${bg};` +
      `display:flex;align-items:center;justify-content:center;border:2px solid #fff;` +
      `box-shadow:0 1px 5px rgba(0,0,0,.5);color:${color};font-size:${Math.round(s * 0.6)}px">` +
      `<i class="bi ${iconClass}"></i></div>`,
    iconSize: [s, s],
    iconAnchor: [s / 2, s / 2],
  })
}
const ANCHOR_ICON = makeQueryIcon({ bg: '#6366f1', color: '#fff', iconClass: 'bi-geo-alt-fill', size: 30 })
const ENTITY_ICON = makeQueryIcon({ bg: '#f97316', color: '#fff', iconClass: 'bi-building', size: 24 })

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;')
}

// Accept either a raw data-tool result or an explicit entities list.
function normalizeOverlayEntities(overlay) {
  const raw = overlay.result || overlay.entities || []
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
}

export function useMapOverlayLayers() {
  let mapInstance = null
  let queryGraphLayer = null
  let agentOverlayLayer = null
  let augmentedAcceptedLayer = null
  let augmentedRejectedLayer = null

  function attach(map) {
    mapInstance = map
    queryGraphLayer = L.layerGroup().addTo(map)
    agentOverlayLayer = L.layerGroup().addTo(map)
    augmentedAcceptedLayer = L.layerGroup().addTo(map)
    augmentedRejectedLayer = L.layerGroup().addTo(map)
  }

  function detach() {
    mapInstance = null
    queryGraphLayer = null
    agentOverlayLayer = null
    augmentedAcceptedLayer = null
    augmentedRejectedLayer = null
  }

  // ── Anchor/entity graph rendering ──────────────────────────────────
  // Anchors (named query locations, indigo pins) + entities (top-k
  // results, orange building icons) + dashed links entity→nearest anchor.
  function renderQueryGraph(graph) {
    if (!mapInstance || !queryGraphLayer) return
    queryGraphLayer.clearLayers()

    if (!graph) return

    const bounds = []

    for (const a of graph.anchors || []) {
        if (a.lat == null || a.lon == null) continue
        const marker = L.marker([a.lat, a.lon], { icon: ANCHOR_ICON })
        marker.bindPopup(
          `<strong>${escapeHtml(a.name || 'Anchor')}</strong><br/>` +
          `<span class="text-secondary">anchor</span><br/>` +
          `${Number(a.lat).toFixed(4)}, ${Number(a.lon).toFixed(4)}`
        )
        marker.addTo(queryGraphLayer)
        bounds.push([a.lat, a.lon])
      }

    for (const e of graph.entities || []) {
        if (!e.geom || e.geom.lat == null) continue
        const marker = L.marker([e.geom.lat, e.geom.lon], { icon: ENTITY_ICON })
        const popup = [
          `<strong>${escapeHtml(e.tags?.name || e.name || `${e.osm_type} ${e.osm_id}`)}</strong>`,
        ]
        if (e.wkg_class) popup.push(`<span>${escapeHtml(e.wkg_class)}</span>`)
        if (e.scores?.final_score != null) {
          popup.push(`<span>score: ${Number(e.scores.final_score).toFixed(3)}</span>`)
        }
        if (e.distance_m != null) {
          popup.push(`<span>${Math.round(e.distance_m)} m from anchor</span>`)
        }
        if (e.osm_type && e.osm_id) {
          popup.push(
            `<a href="https://www.openstreetmap.org/${e.osm_type}/${e.osm_id}" target="_blank">` +
            `OSM ${e.osm_type}/${e.osm_id}</a>`
          )
        }
        marker.bindPopup(popup.join('<br/>'))
        marker.addTo(queryGraphLayer)
        bounds.push([e.geom.lat, e.geom.lon])
      }

    for (const link of graph.links || []) {
        const anchor = graph.anchors?.[link.anchorIdx]
        const entity = graph.entities?.[link.entityIdx]
        if (!anchor || anchor.lat == null || !entity?.geom) continue
        L.polyline(
          [[anchor.lat, anchor.lon], [entity.geom.lat, entity.geom.lon]],
          {
            color: '#a5b4fc',
            weight: 1.5,
            opacity: 0.55,
            dashArray: '4 4',
          }
        ).addTo(queryGraphLayer)
      }

    // Distance/bearing lines between two geocoded anchors
    // (OBJECT-FIELD-MEASURE distance, bearing): solid indigo line with a
    // mid-point distance label.
    for (const line of graph.anchorLines || []) {
      const a = line.from
      const b = line.to
      if (!a || !b || a.lat == null || b.lat == null) continue
      const latlngs = [[a.lat, a.lon], [b.lat, b.lon]]
      L.polyline(latlngs, {
        color: '#818cf8',
        weight: 2,
        opacity: 0.85,
      }).addTo(queryGraphLayer)
      if (line.label) {
        const mid = L.latLngBounds(latlngs).getCenter()
        L.marker(mid, {
          interactive: false,
          icon: L.divIcon({
            className: 'trace-dist-label',
            html:
              `<span style="background: rgba(15,23,42,.9); color:#c7d2fe; ` +
              `font-size:11px; padding:1px 6px; border-radius:4px; ` +
              `border:1px solid #6366f1; white-space:nowrap;">` +
              `${escapeHtml(line.label)}</span>`,
            iconSize: [0, 0],
          }),
        }).addTo(queryGraphLayer)
      }
      bounds.push([a.lat, a.lon], [b.lat, b.lon])
    }

    if (bounds.length) {
      mapInstance.fitBounds(bounds, { padding: [60, 60], maxZoom: 14 })
    }
  }

  // ── Agent overlay rendering (MCP renderToolOverlay → overlayStore) ──
  function renderAgentOverlays(overlayStore) {
    if (!mapInstance || !agentOverlayLayer || !overlayStore) return
    agentOverlayLayer.clearLayers()

    const bounds = []
    for (const overlay of overlayStore.overlays) {
      const color = overlay.color || TOOL_COLORS[overlay.tool] || '#6366f1'
      const kind = overlay.kind || 'markers'
      const entities = normalizeOverlayEntities(overlay)
      const anchor = overlay.anchor || null

      for (const e of entities) {
        let marker
        if (kind === 'scaled-markers') {
          const score = e.score == null ? 0.5 : Math.min(Math.max(e.score, 0), 1)
          marker = L.circleMarker([e.lat, e.lon], {
            radius: 4 + Math.round(score * 12),
            color,
            fillColor: color,
            fillOpacity: 0.7,
            weight: 1.5,
          })
        } else {
          marker = L.circleMarker([e.lat, e.lon], {
            radius: 6,
            color,
            fillColor: color,
            fillOpacity: 0.75,
            weight: 2,
          })
        }
        const popup = [`<strong>${e.name || 'entity'}</strong>`]
        if (e.wkgClass) popup.push(`<span>${e.wkgClass}</span>`)
        if (e.score != null) popup.push(`<span>score: ${Number(e.score).toFixed(3)}</span>`)
        if (e.distanceM != null) popup.push(`<span>${Math.round(e.distanceM)} m</span>`)
        marker.bindPopup(popup.join('<br/>'))
        marker.addTo(agentOverlayLayer)
        bounds.push([e.lat, e.lon])
      }

      if (kind === 'radius' && anchor && overlay.radius != null) {
        L.circle([anchor.lat, anchor.lon], {
          radius: overlay.radius,
          color,
          fillColor: color,
          fillOpacity: 0.12,
          weight: 2,
        }).addTo(agentOverlayLayer)
        bounds.push([anchor.lat, anchor.lon])
      }

      if (kind === 'markers-line' && anchor) {
        for (const e of entities) {
          L.polyline(
            [
              [anchor.lat, anchor.lon],
              [e.lat, e.lon],
            ],
            { color, weight: 2, opacity: 0.7 },
          ).addTo(agentOverlayLayer)
        }
        bounds.push([anchor.lat, anchor.lon])
      }
    }

    if (bounds.length) {
      mapInstance.fitBounds(bounds, { padding: [80, 80] })
    }
  }

  function renderAugmentedLinks({
    augmentedLinks,
    showAcceptedLinks,
    showRejectedLinks,
    visibleRelations,
    hasSearchResults,
  }) {
    if (!mapInstance) return
    if (augmentedAcceptedLayer) augmentedAcceptedLayer.clearLayers()
    if (augmentedRejectedLayer) augmentedRejectedLayer.clearLayers()

    if (!augmentedLinks) return

    const bounds = []
    const visibleSet = visibleRelations ? new Set(visibleRelations) : null
    const isVisible = (relation) => !visibleSet || visibleSet.has(relation)

    // Render accepted links — solid lines, filled markers, relation color
    if (showAcceptedLinks && augmentedLinks.accepted?.length) {
      augmentedLinks.accepted.forEach((link) => {
        if (!isVisible(link.relation)) return
        const hLat = link.head?.lat
        const hLon = link.head?.lon
        const tLat = link.tail?.lat
        const tLon = link.tail?.lon
        if (hLat == null || hLon == null || tLat == null || tLon == null) return

        const color = getRelationColor(link.relation)

        // Solid line connecting head → tail
        const line = L.polyline(
          [[hLat, hLon], [tLat, tLon]],
          {
            color: color,
            weight: 1.5,
            opacity: 0.6,
          }
        )
        line.bindPopup(
          `<strong>Accepted Link</strong><br>` +
          `Relation: ${link.relation}<br>` +
          `Score: ${(link.normalized_score * 100).toFixed(1)}%<br>` +
          `Head: ${link.head.osm_type}/${link.head.osm_id}<br>` +
          `Tail: ${link.tail.osm_type}/${link.tail.osm_id}`
        )
        line.addTo(augmentedAcceptedLayer)

        // Head marker — filled
        const headMarker = L.circleMarker([hLat, hLon], {
          radius: 4,
          color: color,
          fillColor: color,
          fillOpacity: 0.85,
          weight: 1.5,
        })
        headMarker.addTo(augmentedAcceptedLayer)
        bounds.push([hLat, hLon])

        // Tail marker — filled (lighter)
        const tailMarker = L.circleMarker([tLat, tLon], {
          radius: 4,
          color: color,
          fillColor: color,
          fillOpacity: 0.5,
          weight: 1.5,
        })
        tailMarker.addTo(augmentedAcceptedLayer)
        bounds.push([tLat, tLon])
      })
    }

    // Render rejected links — dashed lines, hollow markers, relation color
    if (showRejectedLinks && augmentedLinks.rejected?.length) {
      augmentedLinks.rejected.forEach((link) => {
        if (!isVisible(link.relation)) return
        const hLat = link.head?.lat
        const hLon = link.head?.lon
        const tLat = link.tail?.lat
        const tLon = link.tail?.lon
        if (hLat == null || hLon == null || tLat == null || tLon == null) return

        const color = getRelationColor(link.relation)

        // Dashed line connecting head → tail
        const line = L.polyline(
          [[hLat, hLon], [tLat, tLon]],
          {
            color: color,
            weight: 1.5,
            opacity: 0.4,
            dashArray: '4 3',
          }
        )
        line.bindPopup(
          `<strong>Rejected Link</strong><br>` +
          `Relation: ${link.relation}<br>` +
          `Score: ${(link.normalized_score * 100).toFixed(1)}%<br>` +
          `Head: ${link.head.osm_type}/${link.head.osm_id}<br>` +
          `Tail: ${link.tail.osm_type}/${link.tail.osm_id}`
        )
        line.addTo(augmentedRejectedLayer)

        // Head marker — hollow (outlined only)
        const headMarker = L.circleMarker([hLat, hLon], {
          radius: 4,
          color: color,
          fillColor: 'transparent',
          fillOpacity: 0,
          weight: 1.5,
        })
        headMarker.addTo(augmentedRejectedLayer)
        bounds.push([hLat, hLon])

        // Tail marker — hollow
        const tailMarker = L.circleMarker([tLat, tLon], {
          radius: 4,
          color: color,
          fillColor: 'transparent',
          fillOpacity: 0,
          weight: 1.5,
        })
        tailMarker.addTo(augmentedRejectedLayer)
        bounds.push([tLat, tLon])
      })
    }

    // Fit bounds to augmented links if we're showing them and no search results
    if (bounds.length && !hasSearchResults) {
      mapInstance.fitBounds(bounds, { padding: [80, 80] })
    }
  }

  return {
    attach,
    detach,
    renderQueryGraph,
    renderAgentOverlays,
    renderAugmentedLinks,
  }
}
