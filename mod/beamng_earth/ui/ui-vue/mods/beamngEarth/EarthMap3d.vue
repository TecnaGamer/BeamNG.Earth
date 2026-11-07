<template>
  <!-- the 3D map screen (map3d.lua): transparent over the game; the mouse moves the orbit camera (the UI takes it from
       the camera, as the vanilla big map's screen), the controls as the 2D map's -->
  <div class="earth-map3d" tabindex="-1" ref="rootEl" @keydown.esc.stop="back"
       @keydown="onKey($event, true)" @keyup="onKey($event, false)"
       @mousedown="onDown" @wheel.prevent="onWheel" @contextmenu.prevent>
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
import { ref, computed, onMounted, onUnmounted, nextTick } from "vue"

const rootEl = ref(null)
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
    busy.value = false
    if (!r || !r.ok) msg.value = (r && r.error) || "Teleport failed"
  })
}

onMounted(() => {
  poll()
  timer = setInterval(poll, 250)
  nextTick(() => rootEl.value && rootEl.value.focus())
})

onUnmounted(() => {
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
