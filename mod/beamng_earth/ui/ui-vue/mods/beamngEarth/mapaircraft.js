// The world map's live air traffic layer (lua/ge/extensions/beamngEarth/airtraffic.lua mapItems, through
// settings.uiAirTraffic): each aircraft a plane icon turned to its track, its flight number from LABEL_Z or when
// few are in view; click one to set the GPS to where it is. Pure helpers (tests: mod/tests/test_mapaircraft.mjs).

export const POLL_MS = 3000          // asked this often while the map is open and the layer is on
export const ICON_Z = 6              // icons from this zoom (airliners at cruise are seen from far out)
export const LABEL_Z = 10            // flight numbers from this zoom ...
export const LABEL_FEW = 12          // ... or at any zoom when no more than this many are in view
export const MAX_MARKERS = 300
// a plane seen from above, nose up (rotated to its track by the marker's style), 24 x 24 box round 0, 0
export const PLANE = "M0,-11 L1.6,-8 L1.6,-3 L10,2 L10,4 L1.6,1.5 L1.2,7 L4,9.5 L4,11 L0,10 L-4,11 L-4,9.5 L-1.2,7 L-1.6,1.5 L-10,4 L-10,2 L-1.6,-3 L-1.6,-8 Z"

// The text of a label and of the marker's tooltip.
export function label(a) {
  return String(a.f || a.h || "").slice(0, 12)
}
export function title(a) {
  const parts = [label(a)]
  if (a.t) parts.push(String(a.t).slice(0, 8))
  if (a.g) parts.push("on the ground")
  else if (typeof a.alt === "number") parts.push(a.alt >= 18000 ? "FL" + String(Math.round(a.alt / 100)).padStart(3, "0") : Math.round(a.alt / 100) * 100 + " ft")
  if (typeof a.gs === "number" && !a.g) parts.push(a.gs + " kt")
  if (a.o && a.d) parts.push(String(a.o).slice(0, 4) + " > " + String(a.d).slice(0, 4))
  return parts.join("  ") + " (click: GPS to here)"
}

// The markers to draw at zoom z: { ...item, sx, sy, rot, showLabel, label, title }, those on screen, at most
// MAX_MARKERS (the list comes sorted by hex: the same ones kept as the view moves). project(lat, lon) -> {x, y};
// onScreen({x, y}) -> bool.
export function markers(items, z, project, onScreen) {
  if (z < ICON_Z || !Array.isArray(items)) return []
  const out = []
  for (const a of items) {
    if (typeof a.lat !== "number" || typeof a.lon !== "number") continue
    const s = project(a.lat, a.lon)
    if (!onScreen(s)) continue
    out.push({ ...a, sx: s.x, sy: s.y, rot: ((Number(a.trk) || 0) % 360 + 360) % 360, label: label(a), title: title(a) })
    if (out.length >= MAX_MARKERS) break
  }
  const labels = z >= LABEL_Z || out.length <= LABEL_FEW
  for (const m of out) m.showLabel = labels
  return out
}
