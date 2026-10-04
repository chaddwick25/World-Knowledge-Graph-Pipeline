// ── Anchor/entity query-graph builders (extracted from SemanticSearchPanel.vue,
//    2026-10-02) ─────────────────────────────────────────────────────────────
// Pure data transforms: backend results + executor trace → the graph payload
// consumed by WorldKGMap. No component state — trace/parsedQuery arrive as args.

/** Normalize backend result shapes (executor: top-level lat/lon; triplet: geom). */
export function normalizeResult(r) {
  const lat = r.geom?.lat ?? r.lat
  const lon = r.geom?.lon ?? r.lon
  return {
    osm_type: r.osm_type,
    osm_id: r.osm_id,
    name: r.name || r.tags?.name || r.tags?.['name:en'] || '',
    tags: r.tags || {},
    wkg_class: r.wkg_class || null,
    geom: lat != null && lon != null ? { lat: Number(lat), lon: Number(lon) } : null,
    scores: r.scores || { final_score: null },
    distance_m: r.distance_m ?? null,
  }
}

/** Anchors = geocoded named locations from the executor trace.
 *  Handles every geocoding step shape: 'geocode' / 'geocode_anchor'
 *  (single output) and 'batch_geocode' (paired inputs/outputs). */
export function extractAnchors(trace) {
  const seen = new Set()
  const anchors = []
  const pushAnchor = (name, out) => {
    if (!out) return
    const lat = out.lat
    const lon = out.lon
    if (lat == null || lon == null) return
    const key = name || 'anchor'
    if (seen.has(key)) return
    seen.add(key)
    anchors.push({ name: key, lat: Number(lat), lon: Number(lon) })
  }
  for (const step of trace || []) {
    if (step?.step === 'geocode' || step?.step === 'geocode_anchor') {
      pushAnchor(step.input, step.output)
    } else if (step?.step === 'batch_geocode') {
      const inputs = Array.isArray(step.inputs) ? step.inputs : []
      const outputs = Array.isArray(step.outputs) ? step.outputs : []
      outputs.forEach((out, i) => pushAnchor(inputs[i] || `anchor ${i + 1}`, out))
    }
  }
  return anchors
}

/** Haversine distance in meters (nearest-anchor link assignment). */
export function haversineM(a, b) {
  const R = 6371000
  const toRad = (d) => (d * Math.PI) / 180
  const dLat = toRad(b.lat - a.lat)
  const dLon = toRad(b.lon - a.lon)
  const s =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(toRad(a.lat)) * Math.cos(toRad(b.lat)) * Math.sin(dLon / 2) ** 2
  return 2 * R * Math.asin(Math.sqrt(s))
}

/** Index of the anchor nearest to an entity (links entities to their anchor set). */
export function nearestAnchorIndex(entity, anchors) {
  let best = 0
  let bestDist = Infinity
  anchors.forEach((a, i) => {
    const d = haversineM(entity.geom, a)
    if (d < bestDist) {
      bestDist = d
      best = i
    }
  })
  return best
}

/** Distance/bearing lines between paired geocoded anchors
 *  (OBJECT-FIELD-MEASURE, bearing 5a): batch_geocode steps with two
 *  coordinate-bearing outputs; the label comes from the haversine step
 *  when present. */
export function extractAnchorLines(trace) {
  let distLabel = null
  for (const step of trace || []) {
    if (step?.step === 'haversine' && step.output_km != null) {
      distLabel = `${step.output_km} km`
    }
  }
  const lines = []
  for (const step of trace || []) {
    if (step?.step !== 'batch_geocode') continue
    const outputs = Array.isArray(step.outputs) ? step.outputs : []
    const inputs = Array.isArray(step.inputs) ? step.inputs : []
    const pts = outputs
      .filter((o) => o && o.lat != null && o.lon != null)
      .slice(0, 2)
    if (pts.length === 2) {
      lines.push({
        from: { lat: Number(pts[0].lat), lon: Number(pts[0].lon) },
        to: { lat: Number(pts[1].lat), lon: Number(pts[1].lon) },
        label: distLabel || `${inputs[0] || 'A'} → ${inputs[1] || 'B'}`,
      })
    }
  }
  return lines
}

/** Build the anchor/entity graph payload consumed by WorldKGMap. */
export function buildQueryGraph({ entities, trace, topK }) {
  const anchors = extractAnchors(trace)
  const links = anchors.length
    ? entities
        .filter((e) => e.geom)
        .map((e) => ({
          anchorIdx: nearestAnchorIndex(e, anchors),
          entityIdx: entities.indexOf(e),
        }))
    : []
  return {
    anchors,
    entities,
    links,
    anchorLines: extractAnchorLines(trace),
    topK,
  }
}

/** Parse optional map metadata from executor trace/results for deck viz.
 *  Returns null outside template mode or when no parsed template exists. */
export function buildTemplateVisualization({ isTemplateMode, parsedQuery, templateQuery, trace, entities, graph }) {
  if (!isTemplateMode || !parsedQuery?.template) return null
  const template = parsedQuery.template
  const steps = Array.isArray(trace) ? trace : []
  const radiusStep = steps.find((s) => s?.step === 'default_radius')
  const coneStep = steps.find((s) => s?.step === 'cone_search')
  const amountConcept = (parsedQuery.concepts || []).find((c) => c?.type === 'AMOUNT')
  const amountText = amountConcept?.text || ''
  const amountMatch = String(amountText).match(/(\d+(?:\.\d+)?)\s*(km|m)\b/i)
  const radiusFromAmount = amountMatch
    ? Math.round(parseFloat(amountMatch[1]) * (amountMatch[2].toLowerCase() === 'km' ? 1000 : 1))
    : null
  const firstDirectionalEntity = entities.find((e) => e.direction)
  const direction = firstDirectionalEntity?.direction || null
  return {
    template,
    question: templateQuery.trim(),
    anchors: graph.anchors || [],
    entities: graph.entities || [],
    links: graph.links || [],
    anchorLines: graph.anchorLines || [],
    radiusM: radiusFromAmount || radiusStep?.radius_m || null,
    coneRadiusM: coneStep?.radius_m || null,
    direction,
  }
}
