// Web Mercator math for the world map (EarthMap.vue). Pure functions, tested by
// server/tests/test_mapmath.mjs (node). World pixels: 256 * 2^z across, x east, y south.

export const TILE = 256
export const MIN_Z = 2
export const MAX_Z = 18          // server/maptiles.py MAX_Z
export const MAX_LAT = 85.05112878

export function clampLat(lat) { return Math.max(-MAX_LAT, Math.min(MAX_LAT, lat)) }

// longitude into -180..180
export function wrapLon(lon) {
  const w = ((lon + 180) % 360 + 360) % 360 - 180
  return w === -180 && lon > 0 ? 180 : w
}

// lat/lon -> world pixels at zoom z (fractional z allowed)
export function project(lat, lon, z) {
  const s = TILE * Math.pow(2, z)
  const phi = clampLat(lat) * Math.PI / 180
  return {
    x: (lon + 180) / 360 * s,
    y: (1 - Math.log(Math.tan(phi) + 1 / Math.cos(phi)) / Math.PI) / 2 * s,
  }
}

// world pixels at zoom z -> lat/lon
export function unproject(x, y, z) {
  const s = TILE * Math.pow(2, z)
  const n = Math.PI - 2 * Math.PI * y / s
  return { lat: 180 / Math.PI * Math.atan(Math.sinh(n)), lon: wrapLon(x / s * 360 - 180) }
}

// The tiles covering a w x h view centred on (lat, lon) at fractional zoom z: the integer tile zoom
// tz = floor(z) and each tile's screen rect (scaled by 2^(z - tz)). x wraps around the world; rows
// outside it are left out.
export function visibleTiles(lat, lon, z, w, h) {
  const tz = Math.max(0, Math.floor(z))
  const scale = Math.pow(2, z - tz)
  const c = project(lat, lon, tz)
  const size = TILE * scale
  const left = c.x - w / 2 / scale
  const top = c.y - h / 2 / scale
  const n = 1 << tz
  const x0 = Math.floor(left / TILE), x1 = Math.floor((left + w / scale) / TILE)
  const y0 = Math.max(0, Math.floor(top / TILE)), y1 = Math.min(n - 1, Math.floor((top + h / scale) / TILE))
  const out = []
  for (let ty = y0; ty <= y1; ty++) {
    for (let tx = x0; tx <= x1; tx++) {
      out.push({
        z: tz, x: ((tx % n) + n) % n, y: ty, ux: tx,
        left: (tx * TILE - left) * scale, top: (ty * TILE - top) * scale, size,
        key: `${tz}/${tx}/${ty}`,
      })
    }
  }
  return out
}

// Screen position (px from the view's top left) of a lat/lon in a w x h view centred on (clat, clon).
// Longitudes take the shortest way round, so a marker near the antimeridian is drawn next to the centre.
export function toScreen(lat, lon, clat, clon, z, w, h) {
  const s = TILE * Math.pow(2, z)
  const p = project(lat, lon, z), c = project(clat, clon, z)
  let dx = p.x - c.x
  if (dx > s / 2) dx -= s
  if (dx < -s / 2) dx += s
  return { x: w / 2 + dx, y: h / 2 + (p.y - c.y) }
}

// lat/lon under a screen position in the same view
export function fromScreen(sx, sy, clat, clon, z, w, h) {
  const c = project(clat, clon, z)
  const r = unproject(c.x + sx - w / 2, c.y + sy - h / 2, z)
  return { lat: clampLat(r.lat), lon: r.lon }
}

// The view after panning by (dx, dy) screen px (drag right: the map moves right, the centre west)
export function pan(clat, clon, z, dx, dy) {
  const c = project(clat, clon, z)
  const r = unproject(c.x - dx, c.y - dy, z)
  return { lat: clampLat(r.lat), lon: r.lon }
}

// Zoom to z2 keeping the point under the screen position (sx, sy) where it is (mouse-wheel zoom).
export function zoomAt(clat, clon, z, z2, sx, sy, w, h) {
  z2 = Math.max(MIN_Z, Math.min(MAX_Z, z2))
  const p = fromScreen(sx, sy, clat, clon, z, w, h)
  const pp = project(p.lat, p.lon, z2)
  const r = unproject(pp.x - (sx - w / 2), pp.y - (sy - h / 2), z2)
  return { lat: clampLat(r.lat), lon: r.lon, z: z2 }
}

// Places whose name contains every word of the query (case-insensitive), best (name starts with
// the query) first; at most `limit`.
export function searchPlaces(list, query, limit = 8) {
  const q = String(query || "").trim().toLowerCase()
  if (!q) return []
  const words = q.split(/\s+/)
  const hits = (list || []).filter(p => {
    const n = String(p.name || "").toLowerCase()
    return words.every(wd => n.includes(wd))
  })
  hits.sort((a, b) => {
    const sa = String(a.name).toLowerCase().startsWith(q) ? 0 : 1
    const sb = String(b.name).toLowerCase().startsWith(q) ? 0 : 1
    return sa - sb
  })
  return hits.slice(0, limit)
}

// A WMO weather code (settings.uiMapState's weather.code, from weather.lua's last message) as one emoji, the
// same buckets as weather.lua's M.describe (clear / cloud / fog / rain / snow / showers / storm).
export function weatherIcon(code) {
  code = Number(code)
  if (!Number.isFinite(code)) return ""
  if (code === 0) return "☀️"             // clear sky
  if (code <= 2) return "🌤️"         // mainly clear / partly cloudy
  if (code === 3) return "☁️"              // overcast
  if (code === 45 || code === 48) return "🌫️"   // fog
  if (code >= 95) return "⛈️"              // thunderstorm
  if (code >= 85) return "🌨️"        // snow showers
  if (code >= 71 && code <= 77) return "❄️"           // snow
  if (code >= 80) return "🌧️"        // rain showers
  if (code >= 51 && code <= 67) return "🌧️"     // drizzle / rain
  return "☁️"
}

export function fmtLatLon(lat, lon) {
  return `${Math.abs(lat).toFixed(5)}°${lat >= 0 ? "N" : "S"}, ${Math.abs(lon).toFixed(5)}°${lon >= 0 ? "E" : "W"}`
}

// Stand-ins for tiles not loaded yet (the user, 2026-10-14: "map tiles disappear as you zoom in and out ... scale the
// resolution we already have loaded and replace it once the up to date resolution gets loaded"): for each tile of
// `tiles` (visibleTiles) not in ready (a Set of "z/x/y"), the nearest loaded ancestor up to `levels` zooms coarser,
// placed and scaled to cover it. Each ancestor once; coarsest first (drawn under the finer ones).
export function fallbackTiles(tiles, ready, levels = 6) {
  const out = new Map()
  for (const t of tiles) {
    if (ready.has(`${t.z}/${t.x}/${t.y}`)) continue
    for (let d = 1; d <= levels && t.z - d >= 0; d++) {
      const pz = t.z - d, f = 1 << d, n = 1 << pz
      const ax = Math.floor(t.ux / f), ay = Math.floor(t.y / f)
      const axw = ((ax % n) + n) % n
      if (!ready.has(`${pz}/${axw}/${ay}`)) continue
      const key = `f${pz}/${ax}/${ay}`
      if (!out.has(key)) {
        out.set(key, { z: pz, x: axw, y: ay, key, size: t.size * f,
          left: t.left - (t.ux - ax * f) * t.size, top: t.top - (t.y - ay * f) * t.size })
      }
      break
    }
  }
  return [...out.values()].sort((a, b) => a.z - b.z)
}

// ---- tile layers (server/maptiles.py LAYERS: the files the server writes) -------------------------------------
export const LAYER_EXT = { sat: "jpg", map: "jpg", labels: "png" }

// A layer's tile file under dir: dir/z/x/y.jpg for "sat" (where it always was), dir/<layer>/z/x/y.<ext> otherwise.
export function tilePath(dir, layer, z, x, y) {
  return layer === "sat" ? `${dir}${z}/${x}/${y}.jpg` : `${dir}${layer}/${z}/${x}/${y}.${LAYER_EXT[layer]}`
}

// visibleTiles sorted nearest the view's centre first (the order the map asks for them).
export function centreFirst(tiles, w, h) {
  const d = t => {
    const dx = t.left + t.size / 2 - w / 2, dy = t.top + t.size / 2 - h / 2
    return dx * dx + dy * dy
  }
  return tiles.map(t => [d(t), t]).sort((a, b) => a[0] - b[0]).map(a => a[1])
}

// The EOX map layers' credit (maps.eox.at asks for a link back; the data are OpenStreetMap's and others'), or "".
export const EOX_MAP_CREDIT = "EOX IT Services GmbH (maps.eox.at), data © OpenStreetMap contributors and others"
// base: "sat" / "map" ("dark" too - EarthMap.vue passes "map", its tiles with a CSS filter, nothing fetched
// separately for it)
export function layerCredits(base, labels) {
  if (base === "map") return `Map: Terrain Light${labels ? " and Overlay" : ""} by ${EOX_MAP_CREDIT}`
  if (labels) return `Labels: Overlay by ${EOX_MAP_CREDIT}`
  return ""
}

// ---- overlay layers (maplayers.js: Natural Earth borders and names) -------------------------------------------
// lines: flat [lon, lat, lon, lat, ...] arrays. Their lat/lon boxes [w, s, e, n], once.
export function lineBoxes(lines) {
  return lines.map(l => {
    let w = 180, s = 90, e = -180, n = -90
    for (let i = 0; i < l.length; i += 2) {
      if (l[i] < w) w = l[i]
      if (l[i] > e) e = l[i]
      if (l[i + 1] < s) s = l[i + 1]
      if (l[i + 1] > n) n = l[i + 1]
    }
    return [w, s, e, n]
  })
}

// The view's lat/lon box [w, s, e, n] (w > e when it spans the antimeridian), with a margin of px.
export function viewBox(clat, clon, z, w, h, margin = 20) {
  const tl = fromScreen(-margin, -margin, clat, clon, z, w, h)
  const br = fromScreen(w + margin, h + margin, clat, clon, z, w, h)
  const span = (w + 2 * margin) / (TILE * Math.pow(2, z)) * 360
  if (span >= 360) return [-180, br.lat, 180, tl.lat]
  return [tl.lon, br.lat, br.lon, tl.lat]
}

function boxMeets(b, v) {
  if (b[3] < v[1] || b[1] > v[3]) return false
  if (v[0] <= v[2]) return b[2] >= v[0] && b[0] <= v[2]
  return b[2] >= v[0] || b[0] <= v[2]                     // the view spans the antimeridian
}

// Normalised Web Mercator y (0 north edge .. 1 south edge) of a latitude: project(lat, 0, z).y / (TILE * 2^z),
// in the same operations, so mercY(lat) * s is bit for bit project's y.
export function mercY(lat) {
  const phi = clampLat(lat) * Math.PI / 180
  return (1 - Math.log(Math.tan(phi) + 1 / Math.cos(phi)) / Math.PI) / 2
}

// Lines [lon, lat, ...] as normalised Mercator [x, y, ...] (Float64Array each), once: drawing them is then a
// multiply-add per vertex instead of a log and a tan (visibleLines(..., norm)).
export function normLines(lines) {
  return lines.map(l => {
    const a = new Float64Array(l.length)
    for (let j = 0; j < l.length; j += 2) {
      a[j] = (l[j] + 180) / 360
      a[j + 1] = mercY(l[j + 1])
    }
    return a
  })
}

// Points [name, lon, lat, ...] as normalised Mercator, [x0, y0, x1, y1, ...] (visibleLabels opts.norm).
export function normPoints(points) {
  const a = new Float64Array(points.length * 2)
  points.forEach((p, i) => { a[2 * i] = (p[1] + 180) / 360; a[2 * i + 1] = mercY(p[2]) })
  return a
}

// The lines meeting the view, as SVG polyline point strings in screen px (at most max of them). norm: the lines
// from normLines (the same strings, faster).
export function visibleLines(lines, boxes, clat, clon, z, w, h, max = 3000, norm = null) {
  const v = viewBox(clat, clon, z, w, h)
  const out = []
  const s = TILE * Math.pow(2, z), c = project(clat, clon, z)
  for (let i = 0; i < lines.length && out.length < max; i++) {
    if (!boxMeets(boxes[i], v)) continue
    const pts = []
    if (norm) {
      const n = norm[i]
      for (let j = 0; j < n.length; j += 2) {
        let dx = n[j] * s - c.x                 // = toScreen's, operation for operation
        if (dx > s / 2) dx -= s
        if (dx < -s / 2) dx += s
        pts.push((w / 2 + dx).toFixed(1) + "," + (h / 2 + (n[j + 1] * s - c.y)).toFixed(1))
      }
    } else {
      const l = lines[i]
      for (let j = 0; j < l.length; j += 2) {
        const p = toScreen(l[j + 1], l[j], clat, clon, z, w, h)
        pts.push(p.x.toFixed(1) + "," + p.y.toFixed(1))
      }
    }
    out.push(pts.join(" "))
  }
  return out
}

// Labels for the view: points [name, lon, lat, minZoom] in priority order, shown from their minZoom (and below
// maxZoom), none overlapping another (a box of ~charW px per letter, lineH px high), at most max.
// opts.norm: the points from normPoints (the same labels, faster).
export function visibleLabels(points, clat, clon, z, w, h, opts = {}) {
  const { maxZoom = 99, max = 120, charW = 7, lineH = 16, norm = null } = opts
  const out = []
  if (z >= maxZoom) return out
  const v = viewBox(clat, clon, z, w, h, 0)
  const taken = []
  const ws = TILE * Math.pow(2, z), c = project(clat, clon, z)
  for (let i = 0; i < points.length; i++) {
    const p = points[i]
    if (out.length >= max) break
    if (z < p[3]) continue
    if (!boxMeets([p[1], p[2], p[1], p[2]], v)) continue
    let s
    if (norm) {
      let dx = norm[2 * i] * ws - c.x
      if (dx > ws / 2) dx -= ws
      if (dx < -ws / 2) dx += ws
      s = { x: w / 2 + dx, y: h / 2 + (norm[2 * i + 1] * ws - c.y) }
    } else {
      s = toScreen(p[2], p[1], clat, clon, z, w, h)
    }
    const bw = p[0].length * charW + 8
    const box = [s.x - bw / 2, s.y - lineH, s.x + bw / 2, s.y + 4]
    if (taken.some(t => box[0] < t[2] && box[2] > t[0] && box[1] < t[3] && box[3] > t[1])) continue
    taken.push(box)
    out.push({ name: p[0], x: s.x, y: s.y, rank: p[4] })
  }
  return out
}

// The zoom a populated place's name shows from (Natural Earth scalerank 0 .. 8; capitals a little earlier).
export function placeMinZoom(scalerank, capital) {
  return 2.5 + scalerank * 0.9 - (capital ? 1 : 0)
}

// The map's credit line for a view box [w, s, e, n] (viewBox): the imagery sources whose area meets it (entries
// [credit, w, s, e, n]) or that cover everywhere ([credit]), then the other credits.
export function creditsFor(imagery, other, box) {
  const used = []
  for (const c of imagery || []) {
    const name = c[0]
    if (!name || used.includes(name)) continue
    if (c.length >= 5 && !boxMeets([c[1], c[2], c[3], c[4]], box)) continue
    used.push(name)
  }
  const parts = used.length ? ["Map imagery: " + used.join("; ")] : []
  if (other) parts.push(other)
  return parts.join("; ")
}
