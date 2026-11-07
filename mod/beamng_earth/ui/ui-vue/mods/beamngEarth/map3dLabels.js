// The 3D map's labels, drawn as map apps draw them (the user, 2026-11-07: "handle labels similar how modern map apps
// like Apple Maps dose it. As you zoom in more and more info appears and try to make it as clean as this").
// map3d.lua hands over the labels round the view (world positions, the zoom each starts and stops at, a rank: lower
// first; server maplabels.py) and the camera every frame. Each frame here: every label the zoom allows is projected
// to the screen; the most important are placed first and a label that would overlap one already placed is left out
// (the ones shown last frame a little ahead, so they do not flicker); shown and hidden ones fade. Street names lie
// along their road, places carry a coloured icon, towns a dot, regions and districts are spaced capitals.

const FADE_Z = 0.45            // zoom levels over which a label fades in after its min zoom (and out past its max)
const KEEP_BONUS = 4           // rank bonus of a label shown last frame (no flicker as the view moves)
const LONGER_Z = 1.5           // regions, countries, water and ranges stay this many levels past their max zoom
const LONGER = new Set(["state", "country", "water", "land", "continent"])
const SAME_TEXT_PX = 520       // a street's name again only this far from where it is already written
const STREETS_MAX = 28         // street names on the screen at most (map apps write the main ones)
const POIS_MAX = 16            // places on the screen at most, and ...
const POI_GAP_PX = 150         // ... this far apart (1 mi up showed ~60, a wall of names; map apps show a handful)
const PAD = 3                  // px kept free round each label
const RESERVED = [             // the screen's own controls (px from the edges): no label under them
  { top: 0, height: 86, left: 0, right: 0 },
  { bottom: 0, height: 44, left: 0, right: 0 },
]

// The screen draws a frame or two after the game (the UI is its own process, composited later): the labels trailed
// the ground while the view moved (the user, 2026-11-07: "the lables seems to have a delay as i move the camera
// around"). The camera is carried on by its own motion over the last two frames, LEAD_MS ahead.
const LEAD_MS = 40
const MAX_EXTRAP = 2.5         // at most this many frame steps ahead (a stop: no overshoot past it)

const POI_COLOURS = { food: "#ff9f43", shop: "#f5c518", lodging: "#b48cff", culture: "#ff7eb6", park: "#4cd97b",
  education: "#d9a066", health: "#ff6b6b", transit: "#4aa8ff", fuel: "#7fb3ff" }
// (white glyphs on the coloured disc, 12 x 12 viewBox)
const POI_ICONS = {
  food: '<path d="M3 1v4a1.5 1.5 0 0 0 1 1.4V11h1V6.4A1.5 1.5 0 0 0 6 5V1H5v3H4.5V1h-1v3H3V1zM8.5 1C7.7 1 7 2.3 7 4v2.5h1V11h1V1z"/>',
  shop: '<path d="M3 4h6l.6 7H2.4zM4.3 4V3a1.7 1.7 0 0 1 3.4 0v1h-.9V3a.8.8 0 0 0-1.6 0v1z"/>',
  lodging: '<path d="M1 3h1v4h8a1 1 0 0 1 1 1v3h-1V9.5H2V11H1zM3 5.2a1 1 0 1 1 2 0 1 1 0 0 1-2 0zM5.5 4.5h3.5a1 1 0 0 1 1 1V7H5.5z"/>',
  culture: '<path d="M6 1l5 2.5v1H1v-1zM2 5h1.4v4.5H2zM5.3 5h1.4v4.5H5.3zM8.6 5H10v4.5H8.6zM1 10h10v1H1z"/>',
  park: '<path d="M6 1l3.5 5H7.8l2.2 3H6.6v2H5.4V9H2l2.2-3H2.5z"/>',
  education: '<path d="M6 2l5.5 2.5L6 7 .5 4.5zM2.8 6v2.6C3.8 9.6 8.2 9.6 9.2 8.6V6L6 7.5z"/>',
  health: '<path d="M4.6 1.5h2.8v3.1h3.1v2.8H7.4v3.1H4.6V7.4H1.5V4.6h3.1z"/>',
  transit: '<path d="M3.5 1h5A1.5 1.5 0 0 1 10 2.5v6a1 1 0 0 1-1 1l1 1.5H8.8l-1-1.5H4.2l-1 1.5H2l1-1.5a1 1 0 0 1-1-1v-6A1.5 1.5 0 0 1 3.5 1zM3 3v2.5h6V3zM4 7a.8.8 0 1 0 0 1.6A.8.8 0 0 0 4 7zm4 0a.8.8 0 1 0 0 1.6A.8.8 0 0 0 8 7z"/>',
  fuel: '<path d="M2 2a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v8h.5v1h-7v-1H2zM3 2.2v2.6h4V2.2zM8.6 3.2l.7-.7 1.4 1.4v4.6a1 1 0 0 1-2 0V7H8V6h1v2.5h.7V4.3z"/>',
}

// class -> [font css, text transform, letter spacing (em), colour, extra class]
function styleOf(it, zoom) {
  const k = it.k
  if (k === "continent") return { font: "700 25px", caps: true, ls: 0.3, cls: "lb-continent" }
  if (k === "country") return { font: "700 19px", caps: true, ls: 0.2, cls: "lb-country" }
  if (k === "state") return { font: "700 15px", caps: true, ls: 0.16, cls: "lb-state" }
  if (k === "water") {
    const big = it.e === "ocean" || it.e === "sea"
    return { font: big ? "italic 600 20px" : "italic 600 16px", caps: false, ls: big ? 0.06 : 0.02, cls: "lb-water" }
  }
  if (k === "land") return { font: "600 13px", caps: true, ls: 0.3, cls: "lb-land" }
  if (k === "district") return { font: "700 14px", caps: true, ls: 0.1, cls: "lb-district" }
  if (k === "street") return { font: "600 13px", caps: true, ls: 0.04, cls: "lb-street" }
  if (k === "poi") return { font: "600 14px", caps: false, ls: 0, cls: "lb-poi", icon: true }
  // city / town: by size (rank 40 - log10(population))
  const r = it.r
  const capital = it.e === "capital"
  if (r <= 33.4 || capital) return { font: "700 21px", caps: false, ls: 0, cls: "lb-city lb-big", dot: true }
  if (r <= 34.4) return { font: "700 18px", caps: false, ls: 0, cls: "lb-city", dot: true }
  if (r <= 35.4) return { font: "600 16px", caps: false, ls: 0, cls: "lb-city", dot: true }
  return { font: "600 14.5px", caps: false, ls: 0, cls: "lb-city lb-small", dot: true }
}

function priorityOf(it) {
  // (Natural Earth's ranks: continent 0+, water 10+, country 20+, state 30+, city / town 32-40; ranges after the
  // towns; streets 50+, places 70+)
  if (it.k === "land") return it.r - 15
  if (it.k === "district") return 44 + (it.r - 36) * 0.2
  return it.r
}

export class LabelLayer {
  constructor(el) {
    this.el = el
    this.items = new Map()       // id -> state
    this.view = null
    this.raf = 0
    this.canvas = document.createElement("canvas").getContext("2d")
    this.family = getComputedStyle(el).fontFamily || "sans-serif"
    this.widths = new Map()
  }

  destroy() {
    if (this.raf) cancelAnimationFrame(this.raf)
    this.el.innerHTML = ""
    this.items.clear()
  }

  textWidth(text, font, ls) {
    const key = font + "|" + ls + "|" + text
    let w = this.widths.get(key)
    if (w === undefined) {
      this.canvas.font = `${font} ${this.family}`
      w = this.canvas.measureText(text).width + ls * parseFloat(font.split(" ").pop()) * text.length
      if (this.widths.size > 20000) this.widths.clear()
      this.widths.set(key, w)
    }
    return w
  }

  setLabels(data) {
    const list = (data && data.items) || []
    const seen = new Set()
    for (const it of Object.values(list)) {
      if (!it || !it.id) continue
      seen.add(it.id)
      const s = this.items.get(it.id)
      if (s) { s.it = it; continue }
      this.items.set(it.id, { it, el: null, shown: false, alpha: 0 })
    }
    for (const [id, s] of this.items) {
      if (!seen.has(id)) {
        if (s.el) s.el.remove()
        this.items.delete(id)
      }
    }
  }

  setView(v) {
    const t = performance.now()
    if (this.view) this.prev = { v: this.view, t: this.viewT }
    this.view = v
    this.viewT = t
    if (!this.raf) this.raf = requestAnimationFrame(() => { this.raf = 0; this.frame() })
  }

  // the camera now + LEAD_MS, from its motion between the last two messages
  predicted() {
    const v = this.view, p = this.prev
    if (!p) return v
    const step = this.viewT - p.t
    if (!(step > 2 && step < 120)) return v
    const k = Math.min(MAX_EXTRAP, (performance.now() - this.viewT + LEAD_MS) / step)
    const out = Object.assign({}, v)
    for (const f of ["px", "py", "pz", "fx", "fy", "fz", "ux", "uy", "uz", "rx", "ry", "rz", "zoom"]) {
      out[f] = v[f] + (v[f] - p.v[f]) * k
    }
    for (const b of [["fx", "fy", "fz"], ["ux", "uy", "uz"], ["rx", "ry", "rz"]]) {
      const n = Math.hypot(out[b[0]], out[b[1]], out[b[2]]) || 1
      for (const f of b) out[f] /= n
    }
    return out
  }

  makeEl(s, st, text) {
    const it = s.it
    const d = document.createElement("div")
    d.className = "lb " + st.cls
    if (it.k === "poi" && POI_COLOURS[it.e]) d.style.color = POI_COLOURS[it.e]
    let html = ""
    if (st.dot) html += '<span class="lb-dot"></span>'
    if (st.icon) {
      html += `<span class="lb-icon" style="background:${POI_COLOURS[it.e] || "#888"}"><svg viewBox="0 0 12 12">` +
        `${POI_ICONS[it.e] || ""}</svg></span>`
    }
    const t = document.createElement("span")
    t.className = "lb-text"
    t.textContent = text
    d.innerHTML = html
    d.appendChild(t)
    d.style.font = `${st.font} ${this.family}`
    if (st.ls) d.style.letterSpacing = st.ls + "em"
    this.el.appendChild(d)
    s.el = d
    s.textNode = t
  }

  frame() {
    if (!this.view) return
    const v = this.predicted()
    const W = this.el.clientWidth || window.innerWidth, H = this.el.clientHeight || window.innerHeight
    const F = (H / 2) / Math.tan((v.fov || 55) * Math.PI / 360)
    const zoom = v.zoom
    const cands = []
    for (const s of this.items.values()) {
      const it = s.it
      const xz = it.xz + (LONGER.has(it.k) ? LONGER_Z : 0)
      if (zoom < it.mz - 0.05 || zoom > xz + 0.5) { s.want = false; continue }
      const dx = it.x - v.px, dy = it.y - v.py, dz = it.z - v.pz
      const zc = dx * v.fx + dy * v.fy + dz * v.fz
      if (zc < 1) { s.want = false; continue }
      // (past the horizon: the earth between the camera and the label)
      const nx = it.x - v.ox, ny = it.y - v.oy, nz = it.z - v.oz
      if (nx * -dx + ny * -dy + nz * -dz < 0) { s.want = false; continue }
      const sx = W / 2 + F * (dx * v.rx + dy * v.ry + dz * v.rz) / zc
      const sy = H / 2 - F * (dx * v.ux + dy * v.uy + dz * v.uz) / zc
      if (sx < -200 || sx > W + 200 || sy < -100 || sy > H + 100) { s.want = false; continue }
      s.sx = sx
      s.sy = sy
      s.angle = 0
      if (it.k === "street" && it.dx != null) {      // (along its road on the screen, never upside down)
        const ex = it.x + it.dx - v.px, ey = it.y + it.dy - v.py, ez = it.z + it.dz - v.pz
        const zc2 = ex * v.fx + ey * v.fy + ez * v.fz
        if (zc2 > 1) {
          const sx2 = W / 2 + F * (ex * v.rx + ey * v.ry + ez * v.rz) / zc2
          const sy2 = H / 2 - F * (ex * v.ux + ey * v.uy + ez * v.uz) / zc2
          let a = Math.atan2(sy2 - sy, sx2 - sx) * 180 / Math.PI
          if (a > 90) a -= 180
          if (a < -90) a += 180
          s.angle = a
        }
      }
      s.prio = priorityOf(it) - (s.shown ? KEEP_BONUS : 0)
      cands.push(s)
    }
    cands.sort((a, b) => a.prio - b.prio)
    const placed = []
    const written = new Map()       // a street's text -> where it is written already
    let streets = 0
    const pois = []
    const reserved = RESERVED.map(r => ({
      x0: r.left, x1: W - (r.right || 0),
      y0: r.top != null ? r.top : H - r.bottom - r.height, y1: r.top != null ? r.top + r.height : H - r.bottom,
    }))
    for (const s of cands) {
      const it = s.it
      const st = styleOf(it, zoom)
      let text = st.caps ? it.t.toUpperCase() : it.t
      if (it.k === "state" && it.e && zoom < it.mz + 1.6) text = it.e     // (far out: the postal code, "PA")
      const size = parseFloat(st.font.split(" ").pop())
      const tw = this.textWidth(text, st.font, st.ls)
      const extra = st.dot ? 10 : st.icon ? 21 : 0
      const w = tw + extra, h = size * 1.25
      const rad = Math.abs(s.angle) * Math.PI / 180
      const bw = Math.abs(w * Math.cos(rad)) + Math.abs(h * Math.sin(rad))
      const bh = Math.abs(w * Math.sin(rad)) + Math.abs(h * Math.cos(rad))
      // (a dot or icon at the place, its text beside it; other labels centred on their place)
      const cx = (st.dot || st.icon) ? s.sx + w / 2 - (st.icon ? 9 : 4) : s.sx
      const box = { x0: cx - bw / 2 - PAD, x1: cx + bw / 2 + PAD, y0: s.sy - bh / 2 - PAD, y1: s.sy + bh / 2 + PAD }
      let ok = box.x0 >= 0 && box.x1 <= W && box.y0 >= 0 && box.y1 <= H
      if (ok) for (const r of reserved) if (box.x0 < r.x1 && box.x1 > r.x0 && box.y0 < r.y1 && box.y1 > r.y0) { ok = false; break }
      if (ok) for (const p of placed) if (box.x0 < p.x1 && box.x1 > p.x0 && box.y0 < p.y1 && box.y1 > p.y0) { ok = false; break }
      if (ok && it.k === "poi") {
        if (pois.length >= POIS_MAX) ok = false
        else for (const q of pois) if (Math.hypot(q[0] - s.sx, q[1] - s.sy) < POI_GAP_PX) { ok = false; break }
      }
      if (ok && it.k === "street") {
        if (streets >= STREETS_MAX) ok = false
        const at = written.get(text)
        if (ok && at) for (const q of at) if (Math.hypot(q[0] - s.sx, q[1] - s.sy) < SAME_TEXT_PX) { ok = false; break }
      }
      s.want = ok
      if (!ok) continue
      placed.push(box)
      if (it.k === "poi") pois.push([s.sx, s.sy])
      if (it.k === "street") {
        streets++
        if (!written.has(text)) written.set(text, [])
        written.get(text).push([s.sx, s.sy])
      }
      if (!s.el) this.makeEl(s, st, text)
      else if (s.textNode.textContent !== text) s.textNode.textContent = text
      s.cx = cx
    }
    for (const s of this.items.values()) {
      if (s.want) {
        const it = s.it
        const xz = it.xz + (LONGER.has(it.k) ? LONGER_Z : 0)
        const fade = Math.min(1, Math.max(0, (zoom - it.mz) / FADE_Z + 0.15), Math.max(0, (xz + 0.5 - zoom) / FADE_Z))
        s.el.style.opacity = String(fade)
        s.el.style.transform = `translate(${s.cx.toFixed(1)}px, ${s.sy.toFixed(1)}px) translate(-50%, -50%)` +
          (s.angle ? ` rotate(${s.angle.toFixed(1)}deg)` : "")
        s.shown = true
      } else if (s.el && s.shown) {
        s.el.style.opacity = "0"
        s.shown = false
      }
    }
  }
}
