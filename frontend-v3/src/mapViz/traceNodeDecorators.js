// ── Execution-trace node decorators (extracted from SemanticSearchPanel.vue,
//    2026-10-02) ─────────────────────────────────────────────────────────────
// Each entry of STEP_DECORATORS maps a step type → a partial node (icon,
// title, lines, …). The switch this replaces was a lookup table evaluated by
// branching; now growth is one new key instead of one new case.
//
// Every step type keeps its real fields — nothing is dropped, the layout
// just decides what reads as the one-line insight.
//
// Colors: the heading matches the primary-answer text color ("Found N
// entities within …", rendered with the info emphasis color); each icon is
// colored by its nature (green = resolution, amber = decision, blue =
// ordering/search, cyan = spatial/diffusion, gray = context).
// Warnings/errors flip heading + icon to red.

import { fmtM } from './format'

// ── Shared helpers (pure — no component state) ──────────────────────────────

/** openstreetmap.org link for an OSM entity; null when no id. */
export function osmUrl(e) {
  if (!e || e.osm_id == null) return null
  return `https://www.openstreetmap.org/${e.osm_type || 'node'}/${e.osm_id}`
}

/** OSM Wiki page for a tag value (Tag:key=value); null when empty. */
export function tagSeg(key, value) {
  if (!key || value == null || value === '') return null
  const href = `https://wiki.openstreetmap.org/wiki/Tag:${key}=${encodeURIComponent(value)}`
  return { text: `${key}=${value}`, href }
}

/** Link to a tag VALUE only ("pub" → Tag:amenity=pub), for use after a
 *  plain "amenity" label; null when empty. */
export function tagValueSeg(key, value) {
  if (!key || value == null || value === '') return null
  const href = `https://wiki.openstreetmap.org/wiki/Tag:${key}=${encodeURIComponent(value)}`
  return { text: String(value), href }
}

/** WorldKG depth-1 classes whose UpperCamelCase name is the OSM key
 *  (matches the ontology's KEY_CLASS_MAP). */
const WKGS_KEY_CLASSES = new Set([
  'Amenity', 'Natural', 'Building', 'Highway', 'Railway', 'Leisure',
  'Shop', 'Tourism', 'Historic', 'Waterway', 'Landuse', 'Place',
  'Aeroway', 'Emergency', 'Healthcare', 'Man_made', 'Power',
  'Public_transport',
])

/** Wiki link for a WorldKG class: wkgs:Place → OSM Wiki Key:place
 *  (the OSM key the class is derived from); unknown classes fall back
 *  to the WorldKG class URI. Returns a segment or null. */
export function classSeg(wkgClass) {
  if (!wkgClass || !wkgClass.startsWith('wkgs:')) return null
  const cls = wkgClass.slice(5)
  const key = cls.charAt(0).toLowerCase() + cls.slice(1)
  const href = WKGS_KEY_CLASSES.has(cls)
    ? `https://wiki.openstreetmap.org/wiki/Key:${key}`
    : `http://www.worldkg.org/schema/${cls}`
  return { text: wkgClass, href }
}

/** An executor entity carries an id AND a tags object — anything less is
 *  a bare string/dict, not a resolvable entity. */
export const isEntity = (v) =>
  v && typeof v === 'object' && v.osm_id != null && v.tags

/** Trace line for an OSM entity as segments: the entity NAME, the
 *  WorldKG class, and any place tag each get the green wiki link.
 *  Pass { linked: false } for failed steps (no id/coords) — the
 *  entity renders as a plain diagnostic, never a success link. */
export function entityLine(e, { linked = true } = {}) {
  const segs = []
  const name = e.name || e.tags?.name || 'unnamed'
  const href = linked ? osmUrl(e) : null
  segs.push(href ? { text: name, href } : name)
  if (e.osm_id != null) segs.push(`(${e.osm_type || '?'}/${e.osm_id})`)
  if (e.lat != null && e.lon != null) {
    segs.push(`${Number(e.lat).toFixed(4)}, ${Number(e.lon).toFixed(4)}`)
  }
  if (linked && e.wkg_class) {
    const cls = classSeg(e.wkg_class)
    if (cls) segs.push(cls)
  }
  if (linked && e.tags?.place) {
    const tag = tagSeg('place', e.tags.place)
    if (tag) segs.push(tag)
  }
  return { segments: segs }
}

// ── Per-step-type decorators ────────────────────────────────────────────────
// Signature: (step, ctx) → partial node fields merged over baseNode.
// ctx.resultsByName: lowercase-name → result row (compare_closer name join).

const STEP_DECORATORS = {
  // Anchor resolved → green; entity is a link to OSM. On failure (no usable
  // id/coords) the step shows the error and the entity renders as a plain
  // diagnostic, never a success link.
  geocode(step) {
    const lines = []
    if (isEntity(step.output)) {
      lines.push(entityLine(step.output, { linked: !step.error }))
    }
    return {
      icon: 'bi-geo-alt',
      iconColor: 'text-success',
      title: `Geocode${step.input ? `: ${step.input}` : ''}`,
      lines,
      ...(step.error ? { error: step.error } : {}),
    }
  },

  // Multiple anchors resolved in one step → green links.
  batch_geocode(step) {
    const lines = []
    if (Array.isArray(step.outputs)) {
      step.outputs.forEach((out, i) => {
        if (isEntity(out)) {
          lines.push(entityLine(out, { linked: !step.error }))
        } else if (step.inputs?.[i]) {
          lines.push(step.inputs[i])
        }
      })
    }
    return {
      icon: 'bi-pin-map',
      iconColor: 'text-success',
      title: step.inputs?.length
        ? `Geocode: ${step.inputs.join(', ')}`
        : 'Batch geocode',
      lines,
      ...(step.error ? { error: step.error } : {}),
    }
  },

  // Distance computed between two points → cyan.
  haversine(step) {
    const lines = []
    if (step.output_km != null) {
      lines.push(
        `${step.output_km} km${step.output_m != null ? ` (${step.output_m} m)` : ''}`
      )
    }
    return {
      icon: 'bi-rulers',
      iconColor: 'text-info',
      title: 'Distance (haversine)',
      lines,
    }
  },

  // Directional cone filter → amber compass; amenity links to wiki.
  cone_search(step) {
    const lines = []
    const amenitySeg = tagValueSeg('amenity', step.amenity)
    if (amenitySeg) lines.push({ segments: ['amenity', amenitySeg] })
    if (step.radius_m != null) lines.push(`radius: ${step.radius_m} m`)
    if (step.output_count != null) lines.push(`candidates: ${step.output_count}`)
    return {
      icon: 'bi-compass',
      iconColor: 'text-warning',
      title: step.direction ? `Cone search: ${step.direction}` : 'Cone search',
      lines,
    }
  },

  // Comparison decision → amber; each candidate's name is a green OSM link
  // (resolved entity id comes from the executor; legacy string candidates
  // fall back to a name join with the results).
  compare_closer(step, ctx) {
    let title = step.anchor
      ? `Which is closer to ${step.anchor}?`
      : 'Compared candidates'
    const lines = []
    if (Array.isArray(step.candidates) && step.candidates.length) {
      const rows = step.candidates.map((c) => {
        if (typeof c === 'string') {
          const hit = ctx.resultsByName?.get(String(c).toLowerCase())
          return { text: c, d: hit?.distance_m ?? null, entity: hit }
        }
        return {
          text: c.query || c.name || String(c.osm_id ?? ''),
          d: c.distance_m ?? null,
          entity: c,
        }
      })
      const minD = Math.min(...rows.map((r) => (r.d == null ? Infinity : r.d)))
      for (const r of rows) {
        const segs = []
        if (r.entity && r.entity.osm_id != null) {
          segs.push({ text: r.text, href: osmUrl(r.entity) })
        } else {
          segs.push(r.text)
        }
        if (r.d != null) {
          segs.push(`— ${fmtM(r.d)}${r.d === minD ? ' ✓ closest' : ''}`)
        }
        lines.push({ segments: segs, sep: ' ' })
      }
    } else if (step.output_count != null) {
      title = `Compared ${step.output_count} candidates`
    }
    return {
      icon: 'bi-arrows-angle-contract',
      iconColor: 'text-warning',
      title,
      lines,
    }
  },

  // Supporting context → cyan with a live pulse so the node reads as
  // "context being assembled for the answer".
  entity_context(step) {
    const parts = []
    if (step.uslp_links != null) parts.push(`uslp links ${step.uslp_links}`)
    if (step.communities != null) parts.push(`communities ${step.communities}`)
    if (step.class_distribution != null) parts.push(`classes ${step.class_distribution}`)
    const lines = []
    if (parts.length) lines.push(parts.join(' · '))
    if (Array.isArray(step.sources) && step.sources.length) {
      lines.push(`sources: ${step.sources.join(', ')}`)
    }
    return {
      icon: 'bi-diagram-3',
      iconColor: 'text-info',
      live: true,
      title: 'Entity context',
      lines,
    }
  },

  // Ordering → blue.

  // Ordering → blue.
  rank_by_distance(step) {
    const lines = []
    if (step.anchor) lines.push(`anchor: ${step.anchor}`)
    if (step.top) lines.push(`closest: ${step.top}`)
    return {
      icon: 'bi-sort-numeric-down',
      iconColor: 'text-primary',
      title: 'Ranked by distance',
      lines,
    }
  },

  // Diffusion → cyan; amenity value links to its OSM wiki tag page.
  heat_kernel(step) {
    const segs = []
    const amenitySeg = tagValueSeg('amenity', step.amenity)
    if (amenitySeg) segs.push('amenity', amenitySeg)
    if (step.t != null) segs.push(`t=${step.t}`)
    if (step.total_amenity_nodes != null) segs.push(`${step.total_amenity_nodes} nodes`)
    return {
      icon: 'bi-thermometer-half',
      iconColor: 'text-info',
      title: 'Diffusion (heat kernel)',
      lines: segs.length ? [{ segments: segs }] : [],
    }
  },

  // Enrichment added → green.

  // Enrichment added → green.
  augmented_enrichment(step) {
    return {
      icon: 'bi-arrow-up-right',
      iconColor: 'text-success',
      title: 'Enrichment',
      lines: step.output_count != null ? [`enriched ${step.output_count} entities`] : [],
    }
  },

  place_search(step) {
    return {
      icon: 'bi-search',
      iconColor: 'text-primary',
      title: 'Place search',
      lines: step.warning ? [step.warning] : [],
      ...(step.error ? { error: step.error } : {}),
    }
  },

  // Factor-table retrieval — the heart of the answer. Surface the table,
  // subgraph scope, candidate/result counts and any note (e.g. lenient
  // eigenbasis fallback) instead of a bare label.
  factor_join(step) {
    const lines = []
    if (step.table) lines.push(`table: ${step.table}`)
    if (step.subgraph_slug) lines.push(`subgraph: ${step.subgraph_slug}`)
    if (step.anchor_osm_id != null) lines.push(`anchor: ${step.anchor_osm_id}`)
    if (step.candidates != null) lines.push(`candidates: ${step.candidates}`)
    if (step.output_count != null) lines.push(`results: ${step.output_count}`)
    if (step.note) lines.push(`note: ${step.note}`)
    if (step.detail) lines.push(step.detail)
    if (step.cross_subgraph_transport) {
      const t = step.cross_subgraph_transport
      if (t.transport_matrices_used != null) {
        lines.push(`cross-subgraph transport: ${t.transport_matrices_used} matrices`)
      }
    }
    if (step.warning) lines.push(step.warning)
    return {
      icon: 'bi-table',
      iconColor: 'text-primary',
      title: step.op ? `Factor join: ${step.op.replace(/_/g, ' ')}` : 'Factor join',
      lines,
      ...(step.error ? { error: step.error } : {}),
    }
  },

  // Skipped → amber, flips red via the warning rule.
  spatial_filter_skipped(step) {
    return {
      icon: 'bi-exclamation-triangle',
      iconColor: 'text-warning',
      lines: step.warning ? [step.warning] : [],
      ...(step.error ? { error: step.error } : {}),
    }
  },
}

// Shared decorator bodies — step keys that carry the same node shape.

// Spatial bound / filter → cyan (default_radius + radius_guard).
function radiusNode(step) {
  const lines = []
  if (step.reason) lines.push(step.reason)
  if (step.before != null && step.after != null) {
    lines.push(`${step.before} → ${step.after} entities after radius guard`)
  }
  return {
    icon: 'bi-broadcast',
    iconColor: 'text-info',
    title: `Radius ${step.radius_m ?? '?'} m`,
    lines,
  }
}

// Multiple anchors → blue; category (an amenity value) links to wiki
// (shared across the four multi_anchor_* step keys).
function multiAnchorNode(step, st) {
  const lines = []
  if (step.input) lines.push(step.input)
  const catSeg = tagValueSeg('amenity', step.anchor_category)
  if (catSeg) lines.push({ segments: ['category', catSeg] })
  const resSeg = tagValueSeg('amenity', step.resolved_amenity)
  if (resSeg) lines.push({ segments: ['resolved', resSeg] })
  if (step.anchor_count != null) lines.push(`anchors: ${step.anchor_count}`)
  if (step.radius_m != null) lines.push(`radius: ${step.radius_m} m`)
  if (step.warning) lines.push(step.warning)
  return {
    icon: 'bi-pin-angle',
    iconColor: 'text-primary',
    title: st.replace(/_/g, ' '),
    lines,
  }
}

// Alias registrations — step keys that share a decorator.
STEP_DECORATORS.geocode_anchor = STEP_DECORATORS.geocode
STEP_DECORATORS.default_radius = radiusNode
STEP_DECORATORS.radius_guard = radiusNode
for (const k of [
  'multi_anchor_resolve', 'multi_anchor_search',
  'multi_anchor_anchors', 'multi_anchor_empty',
]) {
  STEP_DECORATORS[k] = (step) => multiAnchorNode(step, k)
}

// Icon chip background follows the icon color (subtle tint).
const ICON_BG = {
  'text-success': 'bg-success-subtle',
  'text-danger': 'bg-danger-subtle',
  'text-warning': 'bg-warning-subtle',
  'text-primary': 'bg-primary-subtle',
  'text-info': 'bg-info-subtle',
}

// Degraded-state keywords — warnings and degraded states (NULL, unverified,
// lenient, pre-migration, fallback) get the amber badge/bar.
const DEGRADED_RE =
  /unverified|lenient|null|pre-migration|fallback|unavailable|degraded|skipped/

/** Decorate one trace step into a flow node: icon + title + insight lines.
 *  ctx.resultsByName — Map(lowercase name → result row) for the
 *  compare_closer candidate name join. */
export function decorateStep(step, ctx = {}) {
  const st = step?.step || 'step'
  const decorator = STEP_DECORATORS[st]
  const node = {
    icon: 'bi-arrow-right',
    title: st.replace(/_/g, ' '),
    lines: [],
    error: null,
    color: 'info-emphasis',
    iconColor: 'text-secondary',
    iconBg: 'bg-secondary-subtle',
    live: false,
    status: null, // 'error' | 'warning' | null — status badge + accent bar
    ...(decorator ? decorator(step, ctx) : defaultLines(step)),
  }

  // Status protocol — errors → red badge/bar; warnings and degraded
  // states → amber badge/bar. The badge catches the eye; the heading keeps
  // its "Found…" color and the icon keeps its nature color.
  const attentionText = [step.warning, step.note, step.detail]
    .filter(Boolean)
    .join(' ')
    .toLowerCase()
  const degraded = DEGRADED_RE.test(attentionText)
  if (node.error || step.error) {
    // step.error must count even when a decorator didn't copy it to
    // node.error (e.g. a failed geocode) — otherwise the error is
    // silently invisible to the status protocol.
    node.status = 'error'
    node.color = 'danger'
    node.iconColor = 'text-danger'
    node.live = false
    node.error = node.error || step.error
  } else if (step.warning || degraded) {
    node.status = 'warning'
    if (step.warning) node.iconColor = 'text-warning'
    node.live = false
  }
  node.iconBg = ICON_BG[node.iconColor] || 'bg-secondary-subtle'
  return node
}

// Generic step: surface every scalar field as key: value — the default
// keeps future step types visible instead of invisible.
function defaultLines(step) {
  const skip = new Set(['step', 'input', 'output'])
  const lines = []
  for (const [k, v] of Object.entries(step)) {
    if (skip.has(k)) continue
    if (typeof v === 'string' || typeof v === 'number') {
      lines.push(`${k}: ${v}`)
    }
  }
  if (isEntity(step.output)) lines.push(entityLine(step.output))
  return { lines }
}
