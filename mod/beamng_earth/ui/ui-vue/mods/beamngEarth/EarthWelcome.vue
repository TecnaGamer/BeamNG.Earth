<template>
  <!-- A one-time welcome (the user, 2026-10-15: "add a 1 time popup with info about this project"): what it is,
       the companion server, the keys, credits. Shown once per WELCOME_VERSION by settings.lua; again from the
       settings page's "Show welcome". -->
  <div class="earth-welcome" @keydown.esc.stop="done" tabindex="-1" ref="rootEl">
    <div class="card">
      <div class="head">
        <svg class="globe" viewBox="0 0 48 48" aria-hidden="true">
          <circle cx="24" cy="24" r="21" fill="#2a6fb0" stroke="#9fd3ff" stroke-width="2"/>
          <ellipse cx="24" cy="24" rx="9" ry="21" fill="none" stroke="#9fd3ff" stroke-width="1.6"/>
          <path d="M3 24h42M7 13h34M7 35h34" stroke="#9fd3ff" stroke-width="1.6" fill="none"/>
          <path d="M14 15c4 1 6 4 5 8s-5 4-6 9M30 12c-2 4 1 7 5 8s5 6 3 10" stroke="#7fd08a" stroke-width="3" fill="none"
                stroke-linecap="round"/>
        </svg>
        <div>
          <div class="title">Welcome to BeamNG.Earth</div>
          <div class="sub">Drive anywhere on the real Earth.</div>
        </div>
      </div>

      <div class="server" :class="{ ok: connected, bad: connected === false }">
        <span class="dot"></span>
        <span v-if="connected">World server connected: the world is building around you.</span>
        <span v-else-if="connected === false">World server not running. Start it with <b>run_server</b> (Windows)
          or <b>run_server.sh</b> (Linux) from the BeamNG.Earth folder, then come back.</span>
        <span v-else>Checking the world server…</span>
      </div>

      <div class="cols">
        <div class="col">
          <div class="h">How it works</div>
          <p>A small companion program on your PC builds the world as you drive: real terrain, roads, buildings,
            trees, water and aerial photos, from open map data. Keep it running while you play.</p>
          <p>A place you visit for the first time takes a few seconds to appear; after that it comes from the cache.</p>
          <div class="h">Getting around</div>
          <ul>
            <li><b>M</b>: the world map. Search an address, right-click to teleport, left-click to set a GPS route.</li>
            <li><b>Autodrive</b> on the GPS bar: the car drives the route by itself.</li>
            <li><b>Places</b> on the map: save spots and jump back to them.</li>
          </ul>
        </div>
        <div class="col">
          <div class="h">Help make it better</div>
          <ul>
            <li><b>Ctrl+Shift+E</b>: report a problem here (a screenshot, the place and your frame rate are saved as a
              ready-to-post issue).</li>
            <li><b>Ctrl+Shift+Y</b>: record a 30-second performance profile.</li>
          </ul>
          <div class="h">Settings</div>
          <p>Pause menu → Mods → <b>BeamNG.Earth</b>: view distance, weather and sky, grass, the download cache.</p>
          <div class="h">Data</div>
          <p class="small">Imagery, elevation and map data from open sources: USGS, USDA NAIP, state and national
            mapping agencies, EOX Sentinel-2 cloudless, Overture Maps / OpenStreetMap contributors, Mapterhorn.
            The sources in use are credited on screen while you drive.</p>
          <p class="small preview">A beta: expect some rough edges. Thanks for trying it!</p>
        </div>
      </div>

      <!-- The telemetry choice (the user, 2026-11-08: "give all players a popup and let them choose before playing";
           TELEMETRY.md): nothing is sent until a button below saves it. The boxes start as this computer's region says
           (settings.uiPrivacy: nothing ticked in the EU / EEA and a few other places, performance unticked in some). -->
      <div class="privacy" :class="{ ask: privacy.needed }">
        <div class="h">Anonymous statistics<span v-if="privacy.needed" class="badge">your choice</span></div>
        <p class="small">BeamNG.Earth can send small anonymous reports to our server every 5 minutes. They never say who
          or where you are: no name, account, address, coordinates or places. They show how many people play, keep the
          free map services from being overloaded, and tell us what makes the game slow or crash. Choose what to send;
          you can change it any time in settings.
          <a href="#" @click.prevent="openLink(DOC_URL)">Everything we collect and why</a></p>
        <label class="tick"><input type="checkbox" @change="touch" v-model="privacy.telemetry"/>
          <span><b>Usage statistics</b>: a random install id, the app version, your operating system, whether the game
            and BeamMP are running. Unticked: nothing at all is sent.</span></label>
        <label class="tick sub" :class="{ off: !privacy.telemetry }">
          <input type="checkbox" @change="touch" v-model="privacy.perf" :disabled="!privacy.telemetry"/>
          <span><b>Performance data</b>: frame rate and hitches, world-building speed, your CPU / GPU model and
            settings, the kind of area (city, rural, ...) and error codes.</span></label>
        <label class="tick sub" :class="{ off: !privacy.telemetry }">
          <input type="checkbox" @change="touch" v-model="privacy.net" :disabled="!privacy.telemetry"/>
          <span><b>Network load</b>: how many requests went to each map-data service.</span></label>
      </div>

      <div class="btns">
        <button class="btn" @click="openMap">Open the map</button>
        <button class="btn primary" @click="done">Start driving</button>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted, onUnmounted, nextTick } from "vue"
import { openLink } from "./earthSettingsStore.js"

const DOC_URL = "https://github.com/TecnaGamer/BeamNG.Earth/blob/main/TELEMETRY.md"
const rootEl = ref(null)
const connected = ref(null)
const privacy = reactive({ needed: false, telemetry: false, perf: false, net: false })
let timer = null

function engineLua(script, cb) {
  if (window.bngApi && window.bngApi.engineLua) window.bngApi.engineLua(script, cb)
}
// the boxes as settings.uiPrivacy says; asked again each second until the server's hello named the region, unless
// the player already clicked one
let privacyKnown = false, privacyTouched = false
function touch() { privacyTouched = true }
function loadPrivacy() {
  if (privacyKnown || privacyTouched) return
  engineLua("beamngEarth_settings and beamngEarth_settings.uiPrivacy and beamngEarth_settings.uiPrivacy() or nil", p => {
    if (!p || privacyTouched) return
    privacyKnown = !!p.known
    privacy.needed = !!p.needed
    privacy.telemetry = p.telemetry === true
    privacy.perf = p.perf === true
    privacy.net = p.net === true
  })
}
function poll() {
  engineLua("beamngEarth_settings and beamngEarth_settings.uiMapState and beamngEarth_settings.uiMapState() or nil", s => {
    connected.value = s ? !!s.connected : false
  })
}
// marks the welcome seen, saves the telemetry choice and goes back to driving (or to the map)
function finish(state) {
  const t = privacy.telemetry === true
  const choice = `{telemetry = ${t}, perf = ${t && privacy.perf === true}, net = ${t && privacy.net === true}}`
  engineLua(`if beamngEarth_settings and beamngEarth_settings.uiWelcomeDone then beamngEarth_settings.uiWelcomeDone(${choice}) end; ` +
            `if beamngEarth_settings and beamngEarth_settings.navigate then beamngEarth_settings.navigate('${state}') ` +
            `else guihooks.trigger('ChangeState', {state = '${state}'}) end`)
}
function done() { finish("play") }
function openMap() { finish("beamngEarthMap") }

onMounted(() => {
  poll()
  loadPrivacy()
  timer = setInterval(() => { poll(); loadPrivacy() }, 1000)
  nextTick(() => rootEl.value && rootEl.value.focus())
})
onUnmounted(() => clearInterval(timer))
</script>

<style scoped>
.earth-welcome {
  position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
  background: rgba(8, 10, 14, 0.62); outline: none; font-family: inherit; color: #e8edf2;
}
.card {
  width: min(980px, 92vw); max-height: 88vh; overflow-y: auto; padding: 26px 30px 22px;
  background: rgba(18, 22, 28, 0.96); border: 1px solid rgba(255, 255, 255, 0.14); border-radius: 10px;
  box-shadow: 0 18px 60px rgba(0, 0, 0, 0.55);
}
.head { display: flex; gap: 16px; align-items: center; margin-bottom: 16px; }
.globe { width: 48px; height: 48px; flex: none; }
.title { font-size: 28px; font-weight: 700; letter-spacing: 0.01em; }
.sub { font-size: 15px; color: #9fb0c0; margin-top: 2px; }
.server {
  display: flex; gap: 10px; align-items: center; padding: 9px 12px; margin-bottom: 18px; font-size: 14px;
  border-radius: 6px; background: rgba(255, 255, 255, 0.05);
}
.server .dot { width: 10px; height: 10px; border-radius: 50%; background: #8a949e; flex: none; }
.server.ok .dot { background: #4cc46a; }
.server.bad { background: rgba(224, 118, 28, 0.14); }
.server.bad .dot { background: #e0761c; }
.cols { display: flex; gap: 28px; }
.col { flex: 1; min-width: 0; }
.h { margin: 14px 0 6px; font-size: 12px; text-transform: uppercase; letter-spacing: 0.06em; color: #9fb0c0; }
.h:first-child { margin-top: 0; }
p { margin: 0 0 8px; font-size: 14px; line-height: 1.45; }
ul { margin: 0 0 8px; padding-left: 18px; font-size: 14px; line-height: 1.45; }
li { margin-bottom: 4px; }
.small { font-size: 12px; color: #aab6c2; }
.preview { color: #ffc28a; }
.privacy { margin-top: 16px; padding: 12px 14px 8px; border-radius: 6px; background: rgba(255, 255, 255, 0.04); }
.privacy.ask { border: 1px solid rgba(224, 118, 28, 0.55); }
.privacy .h { margin-top: 0; }
.privacy a { color: #9fd3ff; }
.badge { margin-left: 8px; padding: 1px 6px; border-radius: 3px; background: #e0761c; color: #fff; font-size: 10px; }
.tick { display: flex; gap: 9px; align-items: flex-start; margin: 0 0 7px; font-size: 13px; line-height: 1.4;
  cursor: pointer; }
.tick input { margin-top: 3px; flex: none; }
.tick.sub { margin-left: 24px; }
.tick.off { opacity: 0.45; }
.btns { display: flex; justify-content: flex-end; gap: 10px; margin-top: 18px; }
.btn {
  padding: 9px 18px; font-size: 14px; color: #fff; background: rgba(255, 255, 255, 0.08);
  border: 1px solid rgba(255, 255, 255, 0.22); border-radius: 5px; cursor: pointer;
}
.btn:hover { background: rgba(255, 255, 255, 0.16); }
.btn.primary { background: #e0761c; border-color: #e0761c; }
.btn.primary:hover { background: #f08a32; }
@media (max-width: 760px) { .cols { flex-direction: column; gap: 4px; } }
</style>
