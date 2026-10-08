// ── Shared map-side formatting helpers (2026-10-02) ────────────────────────
// fmtM was triplicated: traceNodeDecorators (trace lines), WorldKGMap
// tooltips (inline ternary), templateDeckLayers (label text). One home here.

/** Meters → human ("1.2 km" / "450 m"); '' when null. */
export function fmtM(m) {
  if (m == null) return ''
  return m >= 1000 ? `${(m / 1000).toFixed(1)} km` : `${Math.round(m)} m`
}
