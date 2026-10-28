<template>
  <div class="earth-bng-options">

    <!-- Master Header: connection / session status -->
    <div class="options-header">
      <div class="header-title">BeamNG.Earth</div>
      <button class="header-welcome" @click="showWelcome" title="What BeamNG.Earth is, the keys, the credits">Welcome page</button>
      <div class="header-status" :class="{ 'is-connected': status.connection === 'connected' }">
        {{ connectionText }}
      </div>
    </div>

    <!-- Connection state: the same line as the on-screen banner (banner.lua; docs/ROADMAP.md "Robustness") -->
    <div v-if="bannerText" class="options-presets state-bar" :class="'state-' + bannerLevel">
      <span class="state-text">{{ bannerText }}</span>
    </div>

    <!-- A newer version on GitHub (server updates.py) -->
    <div v-if="update" class="options-presets state-bar state-ok update-bar">
      <span class="state-text">Update available: BeamNG.Earth {{ update.latest }} (you have {{ update.current }})</span>
      <button class="update-link" @click="openUpdate">Download page</button>
    </div>

    <!-- Status line -->
    <div class="options-presets status-bar">
      <span class="status-text">{{ statusText }}</span>
    </div>

    <!-- Main Scrollable List -->
    <div class="options-list-scroll">

      <details
        v-for="(category, catIndex) in settingsSchema"
        :key="category.name"
        class="category-block"
        :open="catIndex === 0"
      >
        <!-- Collapsible Category Header -->
        <summary class="category-title"><span class="category-arrow"></span>{{ category.name }}</summary>

        <div class="category-items">

          <!-- Dev tools presets (session only) -->
          <div v-if="category.devPresets" class="options-presets dev-presets">
            <BngButton
              v-for="p in devPresets"
              :key="p.id"
              class="preset-btn"
              :accent="dev.lastPreset === p.id && dev.active ? ACCENTS.main : ACCENTS.outlined"
              @click="applyDevPreset(p.id)"
            >{{ p.label }}</BngButton>
          </div>

          <div
            v-for="item in category.items"
            :key="item.id"
            class="options-item-row"
            @mouseenter="hoveredItem = item"
            @mouseleave="hoveredItem = null"
          >
            <!-- Top Half: Label & Switches -->
            <div class="row-header">
              <div class="option-label">{{ item.name }}</div>

              <!-- Read-only info -->
              <div v-if="item.type === 'info'" class="info-value">{{ infoValue(item) }}</div>

              <!-- Inline Switch for Booleans (persisted settings) -->
              <div v-if="item.type === 'bool'" class="inline-controls">
                <BngSwitch
                  :modelValue="config[item.id] === true"
                  @update:modelValue="val => onSwitchChange(item, val)"
                  :disabled="item.disabled"
                />
                <div class="option-reset">
                  <BngButton
                    :icon="icons.undo"
                    :accent="ACCENTS.outlined"
                    @click="resetSetting(item)"
                    class="bng-reset-btn"
                    :style="{ visibility: config[item.id] !== item.default ? 'visible' : 'hidden' }"
                    title="Reset to default"
                  />
                </div>
              </div>

              <!-- A button that acts at once (Clear cache: asks first, below) -->
              <div v-if="item.type === 'action'" class="inline-controls">
                <span v-if="cacheResult" class="action-result">{{ cacheResult }}</span>
                <BngButton
                  :accent="ACCENTS.outlined"
                  :disabled="cacheBusy || cacheConfirm"
                  @click="askClearCache"
                >{{ cacheBusy ? "Clearing..." : "Clear cache" }}</BngButton>
                <div class="option-reset"></div>
              </div>

              <!-- A path (the cache folder, the BeamMP launcher): the system's own window; typed below as the fallback -->
              <div v-if="item.type === 'path'" class="inline-controls">
                <BngButton
                  :accent="ACCENTS.outlined"
                  :disabled="!!pathBusy"
                  @click="sendPath(item, true)"
                >{{ pathBusy === item.what && pathPick ? "Window open..." : "Change..." }}</BngButton>
                <div class="option-reset"></div>
              </div>

              <!-- Feedback: the report window or the profiler (the menu closes first: settings.lua M.feedback) -->
              <div v-if="item.type === 'feedback'" class="inline-controls">
                <BngButton :accent="ACCENTS.outlined" @click="openFeedback(item.what)">{{ item.label }}</BngButton>
                <div class="option-reset"></div>
              </div>

              <!-- Inline Switch for dev tools (session only, not saved) -->
              <div v-if="item.type === 'dev'" class="inline-controls">
                <BngSwitch
                  :modelValue="dev.on[item.id] === true"
                  @update:modelValue="val => onDevChange(item, val)"
                />
                <div class="option-reset"></div>
              </div>
            </div>

            <!-- Bottom Half: Native BngSlider -->
            <div v-if="item.type === 'float'" class="stacked-controls">
              <BngSlider
                class="native-slider"
                :min="item.min"
                :max="item.max"
                :step="item.step || 0.01"
                :modelValue="config[item.id]"
                @update:modelValue="val => onSliderChange(item, val)"
                :with-input="true"
                :disabled="item.disabled"
              />
              <div class="option-reset">
                <BngButton
                  :icon="icons.undo"
                  :accent="ACCENTS.outlined"
                  @click="resetSetting(item)"
                  class="bng-reset-btn"
                  :style="{ visibility: config[item.id] !== item.default ? 'visible' : 'hidden' }"
                  title="Reset to default"
                />
              </div>
            </div>

            <!-- Bottom Half: Custom Dropdown Select -->
            <div v-if="item.type === 'select'" class="stacked-controls select-control">
              <!-- Invisible overlay to close dropdown when clicking outside -->
              <div v-if="openDropdown === item.id" class="dropdown-backdrop" @click="openDropdown = null"></div>

              <div class="custom-dropdown-container" :class="{ 'is-open': openDropdown === item.id }">
                <div class="dropdown-selected" @click="toggleDropdown(item.id)">
                  <span>{{ getOptionLabel(item, config[item.id]) }}</span>
                  <span class="dropdown-arrow"></span>
                </div>

                <div class="dropdown-list" v-if="openDropdown === item.id">
                  <div
                    v-for="opt in item.options"
                    :key="opt.value"
                    class="dropdown-option"
                    :class="{ 'is-active': config[item.id] === opt.value }"
                    @click="selectOption(item, opt.value)"
                  >
                    {{ opt.label }}
                  </div>
                </div>
              </div>

              <div class="option-reset">
                <BngButton
                  :icon="icons.undo"
                  :accent="ACCENTS.outlined"
                  @click="resetSetting(item)"
                  class="bng-reset-btn"
                  :style="{ visibility: config[item.id] !== item.default ? 'visible' : 'hidden' }"
                  title="Reset to default"
                />
              </div>
            </div>

            <!-- Clear cache: the confirmation, in the page -->
            <div v-if="item.type === 'action' && cacheConfirm" class="stacked-controls confirm-block">
              <span class="confirm-text">Delete the downloaded elevation and imagery? What this session used and the gravity model stay; the rest is downloaded again when needed.</span>
              <BngButton :accent="ACCENTS.main" @click="confirmClearCache">Clear</BngButton>
              <BngButton :accent="ACCENTS.outlined" @click="cacheConfirm = false">Cancel</BngButton>
            </div>

            <!-- A path typed or pasted (the fallback to the system's window), and where it stands -->
            <div v-if="item.type === 'path'" class="stacked-controls path-control">
              <input
                v-model="pathText[item.what]"
                class="path-input"
                :placeholder="item.placeholder"
                maxlength="1000"
                @focus="typing(true)"
                @blur="typing(false)"
                @keydown.enter.stop.prevent="usePath(item)"
              />
              <BngButton
                :accent="ACCENTS.outlined"
                :disabled="!!pathBusy || !(pathText[item.what] || '').trim()"
                @click="usePath(item)"
              >Use</BngButton>
              <div class="option-reset"></div>
            </div>
            <div v-if="item.type === 'path'" class="option-note" :class="{ 'is-error': pathMsg[item.what] && !pathMsg[item.what].ok }">{{ pathNote(item) }}</div>

            <!-- Short help under a control (always shown, not only on hover) -->
            <div v-if="item.note" class="option-note">{{ item.note }}</div>

            <!-- Bottom Half: wrapped text (attribution) -->
            <div v-if="item.type === 'text'" class="stacked-controls text-block">{{ infoValue(item) }}</div>

          </div>
        </div>
      </details>
    </div>

    <!-- Dynamic Info Panel (Always Visible at Bottom) -->
    <div class="options-info-panel">
      <template v-if="hoveredItem">
        <div class="info-header">
          <span class="info-title">{{ hoveredItem.name }}</span>
          <span class="info-default" v-if="hoveredItem.default !== undefined">Default: {{ formatDefault(hoveredItem) }}</span>
        </div>
        <div class="info-desc">{{ hoveredItem.desc }}</div>
      </template>
      <div v-else class="info-empty">
        Hover over a setting to see details.
      </div>
    </div>

  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted } from "vue"
import { BngButton, BngSwitch, BngSlider, BngInput, icons, ACCENTS } from "@/common/components/base"

const config = ref({})
const status = ref({})
const dev = ref({ on: {}, lastPreset: "lod", active: false })
const hoveredItem = ref(null)
const openDropdown = ref(null) // Tracks which dropdown is currently open
let pollTimer = null
const cacheConfirm = ref(false) // Clear cache: the confirmation is showing
const cacheBusy = ref(false)    // Clear cache: sent, waiting for the server's answer
const cacheResult = ref("")     // "Freed 1.2 GB" / an error
let cacheClearsAt = 0           // status.cache.clears when the clear was sent
let cacheSentAt = 0
let lastWriteAt = 0 // a poll answered just after a uiSet may carry the value from before it
const pathText = ref({ cache: "", beammp: "" }) // the typed paths
const pathBusy = ref("")   // the path ('cache' / 'beammp') whose answer is awaited (its window open, or typed)
const pathPick = ref(false) // ... from the system's window
const pathMsg = ref({})    // path -> the server's last answer { ok, text }
let pathAnswers = 0        // status.paths.answers when it was sent
let pathSentAt = 0

// Mirrors lua/ge/extensions/beamngEarth/settings.lua SCHEMA (the Lua side validates and clamps).
const settingsSchema = [
  {
    name: "Connection",
    items: [
      { id: "connection", type: "info", name: "Companion server", desc: "State of the connection to the BeamNG.Earth companion server (127.0.0.1:47613) and the session id it gave this game." },
      { id: "anchor", type: "info", name: "Anchor", desc: "The current world origin (anchor) and its latitude / longitude. A rebase moves it under the player." },
      { id: "player", type: "info", name: "Player", desc: "The player's latitude / longitude and the distance from the anchor origin." },
      { id: "weather", type: "info", name: "Weather", desc: "Real weather from the server (Open-Meteo.com, every 10 min or 25 km; the server's --weather on|off)." },
      { id: "gravity", type: "info", name: "Gravity", desc: "The gravity in use: the server's real value (WGS84 normal + EGM2008, every 2 km or 200 m of height; the server's --gravity on|off) or normal gravity by latitude." },
      { id: "deshadow", type: "info", name: "Imagery de-shadowing", desc: "Server-side relighting of the baked terrain shadows in the USGS imagery (the server's --deshadow off|lambert|full)." },
      { id: "colormatch", type: "info", name: "Imagery colour seams", desc: "Server-side removal of the colour steps between survey flights in the USGS imagery, e.g. at state lines (the server's --colormatch off|s2)." },
    ],
  },
  {
    name: "World",
    items: [
      { id: "rebaseDistance", type: "float", min: 2000, max: 16000, step: 100, default: 15000, name: "Rebase distance (m)", desc: "How far from the anchor origin the world moves under the player (origin rebase). 15 km keeps float precision at ~1 mm; beyond 16 km precision halves." },
      { id: "viewDistanceMode", type: "select", default: "horizon", name: "View distance", desc: "Real horizon (the default): the far field reaches the true line-of-sight horizon and the view distance follows it (the server's --horizon, on by default in mesh mode). Fixed: the renderer's view distance below.",
        options: [ { label: "Fixed distance", value: "fixed" }, { label: "Real horizon (v1.4 M1h)", value: "horizon" } ] },
      { id: "viewDistanceKm", type: "float", min: 20, max: 1000, step: 10, default: 100, name: "Fixed view distance (km)", desc: "The renderer's view distance in km (LevelInfo visibleDistance) in fixed mode. The far field itself extends 80 km today." },
      { id: "hazeStrength", type: "float", min: 0, max: 1, step: 0.05, default: 0, name: "Distance haze (placeholder)", desc: "Strength of the distance haze. Placeholder: the haze comes with the real-horizon far field (v1.4 M1h); this value is saved but has no effect yet.", disabled: true },
      { id: "skyMode", type: "select", default: "real", name: "Sky", desc: "Real time: the sun, moon and stars follow the real clock and date at the player's position. Manual: hands off, use the game's own Time & Weather controls. Changing the time in the pause menu (or another mod changing it) switches this to Manual by itself; choose Real time to go back.",
        note: "Changing the time in the pause menu switches this to Manual.",
        options: [ { label: "Real time", value: "real" }, { label: "Manual", value: "manual" } ] },
      { id: "clouds", type: "bool", default: true, name: "Clouds", desc: "A volumetric cloud layer that follows the ground under the player." },
      { id: "weatherMode", type: "select", default: "real", name: "Weather", desc: "Real: clouds, fog, wind, rain, snow and temperature follow the real weather at the player's position. Manual: hands off, use the game's own Time & Weather controls. Changing the weather in the pause menu (or another mod such as BWM_Weather changing it) switches this to Manual by itself; choose Real weather to go back.",
        note: "Changing the weather in the pause menu switches this to Manual.",
        options: [ { label: "Real weather", value: "real" }, { label: "Manual", value: "manual" } ] },
      { id: "realGravity", type: "bool", default: true, name: "Real gravity", desc: "Gravity follows the player: the server's real value (normal gravity plus the EGM2008 anomalies) or normal gravity by latitude and height. Off: the level's 9.81 m/s2." },
      { id: "groundCover", type: "bool", default: true, name: "Grass and shrubs", desc: "3D grass where the land cover is grassland or forest floor, and scrub with sparse grass on shrubland, near the car (the game's own Hirochi Raceway and West Coast USA plants). Needs ground surfaces from the world server (on by default)." },
      { id: "rocks", type: "bool", default: true, name: "Rocks", desc: "Rocks and boulders on bare, rocky and steep ground. Off removes them (and their collision)." },
      { id: "groundCoverDensity", type: "select", default: "normal", name: "Grass density", desc: "How dense the grass and shrubs are. Without the optional grass patch (tools/gcpatch, Linux) the game regrows the grass in one slower frame each time the ground under the car moves (every ~130-190 m); higher density makes that frame longer.",
        options: [ { label: "Low", value: "low" }, { label: "Normal", value: "normal" }, { label: "High", value: "high" } ] },
      { id: "roads", type: "bool", default: true, name: "Roads", desc: "Real roads from OpenStreetMap / Overture within about 600 m of the car: paved roads as asphalt, gravel and dirt roads and tracks as worn dirt tracks, and the same surfaces under the tyres. Bridges, tunnels, markings and junction shapes come later." },
    ],
  },
  {
    name: "Interface",
    items: [
      { id: "hudVisible", type: "bool", default: false, name: "Status HUD", desc: "Show the compact BeamNG.Earth status window (connection, anchor, far field, quick buttons). Also whether it opens at level start." },
      { id: "dashboardSatellite", type: "bool", default: true, name: "Satellite map on dashboard", desc: "Show BeamNG.Earth satellite imagery on compatible vehicle dashboard screens. Off restores the stock dashboard page." },
      { id: "creditsVisible", type: "bool", default: true, name: "Data credits on screen", desc: "Small text in a screen corner naming the sources of the imagery, elevation and weather around you right now (the far field's own sources on a second line). On by default: CC BY and similar licences ask for visible attribution. Hidden in the pause menu and the big map." },
      { id: "creditsCorner", type: "select", default: "bottom-left", name: "Data credits corner", desc: "The screen corner of the data credits.",
        options: [ { label: "Bottom left", value: "bottom-left" }, { label: "Bottom right", value: "bottom-right" }, { label: "Top left", value: "top-left" }, { label: "Top right", value: "top-right" } ] },
      { id: "creditsSize", type: "select", default: "small", name: "Data credits size", desc: "Text size of the data credits: small (11 px) or normal (14 px).",
        options: [ { label: "Small", value: "small" }, { label: "Normal", value: "normal" } ] },
    ],
  },
  {
    name: "Multiplayer",
    items: [
      { id: "beammpOnLaunch", type: "bool", default: true, name: "Start BeamMP with BeamNG.Earth", desc: "The BeamNG.Earth app starts BeamMP (multiplayer) with the game, so BeamNG.Earth servers show in BeamMP's server browser (main menu, More, BeamMP, the BeamNG.Earth category). Off: BeamMP isn't started or offered. Takes effect at the next start of the app." },
      { id: "beammpLauncher", type: "path", what: "beammp", name: "BeamMP launcher", placeholder: "Path to BeamMP-Launcher (the program or its folder)", desc: "The BeamMP launcher the app starts: the one you chose, else the one it found. Change... opens your system's file window to pick your own (it may open behind the game: Alt+Tab to it); or type or paste its path below and press Use. Used from the next start of the app." },
    ],
  },
  {
    name: "Download cache",
    items: [
      { id: "cacheUsage", type: "info", name: "Download cache", desc: "Elevation and imagery the server downloaded and keeps on disk (in the cache folder below), so revisited places load without downloading again: the space in use, the size limit and the number of files." },
      { id: "cacheDir", type: "path", what: "cache", name: "Cache folder", placeholder: "Folder for the download cache, or default", desc: "Where the download cache is kept, e.g. on another drive. Change... opens your system's folder window (it may open behind the game: Alt+Tab to it); or type or paste a folder below and press Use (default: back to .cache/beamng-earth in your home folder). Used from the next start of the app; move the old folder's contents there first to keep what is already downloaded." },
      { id: "cacheCapGb", type: "select", default: "20", name: "Download cache size", desc: "The most the download cache may use. Over the limit, the server removes the files used longest ago (every few minutes, down to 90 % of the limit); never what this session used in the last hour or the gravity model. Kept by the server for its next start.",
        options: [ { label: "5 GB", value: "5" }, { label: "10 GB", value: "10" }, { label: "20 GB", value: "20" }, { label: "50 GB", value: "50" }, { label: "Unlimited", value: "unlimited" } ] },
      { id: "cacheClear", type: "action", name: "Clear cache", desc: "Deletes the downloaded elevation and imagery (and the server's de-shadowing and colour-seam data). What this session used and the gravity model stay. Places are downloaded again when you visit them." },
    ],
  },
  {
    name: "Dev Tools",
    devPresets: true,
    items: [
      { id: "tint", type: "dev", name: "LOD tint", desc: "Tints every far tile by its level: green = matches the physics ground, cyan L0, blue L1, purple L2, orange to red coarser. Applied in small batches per frame. Session only." },
      { id: "outlines", type: "dev", name: "Tile outlines", desc: "Outlines of the tiles near the player, coloured by level (shown) or state: grey hidden-loaded, yellow in a crossover, magenta previous anchor, red doomed. Session only." },
      { id: "loading", type: "dev", name: "Loading heat", desc: "Queued tiles flash; the next 10 imports are numbered in the order they will load. Session only." },
      { id: "physics", type: "dev", name: "Physics block", desc: "The collidable terrain square, the pending one (dashed) and the terrain-follow prepare / commit squares. Session only." },
      { id: "physicsTerrain", type: "dev", name: "Show physics terrain", desc: "Renders the invisible physics terrain so it can be compared with the visible tiles. Session only." },
      { id: "rings", type: "dev", name: "Rings", desc: "Near-detail radii, the rebase and prefetch circles and the far-field square. Session only." },
      { id: "probe", type: "dev", name: "Under-car probe", desc: "A line at the car: physics ground vs the visible tile; red when the tile under the car is not the exact level or is more than 5 cm off. Session only." },
      { id: "stats", type: "dev", name: "Stats panel", desc: "Frame times, governor budget, imports and deletes per second, queue, objects, pops, failures, server backlog. Session only." },
      { id: "graphs", type: "dev", name: "Graphs panel", desc: "60 s plots of frame time, queue length, imports per second and speed. Session only." },
      { id: "map", type: "dev", name: "Mini-map panel", desc: "Top-down map of the tiles around the player; click a tile to inspect it. Session only." },
      { id: "controls", type: "dev", name: "Controls panel", desc: "Overlay toggles, freeze LOD, force rebase, streaming budget, sky mode. Session only." },
      { id: "log", type: "dev", name: "Event log", desc: "The last 50 mod events (terrain commits, rebases, pops, failures, status). Session only." },
      { id: "devOutlineKm", type: "float", min: 0.5, max: 20, step: 0.5, default: 3, name: "Outline radius (km)", desc: "Tile outlines and loading heat are drawn within this distance of the player." },
      { id: "devMapKm", type: "float", min: 1, max: 80, step: 1, default: 10, name: "Mini-map radius (km)", desc: "The mini-map's initial zoom." },
    ],
  },
  {
    name: "Feedback",
    items: [
      { id: "reportOpen", type: "feedback", what: "report", label: "Report", name: "Report an issue or idea", desc: "Closes this menu, takes a screenshot, the location, the frame rate and your hardware and graphics settings, then opens a window to pick the kind (map issue, performance issue, bug, idea), give a title and describe it. Saves a ready-to-post GitHub issue (issue.md) in the report folder. Also Ctrl+Shift+E while driving." },
      { id: "profilerOpen", type: "feedback", what: "profiler", label: "Start profiler", name: "Performance profiler", desc: "Closes this menu and records 30 s of frame times while you drive (every hitch with its place), then opens the same window as a performance issue. Also Ctrl+Shift+Y while driving." },
      { id: "reportPost", type: "text", name: "Posting it", desc: "After saving, the window shows the folder and opens the GitHub new-issue page; or run python3 tools/reports.py issue in the mod's folder. Edit issue.md and drag screenshot.jpg into the issue." },
    ],
  },
  {
    name: "Attribution",
    items: [
      { id: "credits", type: "text", name: "On screen now", desc: "The data credits for what is around you now, as drawn in the screen corner (sent by the server whenever they change)." },
      { id: "attribution", type: "text", name: "Data sources", desc: "Credits for the elevation and imagery sources in use." },
    ],
  },
]

const devPresets = [
  { id: "off", label: "Off" },
  { id: "lod", label: "LOD" },
  { id: "loading", label: "Loading" },
  { id: "physics", label: "Physics" },
  { id: "all", label: "All" },
]

// Extract global defaults (persisted settings only)
const defaultSettings = {}
settingsSchema.forEach(cat => {
  cat.items.forEach(item => { if (item.default !== undefined) defaultSettings[item.id] = item.default })
})

const connectionText = computed(() => {
  const s = status.value
  if (!s.connection) return "Mod not loaded"
  let t = s.connection
  if (s.session) t += ` (session ${s.session})`
  if (s.lastError) t += ` - ${s.lastError}`
  return t
})

const statusText = computed(() => status.value.status || "-")

// the connection banner's state line (core.banner(): starting / not running / lost / offline / degraded
// / connected, plus the edge guard's line) and its level (info / ok / warn / error)
const bannerText = computed(() => (status.value.banner && status.value.banner.text) || "")
const bannerLevel = computed(() => (status.value.banner && status.value.banner.level) || "info")
// a newer version on GitHub: { current, latest, url } (core.updateInfo)
const update = computed(() => status.value.update || null)
function openUpdate() {
  const url = update.value && update.value.url
  if (url) engineLua(`openWebBrowser(${JSON.stringify(url)})`)
}

function fmt(v, digits) {
  return (typeof v === "number") ? v.toFixed(digits) : "-"
}

function infoValue(item) {
  const s = status.value
  if (item.id === "connection") return connectionText.value
  if (item.id === "anchor") return s.anchorId ? `#${s.anchorId}  ${fmt(s.anchorLat, 5)}, ${fmt(s.anchorLon, 5)}` : "none"
  if (item.id === "player") return (typeof s.playerLat === "number") ? `${fmt(s.playerLat, 5)}, ${fmt(s.playerLon, 5)}  (${fmt(s.dist / 1000, 1)} km from origin)` : "-"
  if (item.id === "attribution") return s.attribution || "-"
  if (item.id === "credits") return s.credits || "-"
  if (item.id === "reportPost") return "github.com/TecnaGamer/BeamNG.Earth/issues"
  if (item.id === "weather") return s.weather || "-"
  if (item.id === "gravity") return s.gravity || "-"
  if (item.id === "deshadow") return s.deshadow || "off"
  if (item.id === "colormatch") return s.colormatch || "off"
  if (item.id === "cacheUsage") return (s.cache && s.cache.line) || "-"
  return ""
}

function formatDefault(item) {
  if (item.type === "bool") return item.default ? "Enabled" : "Disabled"
  if (item.type === "select") {
    const opt = item.options.find(o => o.value === item.default)
    return opt ? opt.label : item.default
  }
  return item.default
}

function luaValue(val) {
  if (typeof val === "boolean") return val ? "true" : "false"
  if (typeof val === "number") return String(val)
  return JSON.stringify(String(val))
}

function showWelcome() {
  engineLua("if beamngEarth_settings and beamngEarth_settings.showWelcome then beamngEarth_settings.showWelcome() end")
}

// Feedback buttons: settings.lua closes the menu, then opens the report window or starts the profiler
function openFeedback(what) {
  engineLua(`if beamngEarth_settings and beamngEarth_settings.feedback then beamngEarth_settings.feedback(${JSON.stringify(what)}) end`)
}

function engineLua(script, cb) {
  if (!window.bngApi || !window.bngApi.engineLua) return
  window.bngApi.engineLua(script, cb)
}

// The poll refreshes the values too: weather.lua / sky.lua switch Weather and Sky to Manual by
// themselves when they are changed in the game, and the dropdowns show it within 2 s.
function applyState(state) {
  if (!state) return
  if (state.settings && Date.now() - lastWriteAt > 1000) config.value = { ...defaultSettings, ...state.settings }
  if (state.status) status.value = state.status
  const c = status.value.cache
  if (cacheBusy.value && c && c.clears > cacheClearsAt) { // the server's answer to our clear
    cacheBusy.value = false
    cacheResult.value = `Freed ${c.clearedText || "0 MB"}`
  } else if (cacheBusy.value && Date.now() - cacheSentAt > 120000) { // a server without the cache, or gone
    cacheBusy.value = false
    cacheResult.value = "No answer from the server"
  }
  const p = status.value.paths
  if (pathBusy.value && p && p.answers > pathAnswers && p.result) { // the server's answer to our choice
    pathMsg.value = { ...pathMsg.value, [p.result.what]: p.result }
    if (p.result.ok) pathText.value = { ...pathText.value, [p.result.what]: "" }
    pathBusy.value = ""
  } else if (pathBusy.value && Date.now() - pathSentAt > (pathPick.value ? 960000 : 30000)) {
    pathMsg.value = { ...pathMsg.value, [pathBusy.value]: { ok: false, text: "No answer from the server" } }
    pathBusy.value = ""
  }
  if (state.dev) dev.value = state.dev
}

// The cache folder and the BeamMP launcher: what they are now (the server's app_paths) or the last answer
function pathNote(item) {
  const m = pathMsg.value[item.what]
  if (m) return m.text
  if (pathBusy.value === item.what) return pathPick.value ? "Pick it in the window that opened (Alt+Tab if it is behind the game)" : "..."
  const p = status.value.paths
  if (!p) return "-"
  if (item.what === "cache") {
    return p.cacheNext && p.cacheNext !== p.cacheDir ? `In use: ${p.cacheDir}  (next start: ${p.cacheNext})` : `In use: ${p.cacheDir || "-"}`
  }
  return p.beammp ? `Now: ${p.beammp}` : "None found"
}

function sendPath(item, pick, text) {
  pathMsg.value = { ...pathMsg.value, [item.what]: null }
  pathAnswers = (status.value.paths && status.value.paths.answers) || 0
  const script = `beamngEarth_settings and beamngEarth_settings.uiPathSet(${JSON.stringify(item.what)}, ${pick ? "true" : "false"}, ${JSON.stringify(text || "")}) or nil`
  engineLua(script, res => {
    if (res && res.ok) { pathBusy.value = item.what; pathPick.value = pick; pathSentAt = Date.now() }
    else pathMsg.value = { ...pathMsg.value, [item.what]: { ok: false, text: (res && res.error) || "mod not loaded" } }
  })
}

function usePath(item) {
  const t = (pathText.value[item.what] || "").trim()
  if (t && !pathBusy.value) sendPath(item, false, t)
}

// typing in a text box: the game's keys off meanwhile
function typing(on) { engineLua(`if setCEFTyping then setCEFTyping(${on ? "true" : "false"}) end`) }

function refresh() {
  engineLua("beamngEarth_settings and beamngEarth_settings.uiState() or nil", applyState)
}

function updateSetting(key) {
  const script = `beamngEarth_settings and beamngEarth_settings.uiSet(${JSON.stringify(key)}, ${luaValue(config.value[key])}) or nil`
  lastWriteAt = Date.now()
  engineLua(script, res => {
    if (res && res.value !== undefined) config.value[key] = res.value // the Lua side clamps
  })
}

function resetSetting(item) {
  config.value[item.id] = item.default
  updateSetting(item.id)
}

function onSliderChange(item, val) {
  config.value[item.id] = val
  updateSetting(item.id)
}

function onSwitchChange(item, val) {
  config.value[item.id] = val
  updateSetting(item.id)
}

function onDevChange(item, val) {
  dev.value.on[item.id] = val
  engineLua(`beamngEarth_settings and beamngEarth_settings.uiDevSet(${JSON.stringify(item.id)}, ${luaValue(val)}) or nil`, state => {
    if (state) dev.value = state
  })
}

function applyDevPreset(name) {
  engineLua(`beamngEarth_settings and beamngEarth_settings.uiDevPreset(${JSON.stringify(name)}) or nil`, state => {
    if (state) dev.value = state
  })
}

// Clear cache: the button asks first (cacheConfirm), Clear sends it; the poll sees the answer
function askClearCache() {
  cacheResult.value = ""
  cacheConfirm.value = true
}

function confirmClearCache() {
  cacheConfirm.value = false
  cacheClearsAt = (status.value.cache && status.value.cache.clears) || 0
  engineLua("beamngEarth_settings and beamngEarth_settings.uiCacheClear() or nil", res => {
    if (res && res.ok) { cacheBusy.value = true; cacheSentAt = Date.now() }
    else cacheResult.value = (res && res.error) || "mod not loaded"
  })
}

// Custom Dropdown Functions
function toggleDropdown(id) {
  openDropdown.value = openDropdown.value === id ? null : id
}

function selectOption(item, val) {
  config.value[item.id] = val
  updateSetting(item.id)
  openDropdown.value = null // Close menu after selection
}

function getOptionLabel(item, val) {
  const opt = item.options.find(o => o.value === val)
  return opt ? opt.label : val
}

onMounted(() => {
  config.value = { ...defaultSettings }
  refresh()
  pollTimer = setInterval(refresh, 2000) // status lines while the page is open
})

onUnmounted(() => {
  if (pollTimer) clearInterval(pollTimer)
  pollTimer = null
})
</script>

<style scoped lang="scss">
* {
  box-sizing: border-box;
}

.earth-bng-options {
  display: flex;
  flex-direction: column;
  width: 100%;
  height: 100%;
  min-height: 65vh;
  overflow: hidden;
  font-family: 'Overpass', sans-serif;
  color: #fff;
  background-color: var(--bng-off-black, rgba(15, 15, 15, 0.95));
  border-radius: 6px;
}

/* Header */
.options-header {
  flex: 0 0 auto;
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 0.5em 1em 0.8em;
  background-color: rgba(0, 0, 0, 0.2);
  border-bottom: 2px solid var(--bng-orange-550, #f60);

  .header-title {
    font-size: 1.3rem;
    font-weight: 600;
  }

  .header-welcome {
    margin-left: auto;
    margin-right: 1em;
    padding: 0.25em 0.8em;
    font-size: 0.9rem;
    color: #fff;
    background: rgba(255, 255, 255, 0.08);
    border: 1px solid rgba(255, 255, 255, 0.25);
    border-radius: 4px;
    cursor: pointer;
  }

  .header-status {
    font-weight: 600;
    color: #f88;

    &.is-connected { color: #8f8; }
  }
}

/* The update line: its text and a button to the download page */
.update-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1em;
  .update-link { padding: 0.2em 0.8em; }
}

/* Connection state line (the on-screen banner's text); a plain div, nothing focusable */
.state-bar {
  border-left: 4px solid #6af;

  .state-text {
    font-size: 0.95em;
    font-weight: 600;
    white-space: pre-line;
  }

  &.state-ok { border-left-color: #4c4; }
  &.state-warn { border-left-color: #fa3; background-color: rgba(255, 170, 51, 0.10); }
  &.state-error { border-left-color: #f54; background-color: rgba(255, 85, 68, 0.12); }
}

/* Presets / status bar */
.options-presets {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  padding: 0.5em 1em;
  background-color: rgba(255, 255, 255, 0.03);
  border-bottom: 1px solid rgba(255, 255, 255, 0.1);

  .preset-btn {
    flex: 1;
    margin: 0 0.25em;
    --bng-button-padding-y: 0.3em;
    font-size: 0.9em;
  }

  .status-text {
    font-size: 0.9em;
    color: #ccc;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
}

/* Main List Array */
.options-list-scroll {
  flex: 1 1 0;
  overflow-y: auto;
  overflow-x: hidden;
  width: 100%;
  padding: 0.5em;

  &::-webkit-scrollbar { width: 8px; }
  &::-webkit-scrollbar-track { background: transparent; }
  &::-webkit-scrollbar-thumb { background: rgba(255, 255, 255, 0.2); border-radius: 4px; }
  &::-webkit-scrollbar-thumb:hover { background: rgba(255, 255, 255, 0.4); }
}

/* Collapsible Details Styles */
details.category-block {
  margin-bottom: 0.5em;
  width: 100%;
  background: rgba(0, 0, 0, 0.15);
  border-radius: 4px;
}

summary.category-title {
  display: flex;
  align-items: center;
  padding: 0.5em 0.75em;
  font-size: 1.05rem;
  font-weight: 600;
  color: #ddd;
  background: rgba(255, 255, 255, 0.05);
  cursor: pointer;
  list-style: none;
  user-select: none;
  border-radius: 4px;
  transition: background-color 0.1s ease;

  &::-webkit-details-marker { display: none; }
  &:hover { background: rgba(255, 255, 255, 0.08); }

  // the arrow is its own element: the game draws its orange focus frame with ::before on the
  // selected item (ours was rotated with it, then replaced by it); a CSS triangle, not a glyph
  .category-arrow {
    display: inline-block;
    flex: none;
    width: 0;
    height: 0;
    margin-right: 0.6em;
    border-top: 0.3em solid transparent;
    border-bottom: 0.3em solid transparent;
    border-left: 0.45em solid var(--bng-orange-550, #f60);
    transition: transform 0.2s ease;
  }
}

details[open] > summary.category-title {
  border-bottom-left-radius: 0;
  border-bottom-right-radius: 0;
  border-bottom: 1px solid rgba(255, 255, 255, 0.05);
  .category-arrow { transform: rotate(90deg); }
}

.category-items {
  padding: 0.25em 0;
}

/* Item Rows */
.options-item-row {
  display: flex;
  flex-direction: column;
  background-color: rgba(0, 0, 0, 0.2);
  margin-bottom: 2px;
  padding: 0.6em 0.8em;
  width: 100%;

  &:nth-child(even) {
    background-color: transparent;
  }

  .row-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    width: 100%;

    .option-label {
      flex: 1 1 auto;
      font-size: 0.95rem;
      padding-right: 0.5em;
    }

    .info-value {
      flex: 0 1 auto;
      font-size: 0.9rem;
      color: #ccc;
      text-align: right;
    }

    .inline-controls {
      flex: 0 0 auto;
      display: flex;
      align-items: center;
      gap: 0.75em;
    }
  }

  .stacked-controls {
    display: flex;
    align-items: center;
    width: 100%;
    margin-top: 0.6em;
    gap: 0.75em;

    .native-slider {
      flex: 1 1 auto;
      min-width: 0;
    }
  }

  .text-block {
    font-size: 0.85rem;
    color: #ccc;
    line-height: 1.35;
    white-space: pre-wrap;
  }

  .action-result {
    font-size: 0.85rem;
    color: #ccc;
  }

  .confirm-block {
    .confirm-text {
      flex: 1 1 auto;
      font-size: 0.85rem;
      color: #ccc;
      line-height: 1.3;
    }
  }

  .option-note {
    margin-top: 0.35em;
    font-size: 0.8rem;
    color: rgba(255, 255, 255, 0.55);
    line-height: 1.3;
    overflow-wrap: anywhere;

    &.is-error { color: #ffb08a; }
  }

  .path-input {
    flex: 1 1 auto;
    min-width: 0;
    padding: 0.35em 0.6em;
    font-size: 0.9rem;
    color: #fff;
    background: rgba(0, 0, 0, 0.55);
    border: 1px solid rgba(255, 255, 255, 0.3);
    border-radius: 4px;
  }
}

/* ==============================================================
   Custom Dropdown CSS (Bypasses CEF OSR restrictions)
   ============================================================== */

.dropdown-backdrop {
  position: fixed;
  inset: 0;
  z-index: 9998;
  cursor: default;
}

.custom-dropdown-container {
  position: relative;
  flex: 1 1 auto;
  font-size: 0.95rem;
  z-index: 1;

  &.is-open {
    z-index: 9999; /* Lift above the backdrop */
  }
}

.dropdown-selected {
  background: rgba(0, 0, 0, 0.4);
  color: #fff;
  border: 1px solid rgba(255, 255, 255, 0.2);
  border-radius: 4px;
  padding: 0.45em 0.75em;
  display: flex;
  justify-content: space-between;
  align-items: center;
  cursor: pointer;
  transition: border-color 0.15s ease;
  user-select: none;

  &:hover {
    border-color: rgba(255, 255, 255, 0.4);
  }

  .dropdown-arrow {             // a CSS triangle in the text colour (no '▼' glyph in the UI font)
    flex: none;
    width: 0;
    height: 0;
    margin-left: 0.5em;
    color: rgba(255, 255, 255, 0.5);
    border-left: 0.3em solid transparent;
    border-right: 0.3em solid transparent;
    border-top: 0.4em solid currentColor;
  }
}

.custom-dropdown-container.is-open .dropdown-selected {
  border-color: var(--bng-orange-550, #f60);
  .dropdown-arrow { color: var(--bng-orange-550, #f60); }
}

.dropdown-list {
  position: absolute;
  top: calc(100% + 4px);
  left: 0;
  right: 0;
  background: #1a1a1a;
  border: 1px solid var(--bng-orange-550, #f60);
  border-radius: 4px;
  overflow: hidden;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.5);
}

.dropdown-option {
  padding: 0.5em 0.75em;
  cursor: pointer;
  color: #ddd;
  transition: background-color 0.1s;

  &:hover {
    background: rgba(255, 102, 0, 0.2);
    color: #fff;
  }

  &.is-active {
    background: var(--bng-orange-550, #f60);
    color: #fff;
    font-weight: 600;
  }
}

/* Reset Button Space */
.option-reset {
  flex: 0 0 2.5em;
  width: 2.5em;
  display: flex;
  justify-content: flex-end;
  padding-left: 0.5em;

  .bng-reset-btn {
    --bng-button-padding-x: 0.3em;
    --bng-button-padding-y: 0.15em;
    opacity: 0.8;
    &:hover { opacity: 1; }
  }
}

/* Info Panel at Bottom */
.options-info-panel {
  flex: 0 0 8.0em;
  min-height: 8.0em;
  max-height: 8.0em;
  width: 100%;
  background-color: rgba(0, 0, 0, 0.6);
  border-top: 2px solid var(--bng-orange-550, #f60);
  padding: 0.6em 1.2em;
  overflow-y: auto;
  overflow-x: hidden;

  .info-empty {
    height: 100%;
    display: flex;
    align-items: center;
    justify-content: center;
    color: rgba(255, 255, 255, 0.4);
    font-size: 0.95em;
    font-style: italic;
  }

  .info-header {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    margin-bottom: 0.2em;

    .info-title {
      font-weight: 700;
      font-size: 1.05rem;
      color: #fff;
    }
    .info-default {
      font-family: monospace;
      font-size: 0.9em;
      color: rgba(255, 255, 255, 0.5);
    }
  }

  .info-desc {
    font-size: 0.9rem;
    color: #ccc;
    line-height: 1.35;
    white-space: pre-wrap;
  }
}
</style>
