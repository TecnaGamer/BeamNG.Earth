<template>
  <!-- the 3D map screen (map3d.lua): transparent over the game; the mouse moves the orbit camera (the UI takes it from
       the camera, as the vanilla big map's screen), the controls as the 2D map's -->
  <div class="earth-map3d" tabindex="-1" ref="rootEl" @keydown.esc.stop="back"
       @keydown="onKey($event, true)" @keyup="onKey($event, false)"
       @mousedown="onDown" @wheel.prevent="onWheel" @contextmenu.prevent @click="ctx = null">
    <!-- the labels (map3dLabels.js: projected each frame from map3d.lua's camera, no overlaps, fading) -->
    <div class="m3-labels" ref="labelsEl"></div>
    <!-- the 2D map's top bar, menus and boxes, the same look and the same calls (the user, 2026-11-07: "overall try to
         make the ui almost the same on both maps"; "set waypoints by clicking on the map and have a right click menu
         like the 2d map") -->
    <div class="map-top" @mousedown.stop @wheel.stop>
      <div class="map-title">BeamNG.Earth map <span class="m3-mode" title="The 3D map is new: expect rough edges">3D (experimental)</span></div>
      <div class="search">
        <input v-model="query" class="search-input" placeholder="Search addresses, places, or lat, lon" maxlength="120"
               @focus="typing(true)" @blur="typing(false)" @keydown.stop="onSearchKey" />
        <div v-if="results.length" class="search-results">
          <div v-for="(r, i) in results" :key="'r' + i" class="search-result" :class="{ active: i === resultIndex }"
               @mousedown.stop @click="goResult(r)">
            <span class="result-name">{{ r.saved ? "★ " : "" }}{{ r.name }}</span>
            <span class="result-coords">{{ r.detail || fmt(r.lat, r.lon) }}</span>
          </div>
          <div class="search-credit">Search: © OpenStreetMap contributors (Nominatim, Photon), Wikipedia, Wikidata</div>
        </div>
        <div v-if="searchMsg && !results.length" class="search-msg">{{ searchMsg }}</div>
        <div v-else-if="searching && !results.length" class="search-msg">Searching…</div>
      </div>
      <button class="map-btn" @click="zoom(-3)" title="Zoom in">+</button>
      <button class="map-btn" @click="zoom(3)" title="Zoom out">-</button>
      <button class="map-btn" @click="toCar" title="Fly to the car">Me</button>
      <div class="layers-wrap">
        <button class="map-btn" :class="{ on: panel === 'places' }" @click="togglePlaces" title="Saved and recent places">Places</button>
        <div v-if="panel === 'places'" class="layers-panel m3-places" @mousedown.stop @wheel.stop>
          <div class="panel-sub">Save this view</div>
          <div class="m3-row">
            <input v-model="saveName" class="search-input" placeholder="Name" maxlength="60"
                   @focus="typing(true)" @blur="typing(false)" @keydown.stop @keydown.enter="saveView" />
            <button class="map-btn small" @click="saveView">Save</button>
          </div>
          <div v-if="placesMsg" class="panel-empty">{{ placesMsg }}</div>
          <div class="panel-sub">Saved</div>
          <div v-for="p in savedPlaces" :key="'p' + p.id" class="panel-item">
            <div class="item-text" @click="goPlace(p)"><div class="item-name">&#9733; {{ p.name }}</div>
              <div class="item-sub">{{ p.text || fmt(p.lat, p.lon) }}</div></div>
          </div>
          <div v-if="!savedPlaces.length" class="panel-empty">No saved places yet.</div>
          <template v-if="recentPlaces.length">
            <div class="panel-sub">Recent</div>
            <div v-for="(p, i) in recentPlaces" :key="'c' + i" class="panel-item">
              <div class="item-text" @click="goPlace(p)"><div class="item-name">{{ p.name || p.text }}</div>
                <div class="item-sub">{{ p.name ? p.text : "" }}</div></div>
            </div>
          </template>
        </div>
      </div>
      <button class="map-btn" @click="to2d" title="The flat world map">2D map</button>
      <button class="map-btn close" @click="back" title="Fly back down to the car (Esc / M)">Close</button>
    </div>

    <!-- where the view is (the 3D map's own: place, coordinates, height) -->
    <div class="m3-place" @mousedown.stop>
      <div class="m3-name">{{ place || "—" }}</div>
      <div class="m3-sub">{{ latlon }} &middot; {{ height }}</div>
    </div>

    <div v-if="toast" class="map-toast">{{ toast }}</div>

    <!-- right-click menu (the 2D map's) -->
    <div v-if="ctx" class="ctx-menu" :style="{ left: ctx.x + 'px', top: ctx.y + 'px' }" @mousedown.stop @contextmenu.prevent>
      <div class="ctx-where">{{ fmt(ctx.lat, ctx.lon) }}</div>
      <div class="ctx-item" :class="{ disabled: !!ms.cannotGo }" @click="ctxTeleport">Teleport here</div>
      <div class="ctx-item" @click="ctxGps">Set GPS destination</div>
      <div class="ctx-item" @click="ctxSave">Save this place</div>
      <div class="ctx-item" @click="ctxCopy">Copy coordinates</div>
      <div v-if="ms.gps" class="ctx-item" @click="clearGps">Clear GPS</div>
    </div>

    <!-- the GPS destination (the 2D map's box) -->
    <div v-if="ms.gps" class="gps-stack" @mousedown.stop>
      <div class="gps-chip">
        <div class="gps-chip-row">
          <span>GPS: {{ ms.gps.name || fmt(ms.gps.lat, ms.gps.lon) }}</span>
          <span v-if="ms.gps.planning" class="gps-dist">planning route…</span>
          <span v-else-if="ms.gps.remaining != null" class="gps-dist">{{ fmtMiles(ms.gps.remaining) }}<template
            v-if="ms.gps.eta != null">, {{ fmtDur(ms.gps.eta) }}</template></span>
          <span v-if="ms.gps.routeError" class="gps-warn" :title="ms.gps.routeError">no road route: heading straight there</span>
          <button class="map-btn small" @click="flyTo(ms.gps.lat, ms.gps.lon)">Show</button>
          <button class="map-btn small" :disabled="!!ms.cannotGo || busy" @click="teleportTo(ms.gps.lat, ms.gps.lon, ms.gps.name)">Teleport</button>
          <button class="map-btn small" :class="{ primary: ms.gps.autodrive }" @click="setAutodrive(!ms.gps.autodrive)"
                  :title="ms.gps.autodrive ? 'Stop driving by itself' : 'The car drives itself along the route (in its lane, at the speed limit)'">
            {{ ms.gps.autodrive ? "Autodrive on" : "Autodrive" }}</button>
          <button class="map-btn small" @click="clearGps">Clear</button>
        </div>
      </div>
    </div>

    <!-- a clicked spot: the 2D map's confirmation (its pin stands on the ground: map3d.setMarker) -->
    <div v-if="target" class="confirm" @mousedown.stop>
      <div class="confirm-title">Teleport here?</div>
      <div class="confirm-where">{{ fmt(target.lat, target.lon) }}</div>
      <div v-if="ms.cannotGo" class="confirm-warn">{{ ms.cannotGo }}</div>
      <div v-if="msg" class="confirm-warn">{{ msg }}</div>
      <div class="confirm-btns">
        <button class="map-btn primary" :disabled="!!ms.cannotGo || busy" @click="teleportTo(target.lat, target.lon, '')">Teleport</button>
        <button class="map-btn" @click="setGps(target.lat, target.lon, ''); setTarget(null)">Set GPS</button>
        <button class="map-btn" @click="setTarget(null)">Cancel</button>
      </div>
    </div>
    <div class="m3-msg" v-else-if="msg" @mousedown.stop>{{ msg }}</div>
    <div class="m3-compass" @mousedown.stop @click="north" title="Face north">
      <!-- a compass needle: its red half points north, the "N" at its tip (the user, 2026-11-08: "add an arrow to the compass") -->
      <svg class="m3-needle" viewBox="-26 -26 52 52" :style="{ transform: `rotate(${-headingDeg}deg)` }">
        <polygon points="0,-15 5,0 -5,0" fill="#ff5a4a" />
        <polygon points="0,15 5,0 -5,0" fill="#e6eaee" />
        <circle r="2" fill="#1b1f24" />
        <text x="0" y="-17" text-anchor="middle" font-size="8.5" font-weight="700" fill="#ff7a6b">N</text>
      </svg>
    </div>
    <div class="m3-help" @mousedown.stop>Click: pick a spot &middot; Right click: menu &middot; Drag: move &middot; Right drag: turn and tilt &middot; Wheel: zoom &middot; WASD: move &middot; Q E: turn &middot; R F: tilt</div>
    <div class="m3-cross"></div>
  </div>
</template>

<script setup>
import { ref, computed, watch, onMounted, onUnmounted, nextTick } from "vue"
import { useEvents } from "@/services/events"
import { LabelLayer } from "./map3dLabels.js"

const rootEl = ref(null)
const labelsEl = ref(null)
let layer = null
const events = useEvents()
events.on("BngEarthMap3dLabels", d => { if (layer) layer.setLabels(d) })
events.on("BngEarthMap3dView", v => { if (layer) layer.setView(v) })
// ---- search, places, GPS (the 2D map's settings.lua calls)
const query = ref("")
const results = ref([])
const searching = ref(false)
const searchMsg = ref("")
const panel = ref("")
const saveName = ref("")
const placesMsg = ref("")
const savedPlaces = ref([])
const recentPlaces = ref([])
let searchSeq = 0
let searchTimer = null
const toArray = v => (Array.isArray(v) ? v : v && typeof v === "object" ? Object.values(v) : [])
const luaStr = t => JSON.stringify(String(t == null ? "" : t))
const num = v => (Number.isFinite(Number(v)) ? String(Number(v)) : "nil")
function fmt(lat, lon) {
  if (lat == null || lon == null) return ""
  return `${Math.abs(lat).toFixed(4)}° ${lat >= 0 ? "N" : "S"}, ${Math.abs(lon).toFixed(4)}° ${lon >= 0 ? "E" : "W"}`
}
function typing(on) { engineLua(`if setCEFTyping then setCEFTyping(${on ? "true" : "false"}) end`) }
function flyTo(lat, lon) {
  if (lat == null || lon == null) return
  engineLua(`beamngEarth_map3d.flyTo(${num(lat)}, ${num(lon)})`)
}
function runSearch(full) {
  const text = query.value.trim()
  const seq = ++searchSeq
  searchMsg.value = ""
  const m = text.match(/^\s*(-?\d+(?:\.\d+)?)\s*[, ]\s*(-?\d+(?:\.\d+)?)\s*$/)
  if (m) { results.value = [{ name: text, lat: Number(m[1]), lon: Number(m[2]) }]; searching.value = false; return }
  if (text.length < 3) { results.value = []; searching.value = false; return }
  searching.value = true
  engineLua(`beamngEarth_settings and beamngEarth_settings.uiGeocode(${luaStr(text)}, ${full ? "true" : "false"}) or nil`, r => {
    if (seq !== searchSeq) return
    if (!r || r.error || !r.id) { searching.value = false; searchMsg.value = (r && r.error) || ""; return }
    let tries = 0
    const poll = () => {
      if (seq !== searchSeq) return
      engineLua(`beamngEarth_settings and beamngEarth_settings.uiGeocodeResult(${r.id}) or nil`, res => {
        if (seq !== searchSeq) return
        if (!res) { if (++tries < 60) setTimeout(poll, 150); else { searching.value = false; searchMsg.value = "No answer from the address search" }; return }
        searching.value = false
        results.value = toArray(res.results)
        if (!results.value.length) searchMsg.value = res.error ? "Address search unavailable" : full ? "Nothing found" : "Press Enter to search streets and addresses too"
      })
    }
    setTimeout(poll, 150)
  })
}
watch(query, () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => runSearch(false), 300) })
const resultIndex = ref(0)
watch(results, () => { resultIndex.value = 0 })
function onSearchKey(e) {
  if (e.key === "Enter") { if (results.value.length) goResult(results.value[resultIndex.value] || results.value[0]); else runSearch(true) }
  else if (e.key === "Escape") { query.value = ""; results.value = []; e.target.blur() }
  else if (e.key === "ArrowDown" && results.value.length) { e.preventDefault(); resultIndex.value = (resultIndex.value + 1) % results.value.length }
  else if (e.key === "ArrowUp" && results.value.length) { e.preventDefault(); resultIndex.value = (resultIndex.value + results.value.length - 1) % results.value.length }
}
function goResult(r) { flyTo(Number(r.lat), Number(r.lon)); results.value = []; query.value = "" }
function loadPlaces(res) {
  if (!res) return
  savedPlaces.value = toArray(res.places)
  recentPlaces.value = toArray(res.recent).slice(0, 8)
  if (res.ok === false && res.error) placesMsg.value = res.error
}
function togglePlaces() {
  panel.value = panel.value === "places" ? "" : "places"
  if (panel.value === "places") {
    placesMsg.value = ""
    engineLua("beamngEarth_settings and beamngEarth_settings.uiPlaces and beamngEarth_settings.uiPlaces() or nil", loadPlaces)
  }
}
function goPlace(p) {
  let lat = p.lat, lon = p.lon
  if (lat == null && p.text) { const m = String(p.text).match(/(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)/); if (m) { lat = Number(m[1]); lon = Number(m[2]) } }
  flyTo(lat, lon)
}
function saveView() {
  if (st.value.lat == null) return
  const name = saveName.value.trim() || (place.value || "").split(",")[0] || "Saved view"
  const text = `${st.value.lat.toFixed(6)}, ${st.value.lon.toFixed(6)}`
  engineLua(`beamngEarth_settings and beamngEarth_settings.uiPlaceAdd and beamngEarth_settings.uiPlaceAdd(${luaStr(name)}, ${luaStr(text)}) or nil`, res => {
    loadPlaces(res)
    if (res && res.ok !== false) { placesMsg.value = `Saved "${name}"`; saveName.value = "" }
  })
}
function routeHere() {
  engineLua(`beamngEarth_map3d.gpsHere(${luaStr((place.value || "").split(",")[0])})`, r => {
    msg.value = r && r.ok === false ? "No route: the GPS could not plan one" : ""
  })
}
function clearRoute() { engineLua("if beamngEarth_settings and beamngEarth_settings.uiMapGps then beamngEarth_settings.uiMapGps(nil) end") }

// ---- the 2D map's state, menus and boxes (settings.uiMapState: the GPS, whether a teleport may go now, the car)
const ms = ref({})
const ctx = ref(null)
const target = ref(null)
const toast = ref("")
let toastTimer = null
function pollMap() {
  engineLua("beamngEarth_settings and beamngEarth_settings.uiMapState and beamngEarth_settings.uiMapState() or nil", r => {
    if (r) ms.value = r
  })
}
function showToast(t) { toast.value = t; clearTimeout(toastTimer); toastTimer = setTimeout(() => { toast.value = "" }, 1500) }
function copyText(text) {
  const fallback = () => {
    try {
      const ta = document.createElement("textarea")
      ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0"
      document.body.appendChild(ta); ta.select(); document.execCommand("copy"); document.body.removeChild(ta)
      showToast("Copied")
    } catch (e) { showToast("Copy failed") }
  }
  if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(() => showToast("Copied"), fallback)
  else fallback()
}
function fmtMiles(m) {
  if (ms.value.metric || (ms.value.gps && ms.value.gps.metric)) {
    const km = m / 1000
    return km < 1 ? `${Math.round(m / 10) * 10} m` : `${km < 10 ? km.toFixed(1) : Math.round(km)} km`
  }
  const mi = m / 1609.344
  return mi < 0.1 ? `${Math.round(m * 3.28084)} ft` : `${mi < 10 ? mi.toFixed(1) : Math.round(mi)} mi`
}
function fmtDur(sec) { const m = Math.round(sec / 60); return m < 60 ? `${Math.max(1, m)} min` : `${Math.floor(m / 60)} h ${m % 60} min` }
function setTarget(t) {
  target.value = t
  msg.value = ""
  engineLua(t ? `beamngEarth_map3d.setMarker(${num(t.lat)}, ${num(t.lon)})` : "beamngEarth_map3d.setMarker(nil)")
}
function setGps(lat, lon, name) {
  engineLua(`beamngEarth_settings and beamngEarth_settings.uiMapGps(${num(lat)}, ${num(lon)}, ${luaStr(name || "")}) or nil`, r => {
    if (r && r.ok) { ms.value = { ...ms.value, gps: { lat, lon, name, planning: true } }; showToast("GPS destination set") }
  })
}
function setAutodrive(on) {
  engineLua(`beamngEarth_settings and beamngEarth_settings.uiGpsAutodrive(${on ? "true" : "false"}) or nil`, r => {
    if (!r || !r.ok) return
    ms.value = { ...ms.value, gps: ms.value.gps && { ...ms.value.gps, autodrive: r.autodrive } }
    if (r.autodrive) back()
  })
}
function clearGps() {
  ctx.value = null
  engineLua("beamngEarth_settings and beamngEarth_settings.uiMapGps(nil) or nil", () => { ms.value = { ...ms.value, gps: null } })
}
function teleportTo(lat, lon, name) {
  if (ms.value.cannotGo || lat == null) return
  busy.value = true
  msg.value = ""
  engineLua(`beamngEarth_map3d.driveHere(${num(lat)}, ${num(lon)}, ${luaStr(name || "")})`, r => {
    if (!r || !r.ok) { busy.value = false; msg.value = (r && r.error) || "Teleport failed" }
    else if (r.pending) { target.value = null; msg.value = "Loading the place, then down to the car..." }
    else busy.value = false
  })
}
function toCar() {
  const p = ms.value.player
  if (p && p.lat != null) flyTo(p.lat, p.lon)
}
function closeCtx() { ctx.value = null }
function ctxTeleport() { const c = ctx.value; if (!c || ms.value.cannotGo) return; closeCtx(); teleportTo(c.lat, c.lon, "") }
function ctxGps() { const c = ctx.value; if (!c) return; closeCtx(); setGps(c.lat, c.lon, "") }
function ctxCopy() { const c = ctx.value; if (!c) return; closeCtx(); copyText(`${c.lat.toFixed(6)}, ${c.lon.toFixed(6)}`) }
function ctxSave() {
  const c = ctx.value
  if (!c) return
  closeCtx()
  engineLua(`beamngEarth_settings and beamngEarth_settings.uiPlaceAdd and beamngEarth_settings.uiPlaceAdd("", ${luaStr(`${c.lat.toFixed(6)}, ${c.lon.toFixed(6)}`)}) or nil`, res => {
    loadPlaces(res)
    showToast(res && res.ok !== false ? "Saved" : "Not saved")
  })
}
// the ground under a screen point (map3d.pick: the camera's ray on the earth)
function pick(x, y, cb) {
  engineLua(`beamngEarth_map3d.pick(${x}, ${y}, ${window.innerWidth}, ${window.innerHeight})`, r => { if (r && r.lat != null) cb(r) })
}

function onResize() { engineLua(`beamngEarth_map3d.setScreen(${window.innerHeight})`) }
const st = ref({})
const place = ref("")
const busy = ref(false)
const msg = ref("")
let timer = null
let mapTimer = null
let drag = null

function engineLua(script, cb) {
  if (!window.bngApi || !window.bngApi.engineLua) return
  window.bngApi.engineLua(script, cb)
}

const headingDeg = computed(() => ((st.value.heading || 0) * 180 / Math.PI) % 360)
const latlon = computed(() => st.value.lat != null
  ? `${Math.abs(st.value.lat).toFixed(5)}° ${st.value.lat >= 0 ? "N" : "S"}, ${Math.abs(st.value.lon).toFixed(5)}° ${st.value.lon >= 0 ? "E" : "W"}`
  : "")
const height = computed(() => {
  const d = st.value.dist || 0
  const up = d * Math.sin(st.value.pitch || 0.9)
  const mi = up / 1609.344
  return mi >= 0.5 ? `${mi.toFixed(mi < 10 ? 1 : 0)} mi up` : `${Math.round(up * 3.28084)} ft up`
})

function poll() {
  engineLua("beamngEarth_map3d and beamngEarth_map3d.uiState and beamngEarth_map3d.uiState() or nil", s => {
    if (!s) return
    st.value = s
    if (s.place != null) place.value = s.place
  })
}

function onDown(e) {
  ctx.value = null
  drag = { x: e.clientX, y: e.clientY, x0: e.clientX, y0: e.clientY, moved: 0, button: e.button,
    mode: e.button === 2 || e.button === 1 ? "orbit" : "pan" }
  window.addEventListener("mousemove", onMove)
  window.addEventListener("mouseup", onUp)
}

function onMove(e) {
  if (!drag) return
  const dx = e.clientX - drag.x, dy = e.clientY - drag.y
  drag.x = e.clientX
  drag.y = e.clientY
  drag.moved = Math.max(drag.moved, Math.hypot(e.clientX - drag.x0, e.clientY - drag.y0))
  if (dx || dy) engineLua(`beamngEarth_map3d.drag(${dx}, ${dy}, '${drag.mode}', ${window.innerHeight})`)
}

function onUp(e) {
  const d = drag
  drag = null
  window.removeEventListener("mousemove", onMove)
  window.removeEventListener("mouseup", onUp)
  // a click, not a drag (the 2D map's): left picks a spot (its pin and the teleport box), right opens the menu
  if (!d || !e || d.moved > 4) return
  const x = d.x0, y = d.y0
  if (d.button === 0) pick(x, y, r => setTarget({ lat: r.lat, lon: r.lon }))
  else if (d.button === 2) pick(x, y, r => {
    ctx.value = { x: Math.min(x, window.innerWidth - 210), y: Math.min(y, window.innerHeight - 200), lat: r.lat, lon: r.lon }
  })
}

function onWheel(e) {
  zoom(Math.sign(e.deltaY) * Math.min(3, Math.max(1, Math.abs(e.deltaY) / 100)))
}

// the keyboard (the game's camera keys are off while a menu screen is open): held keys move the view every frame
const held = new Set()
const KEYS = { w: "up", arrowup: "up", s: "down", arrowdown: "down", a: "left", arrowleft: "left", d: "right",
  arrowright: "right", q: "turnL", e: "turnR", r: "tiltUp", f: "tiltDown", "+": "in", "=": "in", "-": "out",
  pageup: "in", pagedown: "out" }
let keyTimer = null
function onKey(e, down) {
  if (e.target && (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA")) return
  const k = KEYS[(e.key || "").toLowerCase()]
  if (!k) return
  e.preventDefault()
  if (down) held.add(k)
  else held.delete(k)
  if (held.size && !keyTimer) keyTimer = setInterval(keyStep, 33)
  if (!held.size && keyTimer) { clearInterval(keyTimer); keyTimer = null }
}
function keyStep() {
  const px = 18, h = window.innerHeight
  let dx = 0, dy = 0, ox = 0, oy = 0, z = 0
  if (held.has("up")) dy += px
  if (held.has("down")) dy -= px
  if (held.has("left")) dx += px
  if (held.has("right")) dx -= px
  if (held.has("turnL")) ox -= 6
  if (held.has("turnR")) ox += 6
  if (held.has("tiltUp")) oy -= 6
  if (held.has("tiltDown")) oy += 6
  if (held.has("in")) z -= 0.4
  if (held.has("out")) z += 0.4
  let lua = ""
  if (dx || dy) lua += `beamngEarth_map3d.drag(${dx}, ${dy}, 'pan', ${h}); `
  if (ox || oy) lua += `beamngEarth_map3d.drag(${ox}, ${oy}, 'orbit', ${h}); `
  if (z) lua += `beamngEarth_map3d.zoom(${z}); `
  if (lua) engineLua(lua)
}

function zoom(n) { engineLua(`beamngEarth_map3d.zoom(${n})`) }
function north() { engineLua("beamngEarth_map3d.setHeading(0, true)") }   // (turned smoothly, the short way)
function back() { engineLua("beamngEarth_map3d.exit()") }
function to2d() { engineLua("beamngEarth_map3d.to2d()") }

onMounted(() => {
  layer = new LabelLayer(labelsEl.value)
  onResize()
  window.addEventListener("resize", onResize)
  // (the labels the map has already: sent again for this screen)
  engineLua("if beamngEarth_map3d and beamngEarth_map3d.resendLabels then beamngEarth_map3d.resendLabels() end")
  poll()
  pollMap()
  timer = setInterval(poll, 250)
  mapTimer = setInterval(pollMap, 1000)
  nextTick(() => rootEl.value && rootEl.value.focus())
})

onUnmounted(() => {
  window.removeEventListener("resize", onResize)
  if (layer) layer.destroy()
  layer = null
  clearInterval(timer)
  clearInterval(mapTimer)
  clearTimeout(toastTimer)
  if (keyTimer) clearInterval(keyTimer)
  engineLua("if beamngEarth_map3d and beamngEarth_map3d.setMarker then beamngEarth_map3d.setMarker(nil) end")
  onUp()
})
</script>

<style scoped>
/* (an almost invisible background: the game's UI layer hands the mouse to the game wherever the page is fully
   transparent, so a transparent screen never saw a drag or the wheel; the user, 2026-11-07) */
.earth-map3d { position: absolute; inset: 0; outline: none; user-select: none; cursor: grab; font-family: inherit;
  background: rgba(0, 0, 0, 0.01); }
.earth-map3d:active { cursor: grabbing; }
/* the labels: white with a soft dark halo, as map apps write on the photo; no boxes */
.m3-labels { position: absolute; inset: 0; pointer-events: none; overflow: hidden; }
.m3-labels :deep(.lb) { position: absolute; left: 0; top: 0; white-space: nowrap; color: #fff; opacity: 0;
  display: flex; align-items: center; gap: 4px; transition: opacity 0.3s ease; will-change: transform, opacity;
  text-shadow: 0 0 2px rgba(0, 0, 0, 0.95), 0 0 5px rgba(0, 0, 0, 0.55), 0 1px 2px rgba(0, 0, 0, 0.8); }
.m3-labels :deep(.lb-country) { color: #fff; }
.m3-labels :deep(.lb-continent) { color: #fff; }
.m3-labels :deep(.lb-state) { color: rgba(255, 255, 255, 0.85); }
.m3-labels :deep(.lb-water) { color: #a8dcf5; text-shadow: 0 0 3px rgba(0, 30, 60, 0.9), 0 1px 2px rgba(0, 0, 0, 0.6); }
.m3-labels :deep(.lb-land) { color: #efe3cf; }
.m3-labels :deep(.lb-district) { color: rgba(255, 255, 255, 0.92); }
.m3-labels :deep(.lb-street) { color: #fff; }
.m3-labels :deep(.lb-dot) { width: 6px; height: 6px; border-radius: 50%; background: #fff; border: 1.5px solid #333;
  box-shadow: 0 0 2px rgba(0, 0, 0, 0.8); flex: none; }
.m3-labels :deep(.lb-big .lb-dot) { width: 8px; height: 8px; }
.m3-labels :deep(.lb-icon) { width: 17px; height: 17px; border-radius: 50%; display: flex; align-items: center;
  justify-content: center; flex: none; box-shadow: 0 0 0 1.5px rgba(255, 255, 255, 0.9), 0 1px 3px rgba(0, 0, 0, 0.6); }
.m3-labels :deep(.lb-icon svg) { width: 11px; height: 11px; fill: #fff; }
/* the 2D map's (EarthMap.vue): copied so both screens look alike */
.confirm { bottom: calc(6.5vh + 48px) !important; }
.map-top {
  position: absolute; left: 12px; right: 12px; top: 12px; display: flex; gap: 8px; align-items: flex-start; z-index: 10;
}
.map-title {
  padding: 7px 12px; background: rgba(0, 0, 0, 0.7); border-radius: 4px; font-weight: 600; white-space: nowrap;
}
.weather-badge {
  display: flex; gap: 7px; align-items: center; padding: 7px 12px; background: rgba(0, 0, 0, 0.7);
  border-radius: 4px; white-space: nowrap; font-size: 13px;
}
.search { position: relative; flex: 1; max-width: 460px; }
.search-results {
  position: absolute; left: 0; right: 0; top: 36px; background: rgba(20, 22, 26, 0.95); border-radius: 4px; overflow: hidden;
}
.search-result { padding: 6px 10px; display: flex; justify-content: space-between; gap: 10px; cursor: pointer; }
.search-result:hover { background: rgba(255, 154, 60, 0.35); }
.result-coords { opacity: 0.6; font-size: 12px; }
.search-msg { margin-top: 4px; padding: 5px 10px; font-size: 12px; background: rgba(0, 0, 0, 0.75); border-radius: 4px; }
.map-btn {
  padding: 7px 12px; min-width: 34px; font-size: 14px; color: #fff; cursor: pointer;
  background: rgba(0, 0, 0, 0.7); border: 1px solid rgba(255, 255, 255, 0.3); border-radius: 4px;
}
.map-btn:hover { background: rgba(60, 60, 60, 0.9); }
.map-btn.on { border-color: #36a3ff; color: #9fd3ff; }
.map-btn.primary { background: #e0761c; border-color: #e0761c; }
.map-btn:disabled { opacity: 0.45; cursor: default; }
.map-btn.close { margin-left: auto; }
.layers-wrap { position: relative; }
.layers-panel {
  position: absolute; right: 0; top: 38px; z-index: 12; min-width: 190px; padding: 8px 12px 10px;
  background: rgba(15, 18, 22, 0.94); border: 1px solid rgba(255, 255, 255, 0.2); border-radius: 6px;
  box-shadow: 0 3px 12px rgba(0, 0, 0, 0.5); cursor: default;
}
.map-btn .following { display: inline-block; max-width: 180px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  vertical-align: bottom; }
.map-btn.small { padding: 4px 8px; font-size: 12px; }
.map-btn.danger { background: #a3302a; border-color: #a3302a; }
.panel-sub { margin: 10px 0 5px; font-size: 12px; text-transform: uppercase; letter-spacing: 0.05em; color: #9aa4ae; }
.panel-sub:first-child { margin-top: 0; }
.panel-item {
  display: flex; gap: 8px; align-items: center; padding: 6px 4px; border-bottom: 1px solid rgba(255, 255, 255, 0.08);
}
.item-text { flex: 1; min-width: 0; cursor: pointer; }
.item-text:hover .item-name { color: #ffc28a; }
.item-name { font-size: 13px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.item-sub { font-size: 11px; color: #9aa4ae; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.item-btns { display: flex; gap: 4px; flex-shrink: 0; }
.panel-empty { padding: 6px 4px; font-size: 12px; color: #9aa4ae; }
.search-result.active { background: rgba(224, 118, 28, 0.6); }
.search-more .result-name { color: #ff9a3c; }
.search-credit { padding: 4px 10px; font-size: 10px; color: #8a949e; border-top: 1px solid rgba(255, 255, 255, 0.08); }
.ctx-menu {
  position: absolute; z-index: 10; min-width: 190px; padding: 4px 0; background: rgba(15, 18, 22, 0.95);
  border: 1px solid rgba(255, 255, 255, 0.2); border-radius: 5px; box-shadow: 0 3px 12px rgba(0, 0, 0, 0.5);
}
.ctx-where { padding: 4px 12px 6px; font-size: 11px; color: #9aa4ae; border-bottom: 1px solid rgba(255, 255, 255, 0.1); }
.ctx-item { padding: 7px 12px; font-size: 13px; cursor: pointer; }
.ctx-item:hover { background: rgba(224, 118, 28, 0.85); }
.ctx-item.disabled { opacity: 0.45; cursor: default; }
.ctx-item.disabled:hover { background: none; }
.gps-stack {
  position: absolute; left: 10px; top: 56px; z-index: 6; display: flex; flex-direction: column; gap: 6px;
  align-items: flex-start;
}
.gps-chip {
  display: flex; flex-direction: column; gap: 6px;
  padding: 5px 8px 5px 12px; font-size: 13px; background: rgba(15, 18, 22, 0.88); border: 1px solid #36a3ff; border-radius: 5px;
}
.gps-chip-row { display: flex; gap: 8px; align-items: center; }
.gps-dist { color: #9fd3ff; }
.gps-warn { color: #ffb08a; font-size: 12px; }
.map-notice {
  position: absolute; left: 50%; top: 60px; transform: translateX(-50%); z-index: 9;
  padding: 7px 14px; background: rgba(120, 40, 30, 0.85); border-radius: 4px; font-size: 13px;
}
.map-toast {
  position: absolute; left: 50%; bottom: calc(6.5vh + 40px); transform: translateX(-50%); z-index: 13;
  padding: 6px 14px; background: rgba(20, 22, 26, 0.9); border-radius: 4px; font-size: 13px; pointer-events: none;
}
.confirm {
  position: absolute; left: 50%; bottom: 56px; transform: translateX(-50%); z-index: 11; min-width: 300px;
  padding: 12px 16px; background: rgba(15, 17, 20, 0.92); border: 1px solid rgba(255, 255, 255, 0.25); border-radius: 6px;
}
.confirm-title { font-size: 16px; font-weight: 600; margin-bottom: 4px; }
.confirm-where { font-size: 13px; opacity: 0.85; }
.confirm-warn { margin-top: 6px; font-size: 12px; color: #ffb27a; }
.confirm-btns { display: flex; gap: 8px; margin-top: 10px; }
.search-input {
  width: 100%; box-sizing: border-box; padding: 7px 10px; font-size: 14px; color: #fff;
  background: rgba(0, 0, 0, 0.75); border: 1px solid rgba(255, 255, 255, 0.3); border-radius: 4px; outline: none;
}
.search-input:focus { border-color: #ff9a3c; }
.search-input:focus { border-color: #ff9a3c; }
.gps-stack {
  position: absolute; left: 10px; top: 56px; z-index: 6; display: flex; flex-direction: column; gap: 6px;
  align-items: flex-start;
}
.gps-chip {
  display: flex; flex-direction: column; gap: 6px;
  padding: 5px 8px 5px 12px; font-size: 13px; background: rgba(15, 18, 22, 0.88); border: 1px solid #36a3ff; border-radius: 5px;
}
.gps-chip {
  display: flex; flex-direction: column; gap: 6px;
  padding: 5px 8px 5px 12px; font-size: 13px; background: rgba(15, 18, 22, 0.88); border: 1px solid #36a3ff; border-radius: 5px;
}
.elev-box {
  display: flex; flex-direction: column; gap: 4px; padding: 5px 8px 6px 10px;
  background: rgba(15, 18, 22, 0.88); border: 1px solid #2a3a4a; border-radius: 5px;
}
.gps-chip-row { display: flex; gap: 8px; align-items: center; }
.gps-dist { color: #9fd3ff; }
.gps-warn { color: #ffb08a; font-size: 12px; }
.confirm {
  position: absolute; left: 50%; bottom: 56px; transform: translateX(-50%); z-index: 11; min-width: 300px;
  padding: 12px 16px; background: rgba(15, 17, 20, 0.92); border: 1px solid rgba(255, 255, 255, 0.25); border-radius: 6px;
}
.confirm-title { font-size: 16px; font-weight: 600; margin-bottom: 4px; }
.confirm-title { font-size: 16px; font-weight: 600; margin-bottom: 4px; }
.confirm-where { font-size: 13px; opacity: 0.85; }
.confirm-warn { margin-top: 6px; font-size: 12px; color: #ffb27a; }
.confirm-btns { display: flex; gap: 8px; margin-top: 10px; }
.ctx-menu {
  position: absolute; z-index: 10; min-width: 190px; padding: 4px 0; background: rgba(15, 18, 22, 0.95);
  border: 1px solid rgba(255, 255, 255, 0.2); border-radius: 5px; box-shadow: 0 3px 12px rgba(0, 0, 0, 0.5);
}
.ctx-where { padding: 4px 12px 6px; font-size: 11px; color: #9aa4ae; border-bottom: 1px solid rgba(255, 255, 255, 0.1); }
.ctx-where { padding: 4px 12px 6px; font-size: 11px; color: #9aa4ae; border-bottom: 1px solid rgba(255, 255, 255, 0.1); }
.ctx-item { padding: 7px 12px; font-size: 13px; cursor: pointer; }
.ctx-item:hover { background: rgba(224, 118, 28, 0.85); }
.ctx-item.disabled { opacity: 0.45; cursor: default; }
.ctx-item.disabled:hover { background: none; }
.map-toast {
  position: absolute; left: 50%; bottom: calc(6.5vh + 40px); transform: translateX(-50%); z-index: 13;
  padding: 6px 14px; background: rgba(20, 22, 26, 0.9); border-radius: 4px; font-size: 13px; pointer-events: none;
}
.confirm {
  position: absolute; left: 50%; bottom: 56px; transform: translateX(-50%); z-index: 11; min-width: 300px;
  padding: 12px 16px; background: rgba(15, 17, 20, 0.92); border: 1px solid rgba(255, 255, 255, 0.25); border-radius: 6px;
}
/* (clear of the game's own corner icons, top right, and its bar at the bottom: the screen is see-through) */
.map-top { color: #eee; right: 90px; }
.m3-mode { margin-left: 6px; padding: 1px 6px; font-size: 11px; font-weight: 600; color: #1d2228; background: #ffb347;
  border-radius: 3px; vertical-align: 1px; }
.m3-row { display: flex; gap: 6px; align-items: center; margin-bottom: 4px; }
.m3-places { min-width: 280px; max-height: 60vh; overflow-y: auto; }
.m3-place { position: absolute; left: 12px; bottom: calc(6.5vh + 12px); padding: 6px 12px; color: #fff; background: rgba(0, 0, 0, 0.6);
  border-radius: 5px; min-width: 220px; }
.m3-name { font-size: 15px; font-weight: 600; }
.m3-sub { font-size: 12px; opacity: 0.8; margin-top: 2px; }
.m3-msg { position: absolute; left: 50%; bottom: calc(6.5vh + 48px); transform: translateX(-50%); padding: 7px 14px; color: #fff;
  background: rgba(15, 17, 20, 0.92); border: 1px solid rgba(255, 255, 255, 0.25); border-radius: 6px; }
.gps-stack, .ctx-menu, .confirm, .map-toast { color: #eee; }
.m3-compass { position: absolute; right: 24px; bottom: calc(6.5vh + 60px); width: 52px; height: 52px; border-radius: 50%; cursor: pointer;
  background: rgba(0, 0, 0, 0.6); border: 1px solid rgba(255, 255, 255, 0.3); display: flex; align-items: center; justify-content: center; }
.m3-needle { width: 100%; height: 100%; transition: transform 0.1s linear; overflow: visible; }
.m3-zoom { position: absolute; right: 24px; bottom: 190px; display: flex; flex-direction: column; gap: 6px; }
.m3-help { position: absolute; left: 50%; bottom: calc(6.5vh + 6px); transform: translateX(-50%); padding: 4px 12px; font-size: 13px; color: #fff;
  background: rgba(0, 0, 0, 0.5); border-radius: 4px; }
.m3-cross { position: absolute; left: 50%; top: 50%; width: 14px; height: 14px; margin: -7px 0 0 -7px; pointer-events: none;
  border: 2px solid rgba(255, 255, 255, 0.85); border-radius: 50%; box-shadow: 0 0 3px rgba(0, 0, 0, 0.8); }
</style>
