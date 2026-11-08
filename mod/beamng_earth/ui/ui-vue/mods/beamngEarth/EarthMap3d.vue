<template>
  <!-- the 3D map screen (map3d.lua): transparent over the game; the mouse moves the orbit camera (the UI takes it from
       the camera, as the vanilla big map's screen), the controls as the 2D map's -->
  <div class="earth-map3d" tabindex="-1" ref="rootEl" @keydown.esc.stop="back"
       @keydown="onKey($event, true)" @keyup="onKey($event, false)"
       @mousedown="onDown" @wheel.prevent="onWheel" @contextmenu.prevent>
    <!-- the labels (map3dLabels.js: projected each frame from map3d.lua's camera, no overlaps, fading) -->
    <div class="m3-labels" ref="labelsEl"></div>
    <div class="m3-top" @mousedown.stop>
      <div class="m3-place">
        <div class="m3-name">{{ place || "—" }}</div>
        <div class="m3-sub">{{ latlon }} &middot; {{ height }}</div>
      </div>
      <div class="m3-buttons">
        <button class="map-btn" @click="to2d" title="The flat world map">2D map</button>
        <button class="map-btn primary" @click="driveHere" :disabled="busy" title="Teleport the car to the middle of the view">Drive here</button>
        <button class="map-btn" @click="back" title="Fly back down to the car (Esc, or the map key)">Back to car</button>
      </div>
    </div>
    <div class="m3-msg" v-if="msg" @mousedown.stop>{{ msg }}</div>
    <!-- the 2D map's tools here too (the user, 2026-11-07: "bring more elements from the 2d map. saved places, search,
         gps"): search (a smooth flight there), places, a GPS route to the view -->
    <div class="m3-tools" @mousedown.stop @wheel.stop>
      <div class="m3-search">
        <input v-model="query" class="m3-input" placeholder="Search places and addresses" maxlength="120"
               @focus="typing(true)" @blur="typing(false)" @keydown.stop="onSearchKey" />
        <button class="map-btn" :class="{ active: panel === 'places' }" @click="togglePlaces" title="Saved and recent places">&#9733;</button>
      </div>
      <div class="m3-list" v-if="results.length || searching || searchMsg">
        <div v-if="searching" class="m3-note">Searching&hellip;</div>
        <div v-else-if="searchMsg && !results.length" class="m3-note">{{ searchMsg }}</div>
        <div v-for="(r, i) in results" :key="'r' + i" class="m3-item" @click="goResult(r)">
          <div class="m3-item-name">{{ r.name }}</div>
          <div class="m3-item-sub">{{ r.detail || fmt(r.lat, r.lon) }}</div>
        </div>
      </div>
      <div class="m3-list" v-else-if="panel === 'places'">
        <div class="m3-row">
          <input v-model="saveName" class="m3-input small" placeholder="Name for this view" maxlength="60"
                 @focus="typing(true)" @blur="typing(false)" @keydown.stop @keydown.enter="saveView" />
          <button class="map-btn" @click="saveView">Save</button>
        </div>
        <div v-if="placesMsg" class="m3-note">{{ placesMsg }}</div>
        <div v-for="p in savedPlaces" :key="'p' + p.id" class="m3-item" @click="goPlace(p)">
          <div class="m3-item-name">&#9733; {{ p.name }}</div>
          <div class="m3-item-sub">{{ p.text || fmt(p.lat, p.lon) }}</div>
        </div>
        <div v-if="recentPlaces.length" class="m3-head">Recent</div>
        <div v-for="(p, i) in recentPlaces" :key="'c' + i" class="m3-item" @click="goPlace(p)">
          <div class="m3-item-name">{{ p.name || p.text }}</div>
          <div class="m3-item-sub">{{ p.name ? p.text : "" }}</div>
        </div>
      </div>
      <div class="m3-gps">
        <button class="map-btn" @click="routeHere" title="Plan a GPS route from the car to the middle of the view">Route here</button>
        <button class="map-btn" v-if="st.gps" @click="clearRoute" title="Stop the GPS route">Clear route</button>
        <span v-if="st.gps && st.gps.text" class="m3-gps-text">{{ st.gps.text }}</span>
      </div>
    </div>
    <div class="m3-compass" @mousedown.stop @click="north" title="Face north">
      <div class="m3-needle" :style="{ transform: `rotate(${-headingDeg}deg)` }">N</div>
    </div>
    <div class="m3-zoom" @mousedown.stop>
      <button class="map-btn" @click="zoom(-3)" title="Zoom in">+</button>
      <button class="map-btn" @click="zoom(3)" title="Zoom out">&minus;</button>
    </div>
    <div class="m3-help" @mousedown.stop>Drag: move &middot; Right drag: turn and tilt &middot; Wheel: zoom &middot; WASD / arrows: move &middot; Q E: turn &middot; R F: tilt</div>
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
function onSearchKey(e) {
  if (e.key === "Enter") { if (results.value.length) goResult(results.value[0]); else runSearch(true) }
  else if (e.key === "Escape") { query.value = ""; results.value = []; e.target.blur() }
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

function onResize() { engineLua(`beamngEarth_map3d.setScreen(${window.innerHeight})`) }
const st = ref({})
const place = ref("")
const busy = ref(false)
const msg = ref("")
let timer = null
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
  drag = { x: e.clientX, y: e.clientY, mode: e.button === 2 || e.button === 1 ? "orbit" : "pan" }
  window.addEventListener("mousemove", onMove)
  window.addEventListener("mouseup", onUp)
}

function onMove(e) {
  if (!drag) return
  const dx = e.clientX - drag.x, dy = e.clientY - drag.y
  drag.x = e.clientX
  drag.y = e.clientY
  if (dx || dy) engineLua(`beamngEarth_map3d.drag(${dx}, ${dy}, '${drag.mode}', ${window.innerHeight})`)
}

function onUp() {
  drag = null
  window.removeEventListener("mousemove", onMove)
  window.removeEventListener("mouseup", onUp)
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
function north() { engineLua("beamngEarth_map3d.setHeading(0)") }
function back() { engineLua("beamngEarth_map3d.exit()") }
function to2d() { engineLua("beamngEarth_map3d.to2d()") }

function driveHere() {
  busy.value = true
  msg.value = ""
  engineLua("beamngEarth_map3d.driveHere()", r => {
    if (!r || !r.ok) {
      busy.value = false
      msg.value = (r && r.error) || "Teleport failed"
    } else if (r.pending) {
      // (the map stays open over the place while it loads, then flies down to the car: map3d.driveStep)
      msg.value = "Loading the place, then down to the car..."
    } else busy.value = false
  })
}

onMounted(() => {
  layer = new LabelLayer(labelsEl.value)
  onResize()
  window.addEventListener("resize", onResize)
  // (the labels the map has already: sent again for this screen)
  engineLua("if beamngEarth_map3d and beamngEarth_map3d.resendLabels then beamngEarth_map3d.resendLabels() end")
  poll()
  timer = setInterval(poll, 250)
  nextTick(() => rootEl.value && rootEl.value.focus())
})

onUnmounted(() => {
  window.removeEventListener("resize", onResize)
  if (layer) layer.destroy()
  layer = null
  clearInterval(timer)
  if (keyTimer) clearInterval(keyTimer)
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
.m3-top { position: absolute; top: 12px; left: 12px; right: 12px; display: flex; align-items: flex-start; gap: 12px; }
.m3-place { padding: 8px 14px; color: #fff; background: rgba(0, 0, 0, 0.6); border-radius: 6px; min-width: 260px; }
.m3-name { font-size: 18px; font-weight: 600; }
.m3-sub { font-size: 13px; opacity: 0.8; margin-top: 2px; }
.m3-buttons { margin-left: auto; display: flex; gap: 8px; }
.m3-msg { position: absolute; top: 80px; left: 12px; padding: 6px 12px; color: #fff; background: rgba(163, 48, 42, 0.9); border-radius: 4px; }
.m3-compass { position: absolute; right: 24px; bottom: 120px; width: 52px; height: 52px; border-radius: 50%; cursor: pointer;
  background: rgba(0, 0, 0, 0.6); border: 1px solid rgba(255, 255, 255, 0.3); display: flex; align-items: center; justify-content: center; }
.m3-needle { color: #ff6b5b; font-weight: 700; font-size: 16px; transition: transform 0.1s linear; }
.m3-zoom { position: absolute; right: 24px; bottom: 190px; display: flex; flex-direction: column; gap: 6px; }
.m3-help { position: absolute; left: 50%; bottom: 14px; transform: translateX(-50%); padding: 4px 12px; font-size: 13px; color: #fff;
  background: rgba(0, 0, 0, 0.5); border-radius: 4px; }
.m3-tools { position: absolute; top: 86px; left: 12px; width: 340px; display: flex; flex-direction: column; gap: 6px; }
.m3-search, .m3-row { display: flex; gap: 6px; }
.m3-input { flex: 1; padding: 7px 10px; font-size: 14px; color: #fff; background: rgba(0, 0, 0, 0.7);
  border: 1px solid rgba(255, 255, 255, 0.3); border-radius: 4px; outline: none; }
.m3-input:focus { border-color: #e0761c; }
.m3-list { max-height: 50vh; overflow-y: auto; background: rgba(0, 0, 0, 0.72); border-radius: 6px; padding: 4px; }
.m3-item { padding: 6px 8px; border-radius: 4px; cursor: pointer; color: #fff; }
.m3-item:hover { background: rgba(255, 255, 255, 0.12); }
.m3-item-name { font-size: 14px; font-weight: 600; }
.m3-item-sub { font-size: 12px; opacity: 0.7; }
.m3-note, .m3-head { padding: 6px 8px; font-size: 12px; color: rgba(255, 255, 255, 0.75); }
.m3-head { text-transform: uppercase; letter-spacing: 0.1em; }
.m3-gps { display: flex; gap: 6px; align-items: center; }
.m3-gps-text { font-size: 12px; color: #fff; background: rgba(0, 0, 0, 0.6); padding: 4px 8px; border-radius: 4px; }
.map-btn.active { background: #e0761c; border-color: #e0761c; }
.m3-cross { position: absolute; left: 50%; top: 50%; width: 14px; height: 14px; margin: -7px 0 0 -7px; pointer-events: none;
  border: 2px solid rgba(255, 255, 255, 0.85); border-radius: 50%; box-shadow: 0 0 3px rgba(0, 0, 0, 0.8); }
.map-btn {
  padding: 7px 12px; min-width: 34px; font-size: 14px; color: #fff; cursor: pointer;
  background: rgba(0, 0, 0, 0.7); border: 1px solid rgba(255, 255, 255, 0.3); border-radius: 4px;
}
.map-btn:hover { background: rgba(60, 60, 60, 0.9); }
.map-btn.primary { background: #e0761c; border-color: #e0761c; }
.map-btn:disabled { opacity: 0.45; cursor: default; }
</style>
