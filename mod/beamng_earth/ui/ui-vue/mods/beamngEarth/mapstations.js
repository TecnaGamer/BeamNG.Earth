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
// where its box (EarthMap.vue .marker-label: right of the icon, ~7 px a letter) would overlap a name already shown or
// another station's icon (a 76's pump over "Tesla Supercharger", in game 2026-11-09).
const ICON_R = 11                   // px: half the icon's size (.station-icon)
export function labels(markers, z, w, h) {
  if (z < LABEL_Z) return markers.map(m => ({ ...m, showLabel: false }))
  const hit = (a, b) => a.x0 < b.x1 && b.x0 < a.x1 && a.y0 < b.y1 && b.y0 < a.y1
  const icons = markers.map(m => ({ x0: m.sx - ICON_R, x1: m.sx + ICON_R, y0: m.sy - ICON_R, y1: m.sy + ICON_R }))
  const order = markers.map((m, i) => ({ m, i, d: (m.sx - w / 2) ** 2 + (m.sy - h / 2) ** 2 })).sort((a, b) => a.d - b.d)
  const boxes = [], show = new Set()
  for (const { m, i } of order) {
    const box = { x0: m.sx + ICON_R, x1: m.sx + ICON_R + 10 + 7 * String(m.name || "").length, y0: m.sy - 9, y1: m.sy + 9 }
    if (boxes.some(b => hit(box, b)) || icons.some((b, j) => j !== i && hit(box, b))) continue
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

// The right-click "Nearest station from here": ask(cb) until an answer is not partial or limitMs has passed, then
// done(answer). A newer search (state.gen) cancels an older one's pending retry and drops its answers (Codex on #1056:
// an older retry fired after a newer search started, cancelled its polling and routed to the older point's station).
export function nearSearch(state, ask, done, { limitMs = 20000, retryMs = 500, now = Date.now, later = setTimeout,
  cancel = clearTimeout } = {}) {
  const gen = ++state.gen
  cancel(state.retry)
  state.retry = null
  const started = now()
  const go = () => ask(res => {
    if (gen !== state.gen) return
    if (res && res.partial && now() - started < limitMs) { state.retry = later(go, retryMs); return }
    done(res)
  })
  go()
}
