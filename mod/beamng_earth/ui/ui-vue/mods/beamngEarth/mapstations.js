// The world map's gas station and EV charger layers (the user, 2026-11-09: "making a new layer on the map that you can
// enable, one for gas stations and one for ev's to make them easier to find"): which stations to draw, their names
// without overlaps, and the radius the map asks the server for (server stationindex.py through finder.lua).
import { TILE } from "./mapmath.js"

export const ICON = { fuel: "⛽", ev: "⚡" }
export const NAMES = { fuel: "Gas station", ev: "EV charger" }     // an unnamed one (finder.lua NAMES)
export const ICON_Z = 12            // icons from this zoom
export const LABEL_Z = 15           // names from this zoom
export const R_KM_MAX = 25          // the server's farthest (stationindex.R_KM_MAX)

// The stations to draw at zoom z with the layers' switches { fuel, ev }.
export function visible(items, { fuel, ev, z }) {
  if (z < ICON_Z) return []
  return (items || []).filter(i => (i.kind === "ev" ? ev : fuel))
}

// The markers ({ sx, sy, name }) with showLabel: from LABEL_Z, the ones nearer the view's centre first, a name left out
// where its box would overlap one already shown.
export function labels(markers, z, w, h) {
  if (z < LABEL_Z) return markers.map(m => ({ ...m, showLabel: false }))
  const order = markers.map((m, i) => ({ m, i, d: (m.sx - w / 2) ** 2 + (m.sy - h / 2) ** 2 })).sort((a, b) => a.d - b.d)
  const boxes = [], show = new Set()
  for (const { m, i } of order) {
    const bw = 10 + 7 * String(m.name || "").length, box = { x0: m.sx - bw / 2, x1: m.sx + bw / 2, y0: m.sy + 6, y1: m.sy + 22 }
    if (boxes.some(b => box.x0 < b.x1 && b.x0 < box.x1 && box.y0 < b.y1 && b.y0 < box.y1)) continue
    boxes.push(box)
    show.add(i)
  }
  return markers.map((m, i) => ({ ...m, showLabel: show.has(i) }))
}

// km from the view's centre to its corner (Web-Mercator metres per pixel at the centre's latitude), at most R_KM_MAX.
export function radiusKm(view, w, h) {
  const mPerPx = (40075016.686 * Math.cos((view.lat * Math.PI) / 180)) / (TILE * Math.pow(2, view.z))
  return Math.min(R_KM_MAX, (Math.hypot(w, h) / 2) * mPerPx / 1000)
}
