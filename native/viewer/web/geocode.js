// Place search and "where am I", the mod's way: the viewer never calls a geocoder itself. It sends earthd the
// same messages the mod sends (server/geocode.py: Photon by komoot, OpenStreetMap data; earthd's netfetch limits
// and caches the requests):
//   geocode {id, q, lat, lon, full?} -> geocode_result {id, results: [{name, detail, kind, lat, lon}], error?}
//       the in-game map's search box (EarthMap.vue liveSearch): 300 ms after the last key, 3+ characters,
//       biased toward the camera; a typed "lat, lon" goes straight there on Enter
//   where {id, lat, lon, online}  -> where_result {id, street, place, region, country}
//       the status bar (statusbar.lua M.request): the street from earthd's map data, the town / region /
//       country from Photon's reverse geocoding (position rounded to ~1 km) only when `online`
export const ATTRIBUTION = 'Search: OpenStreetMap (Nominatim, Photon by komoot), Wikipedia, Wikidata'
export const MIN_CHARS = 3
export const DEBOUNCE_MS = 300
export const LATLON_RE = /^\s*([-+]?\d+(?:\.\d+)?)\s*[°]?\s*([NSns])?\s*[,;\s]\s*([-+]?\d+(?:\.\d+)?)\s*[°]?\s*([EWew])?\s*$/

// "lat, lon" (also "36.5 N, 118.2 W") -> [lat, lon], or null
export function parseLatLon(text) {
  const m = LATLON_RE.exec(String(text))
  if (!m) return null
  let lat = +m[1], lon = +m[3]
  if (m[2] && /s/i.test(m[2])) lat = -Math.abs(lat)
  if (m[4] && /w/i.test(m[4])) lon = -Math.abs(lon)
  if (!(Math.abs(lat) <= 90 && Math.abs(lon) <= 180)) return null
  return [lat, lon]
}

// metres between two lat / lon points (statusbar.lua M.dist: equirectangular)
export function dist(lat1, lon1, lat2, lon2) {
  const k = 111320
  const dx = (lon2 - lon1) * k * Math.cos((lat1 + lat2) / 2 * Math.PI / 180)
  return Math.hypot(dx, (lat2 - lat1) * k)
}

// The status bar's timing (statusbar.lua): a request when the camera moved WHERE_STREET_M (at most every
// WHERE_MIN_S), the place names again after WHERE_PLACE_M or WHERE_PLACE_S; a jump past JUMP_M clears the old place.
export class Where {
  static STREET_M = 30
  static PLACE_M = 1500
  static MIN_S = 2.0
  static PLACE_S = 120
  static JUMP_M = 2000

  constructor() {
    this.loc = { street: '', place: '', region: '', country: '' }
    this.last = null
    this.nextId = 0
    this.online = true      // (the mod's setting placeNamesOnline)
  }

  // the 'where' message due now, or null. now: s
  request(now, lat, lon) {
    if (lat == null || lon == null) return null
    const last = this.last
    const moved = last ? dist(last.lat, last.lon, lat, lon) : Infinity
    if (last && (now - last.t < Where.MIN_S || moved < Where.STREET_M)) return null
    const placeDue = this.online && (!last || last.pt == null || now - last.pt >= Where.PLACE_S
      || dist(last.plat, last.plon, lat, lon) >= Where.PLACE_M)
    this.last = { lat, lon, t: now, plat: last?.plat, plon: last?.plon, pt: last?.pt }
    if (placeDue) { this.last.plat = lat; this.last.plon = lon; this.last.pt = now }
    if (moved !== Infinity && moved > Where.JUMP_M) this.loc = { street: '', place: '', region: '', country: '' }
    return { type: 'where', id: ++this.nextId, lat, lon, online: !!placeDue }
  }

  onResult(msg) {
    if (msg.street != null) this.loc.street = String(msg.street)
    if (msg.place != null || msg.country != null) {
      this.loc.place = msg.place || ''; this.loc.region = msg.region || ''; this.loc.country = msg.country || ''
    }
  }

  reset() { this.loc = { street: '', place: '', region: '', country: '' }; this.last = null }

  // "Thames Street  |  Baltimore, Maryland, United States"
  text() {
    const l = this.loc
    const place = [l.place, l.region, l.country].filter(Boolean).join(', ')
    return [l.street, place].filter(Boolean).join('  |  ')
  }
}

// Recent places (the mod's "recent" list): the places teleported to, newest first.
const RECENT_KEY = 'bnge-viewer-recent'
const RECENT_MAX = 20
export function recentPlaces() {
  try { const v = JSON.parse(localStorage.getItem(RECENT_KEY)); return Array.isArray(v) ? v : [] } catch (e) { return [] }
}
export function addRecent(p) {
  if (!p || !p.name) return
  const list = recentPlaces().filter(r => dist(r.lat, r.lon, p.lat, p.lon) > 50 || r.name !== p.name)
  list.unshift({ name: p.name, detail: p.detail || '', lat: p.lat, lon: p.lon })
  try { localStorage.setItem(RECENT_KEY, JSON.stringify(list.slice(0, RECENT_MAX))) } catch (e) { /* none */ }
}
// recent places matching text (every word a prefix of a word of the name or detail)
export function recentHits(text, limit = 3) {
  const words = String(text).toLowerCase().split(/\s+/).filter(Boolean)
  if (!words.length) return []
  return recentPlaces().filter(r => {
    const hay = `${r.name} ${r.detail}`.toLowerCase().split(/[\s,]+/)
    return words.every(w => hay.some(h => h.startsWith(w)))
  }).slice(0, limit).map(r => ({ ...r, recent: true }))
}

// The search box: requests through the world's earthd connection, answers matched by id.
export class Search {
  constructor(send) {
    this.send = send
    this.nextId = 0
    this.pending = new Map()      // id -> resolve
  }
  // a promise of {results, error}; rejects after timeoutMs without an answer
  query(q, lat, lon, timeoutMs = 15000, full = false) {
    const id = ++this.nextId
    const msg = { type: 'geocode', id, q }
    if (full) msg.full = true
    if (lat != null && lon != null) { msg.lat = lat; msg.lon = lon }
    return new Promise((resolve, reject) => {
      if (!this.send(msg)) { reject(new Error('Not connected to earthd')); return }
      const timer = setTimeout(() => { this.pending.delete(id); reject(new Error('No answer from the address search')) }, timeoutMs)
      this.pending.set(id, r => { clearTimeout(timer); resolve(r) })
    })
  }
  onResult(msg) {
    const done = this.pending.get(msg.id)
    if (!done) return
    this.pending.delete(msg.id)
    done({ results: Array.isArray(msg.results) ? msg.results : [], error: msg.error, text: msg.text })
  }
}

// --- geocoding in the viewer itself (a debugging switch) -------------------------------------------------
// The same Photon requests earthd makes (server/geocode.py url / reverse_url) and the same parsing (label, parse,
// parse_place, ported line for line), sent by the page (Photon allows any origin), so each request and answer can be
// watched in the browser's network tab. Photon's fair use as earthd's netfetch applies it: one request at a time, at
// most one a second, answers cached (searches 1 day, as the rate-limits notes; reverse by the rounded position).
// Names in English: earthd picks the language from the country under the camera (geocode.lang_for).
export const PHOTON_URL = 'https://photon.komoot.io/api/'
export const REVERSE_URL = 'https://photon.komoot.io/reverse'
export const LIMIT = 7
export const REVERSE_ROUND = 2
const SEARCH_TTL_MS = 24 * 3600 * 1000
const MIN_GAP_MS = 1000

export function photonUrl(q, lat = null, lon = null, limit = LIMIT, lang = 'en') {
  const p = new URLSearchParams([['q', q.trim()], ['limit', String(limit | 0)], ['lang', lang]])
  if (lat != null && lon != null) { p.append('lat', (+lat).toFixed(4)); p.append('lon', (+lon).toFixed(4)) }
  return `${PHOTON_URL}?${p.toString().replace(/%20/g, '+')}`
}

// Python's round(x, 2) then "%.2f" (half to even on the binary value: toFixed is the same rounding of the double)
export function reverseUrl(lat, lon, lang = 'en') {
  const p = new URLSearchParams([['lat', (+lat).toFixed(REVERSE_ROUND)], ['lon', (+lon).toFixed(REVERSE_ROUND)],
    ['lang', lang], ['limit', '1']])
  return `${REVERSE_URL}?${p}`
}

// geocode.label: (name, detail, kind) of one feature's properties
export function label(props) {
  const p = props || {}
  const street = [p.housenumber, p.street].filter(Boolean).join(' ')
  const name = p.name || street || p.city || p.state || p.country || '?'
  const parts = []
  if (street && street !== name) parts.push(street)
  for (const k of ['city', 'district', 'county', 'state', 'country']) {
    const v = p[k]
    if (v && v !== name && !parts.includes(v)) parts.push(v)
    if (parts.length >= 3) break
  }
  return [name, parts.join(', '), p.osm_value || p.type || '']
}

// geocode.parse: [{name, detail, kind, lat, lon}] of a Photon GeoJSON answer
export function parse(data) {
  const out = []
  for (const f of (data && data.features) || []) {
    const c = f && f.geometry && f.geometry.coordinates
    if (!Array.isArray(c) || c.length < 2) continue
    const lon = +c[0], lat = +c[1]
    if (!(lat >= -90 && lat <= 90 && lon >= -180 && lon <= 180)) continue
    const [name, detail, kind] = label(f.properties)
    out.push({ name, detail, kind, lat: Math.round(lat * 1e6) / 1e6, lon: Math.round(lon * 1e6) / 1e6 })
  }
  return out
}

// geocode.parse_place: {place, region, country} of a reverse answer
export function parsePlace(data) {
  const feats = (data && data.features) || []
  const p = (feats[0] && feats[0].properties) || {}
  let place = p.city || p.town || p.village || p.district || p.county || ''
  if (!place && ['city', 'town', 'village', 'locality'].includes(p.type)) place = p.name || ''
  return { place, region: p.state || '', country: p.country || '' }
}

export class Direct {
  constructor(onDebug) {
    this.onDebug = onDebug || (() => {})
    this.cache = new Map()        // url -> { at, data }
    this.chain = Promise.resolve()
    this.lastAt = 0
    this.lastReverse = null       // [key, place] (earthd's _where_place: asked again when the rounded position changed)
  }

  // one request at a time, MIN_GAP_MS apart; cached answers skip the line
  get(url, ttl = SEARCH_TTL_MS) {
    const hit = this.cache.get(url)
    if (hit && Date.now() - hit.at < ttl) {
      this.onDebug({ url, status: 'cache', ms: 0, n: (hit.data.features || []).length })
      return Promise.resolve(hit.data)
    }
    const run = async () => {
      const gap = this.lastAt + MIN_GAP_MS - performance.now()
      if (gap > 0) await new Promise(r => setTimeout(r, gap))
      const t0 = performance.now()
      this.lastAt = t0
      let r
      try {
        r = await fetch(url, { headers: { Accept: 'application/json' } })
      } catch (e) {
        this.onDebug({ url, status: 'network error', ms: performance.now() - t0, error: String(e) })
        throw new Error(`FetchFailed: ${url}: ${e.message || e}`)
      }
      const ms = performance.now() - t0
      if (!r.ok) {
        this.onDebug({ url, status: r.status, ms })
        throw new Error(`FetchFailed: ${url}: HTTP ${r.status}`)
      }
      const data = await r.json()
      this.cache.set(url, { at: Date.now(), data })
      if (this.cache.size > 500) this.cache.delete(this.cache.keys().next().value)
      this.onDebug({ url, status: r.status, ms, n: (data.features || []).length })
      return data
    }
    const p = this.chain.then(run, run)
    this.chain = p.catch(() => {})
    return p
  }

  // as Search.query: {results, error}
  async query(q, lat, lon) {
    q = String(q || '').trim().slice(0, 200)
    if (q.length < MIN_CHARS) return { results: [] }
    try {
      return { results: parse(await this.get(photonUrl(q, lat, lon))) }
    } catch (e) {
      return { results: [], error: e.message }
    }
  }

  // the town / region / country under (lat, lon), or null (a failure: the line keeps the street alone)
  async reverse(lat, lon) {
    const key = `${(+lat).toFixed(REVERSE_ROUND)},${(+lon).toFixed(REVERSE_ROUND)}`
    if (this.lastReverse && this.lastReverse[0] === key) return this.lastReverse[1]
    try {
      const place = parsePlace(await this.get(reverseUrl(lat, lon), Infinity))
      this.lastReverse = [key, place]
      return place
    } catch (e) {
      return null
    }
  }
}

// What to tell the user when the search has no answer: earthd's own words (geocode_result.text, from a server
// with geocode.error_text, GitHub #602), else the HTTP status of a refusal, else the map's old words.
export function errorText(r) {
  if (r.text) return String(r.text)
  const m = /HTTP (\d{3})/.exec(String(r.error || ''))
  if (m && (m[1] === '401' || m[1] === '403')) return `Address search refused by the geocoder (HTTP ${m[1]})`
  if (m) return `Address search failed (HTTP ${m[1]})`
  return 'Address search unavailable (offline?)'
}
