// BeamNG.Earth's settings: the schema, the presets and the state shared by the settings pages (the pause menu's
// BeamNG.Earth tab: EarthSettingsPage.vue per page, EarthSettingsSide.vue beside them; and the full page
// EarthSettings.vue, a route of its own for the main menu). One state for all of them (this module is loaded once):
// the setting pointed at in a page shows its help in the side card. Lua side: lua/ge/extensions/beamngEarth/settings.lua.
import { ref, computed } from "vue"

// Mirrors lua/ge/extensions/beamngEarth/settings.lua SCHEMA (the Lua side validates and clamps). Sections down the
// left; Graphics first (the quality presets: settings.lua PRESETS).
export const settingsSchema = [
  {
    name: "Graphics",
    presets: true,
    intro: "Pick a preset, then fine-tune the settings below if you like. Each change applies at once (the ground photo resolution at the next level load).",
    items: [
      { id: "framePacing", type: "bool", default: true, name: "Even frame pacing", desc: "Keeps the time between frames steady, so the game's speed doesn't pulse while the world loads around you. Costs up to about a tenth of the frame rate. Turn it off for the highest frame rate." },
      { id: "terrainDetail", type: "select", default: "normal", name: "Terrain detail", desc: "How detailed the distant ground is: how far out each finer level of the land's shape reaches, out to the horizon. Low: coarser hills sooner (fewer pieces to build and draw: about 20 % less). High, Ultra and Extreme: sharper mountains and coastlines far away; Extreme builds about twice as many pieces of distant ground, so new places take longer to fill in. Applies at once.",
        options: [ { label: "Low", value: "low" }, { label: "Normal", value: "normal" }, { label: "High", value: "high" }, { label: "Ultra", value: "ultra" }, { label: "Extreme", value: "extreme" } ] },
      { id: "groundPhoto", type: "select", default: "2048", name: "Ground photo", desc: "The sharpness of the aerial photo and the painted roads on the ground around you (each 1 km square). 1024: 1 m a pixel, the lightest (handhelds). 2048: 0.5 m, the default. 4096: 0.25 m, the crispest road edges and markings in the ground, but each square takes 1-3 s longer to make on a fast PC (much longer on a handheld) and uses 4 times the video memory.",
        note: "Takes effect when the level next loads.",
        options: [ { label: "1024 (light)", value: "1024" }, { label: "2048 (default)", value: "2048" }, { label: "4096 (sharpest)", value: "4096" } ] },
      { id: "trees", type: "select", default: "normal", name: "Tree density", desc: "How many trees and bushes are drawn at each distance. Low: thinner forests (the most frame time saved in wooded places). Ultra and Extreme: denser forests far out (strong GPUs).",
        options: [ { label: "Low", value: "low" }, { label: "Medium", value: "medium" }, { label: "Normal", value: "normal" }, { label: "High", value: "high" }, { label: "Ultra", value: "ultra" }, { label: "Extreme", value: "extreme" } ] },
      { id: "treeDistance", type: "select", default: "normal", name: "Tree draw distance", desc: "How far out trees, rocks, poles and roadside props are drawn: about 1.3 km at Normal, 1 km at Near, 2.8 km at Max. Farther needs more memory and a stronger GPU, and the app builds more tree tiles around you.",
        options: [ { label: "Near", value: "near" }, { label: "Medium", value: "medium" }, { label: "Normal", value: "normal" }, { label: "Far", value: "far" }, { label: "Farther", value: "farther" }, { label: "Max", value: "max" } ] },
      { id: "buildingDistance", type: "select", default: "normal", name: "Building draw distance", desc: "How far out buildings and bridges are drawn: simple far buildings to about 1.6 km and the skyline's big buildings to about 4.8 km at Normal; Near 60 %, Far 150 %, Max twice as far. The buildings near you are always drawn in full.",
        options: [ { label: "Near", value: "near" }, { label: "Normal", value: "normal" }, { label: "Far", value: "far" }, { label: "Max", value: "max" } ] },
      { id: "objectDetail", type: "select", default: "game", name: "Object detail", desc: "How far out trees, buildings, props and cars keep their detailed models before switching to simpler ones, in BeamNG.Earth's world only. Game setting: the game's own Mesh Quality (Options, Graphics). Low to Extreme override it here (Ultra is the game's highest; Extreme 1.5 times more). Higher costs GPU time where much is in view.",
        options: [ { label: "Game setting", value: "game" }, { label: "Low", value: "low" }, { label: "Normal", value: "normal" }, { label: "High", value: "high" }, { label: "Ultra", value: "ultra" }, { label: "Extreme", value: "extreme" } ] },
      { id: "grass", type: "select", virtual: true, default: "normal", name: "Grass and shrubs", desc: "3D grass and scrub near the car (the game's own Hirochi Raceway and West Coast USA plants). Off saves the most on handhelds. Without the optional grass patch (tools/gcpatch, Linux) the game regrows the grass in one slower frame each time the ground under the car moves; denser grass makes that frame longer.",
        options: [ { label: "Off", value: "off" }, { label: "Low", value: "low" }, { label: "Normal", value: "normal" }, { label: "High", value: "high" } ] },
      { id: "grassDistance", type: "select", default: "normal", name: "Grass draw distance", desc: "How far out the 3D grass reaches (about 50-70 m at Normal; Near 70 %, Far 150 %). The same density over a larger area: Far draws about twice as many blades, and the regrow frame when the ground moves gets longer.", showIf: c => c.groundCover !== false,
        options: [ { label: "Near", value: "near" }, { label: "Normal", value: "normal" }, { label: "Far", value: "far" } ] },
      { id: "rocks", type: "bool", default: true, name: "Rocks", desc: "Rocks and boulders on bare, rocky and steep ground. Off removes them (and their collision)." },
      { id: "nightLights", type: "select", default: "normal", name: "Street lamp lights", desc: "How many street lamps near you cast real light on the road at night (the lamps further out still glow). Fewer: cheaper nights.",
        options: [ { label: "Few", value: "few" }, { label: "Normal", value: "normal" }, { label: "Many", value: "many" } ] },
      { id: "viewDistanceMode", type: "select", default: "horizon", name: "View distance", desc: "Real horizon (the default): the land reaches the true horizon. Fixed: the view ends at the distance below (cheaper on weak hardware).",
        options: [ { label: "Real horizon", value: "horizon" }, { label: "Fixed distance", value: "fixed" } ] },
      { id: "viewDistanceKm", type: "float", min: 20, max: 1000, step: 10, default: 100, name: "Fixed view distance (km)", desc: "How far you can see with a fixed view distance.", showIf: c => c.viewDistanceMode === "fixed" },
    ],
  },
  {
    name: "World",
    items: [
      { id: "skyMode", type: "select", default: "real", name: "Sun and stars", desc: "Real time: the sun, moon and stars follow the real clock and date where you are. Manual: use the game's own Time & Weather controls. Changing the time in the pause menu switches this to Manual by itself; choose Real time to go back.",
        note: "Changing the time in the pause menu switches this to Manual.",
        options: [ { label: "Real time", value: "real" }, { label: "Manual", value: "manual" } ] },
      { id: "weatherMode", type: "select", default: "real", name: "Weather", desc: "Real: clouds, fog, wind, rain, snow and temperature follow the real weather where you are. Manual: use the game's own Time & Weather controls. Changing the weather in the pause menu (or another weather mod) switches this to Manual by itself; choose Real weather to go back.",
        note: "Changing the weather in the pause menu switches this to Manual.",
        options: [ { label: "Real weather", value: "real" }, { label: "Manual", value: "manual" } ] },
      { id: "clouds", type: "bool", default: true, name: "Clouds", desc: "A volumetric cloud layer that follows the ground under you." },
      { id: "realGravity", type: "bool", default: true, name: "Real gravity", desc: "Gravity as it really is where you are (it varies a little with latitude, height and the ground below). Off: the usual 9.81 m/s2." },
      { id: "roads", type: "bool", default: true, name: "Roads", desc: "Real roads, bridges and buildings near you from OpenStreetMap and Overture. Off: bare terrain only." },
      { id: "highwaySigns", type: "bool", default: true, name: "Highway signs", desc: "Exit and overhead destination signs, merge warnings and Interstate mile markers along US highways, from the signs mapped in OpenStreetMap. Needs Roads." },
      { id: "rebaseDistance", type: "float", min: 2000, max: 16000, step: 100, default: 15000, name: "Rebase distance (m)", desc: "Advanced. How far you drive before the world quietly moves back under you (keeps the physics precise). 15 km keeps precision at about 1 mm." },
    ],
  },
  {
    name: "Interface",
    items: [
      { id: "statusBar", type: "bool", default: true, name: "Status bar", desc: "The bar at the top of the screen: the street and town you are in, loading progress and the frame rate." },
      { id: "placeNamesOnline", type: "bool", default: true, name: "Place names (online)", desc: "Town, state and country names in the status bar, looked up online (Photon by komoot, sent your position rounded to about 1 km)." },
      { id: "hudVisible", type: "bool", default: false, name: "Status window", desc: "The compact BeamNG.Earth status window (connection, location, loading, quick buttons). Also whether it opens at level start." },
      { id: "dashboardSatellite", type: "bool", default: true, name: "Satellite map on dashboards", desc: "BeamNG.Earth's satellite map on vehicles with dashboard screens. Off: the stock dashboard page." },
      { id: "creditsVisible", type: "bool", default: true, name: "Data credits on screen", desc: "Small text in a corner naming the sources of the imagery, elevation and weather around you. On by default: many of the data licences ask for visible credit. Hidden in the pause menu and the big map." },
      { id: "creditsCorner", type: "select", default: "bottom-left", name: "Data credits corner", desc: "The screen corner of the data credits.", showIf: c => c.creditsVisible !== false,
        options: [ { label: "Bottom left", value: "bottom-left" }, { label: "Bottom right", value: "bottom-right" }, { label: "Top left", value: "top-left" }, { label: "Top right", value: "top-right" } ] },
      { id: "creditsSize", type: "select", default: "small", name: "Data credits size", desc: "Text size of the data credits.", showIf: c => c.creditsVisible !== false,
        options: [ { label: "Small", value: "small" }, { label: "Normal", value: "normal" } ] },
    ],
  },
  {
    name: "Multiplayer",
    items: [
      { id: "beammpOnLaunch", type: "bool", default: true, name: "Start BeamMP with BeamNG.Earth", desc: "The BeamNG.Earth app starts BeamMP (multiplayer) with the game, so BeamNG.Earth servers show in BeamMP's server browser (main menu, More, BeamMP, the BeamNG.Earth category). Takes effect at the next start of the app." },
      { id: "beammpLauncher", type: "path", what: "beammp", name: "BeamMP launcher", placeholder: "Path to BeamMP-Launcher (the program or its folder)", desc: "The BeamMP launcher the app starts: the one you chose, else the one it found. Change... opens your system's file window (it may open behind the game: Alt+Tab to it); or type or paste its path below and press Use. Used from the next start of the app." },
    ],
  },
  {
    name: "Storage",
    items: [
      { id: "cacheUsage", type: "info", name: "Download cache", desc: "Elevation, imagery and map data kept on disk so places you visited load without downloading again: the space in use, the limit and the number of files." },
      { id: "cacheCapGb", type: "number", default: 20, min: 0, unit: "GB", name: "Cache size limit (GB)", desc: "The most the download cache may use, in GB: type a number and press Enter (0: no limit). Over the limit the files used longest ago are removed (never what this session used in the last hour), and pre-cached areas and routes are kept." },
      { id: "routeDownload", type: "select", default: "ahead", name: "Download along the GPS route", desc: "When you plan a GPS route, its elevation and road and building data are downloaded ahead in the background, so the drive loads faster. Ahead: the next 12 miles. Whole route: all of it (up to 300 miles). To keep a route for good, with its aerial photos, use Pre-cache route on the map's GPS box.",
        options: [ { label: "Off", value: "off" }, { label: "Ahead (12 miles)", value: "ahead" }, { label: "Whole route", value: "route" } ] },
      { id: "cacheDir", type: "path", what: "cache", name: "Cache folder", placeholder: "Folder for the download cache, or default", desc: "Where the download cache is kept, e.g. on another drive. Change... opens your system's folder window (it may open behind the game: Alt+Tab to it); or type or paste a folder below and press Use (default: .cache/beamng-earth in your home folder). Used from the next start of the app; move the old folder's contents there first to keep what is downloaded." },
      { id: "cacheClear", type: "action", name: "Clear cache", desc: "Deletes the downloaded elevation and imagery. What this session used and the gravity model stay. Places are downloaded again when you visit them." },
    ],
  },
  {
    name: "Feedback",
    items: [
      { id: "reportOpen", type: "feedback", what: "report", label: "Report", name: "Report an issue or idea", desc: "Closes this page, takes a screenshot, the location, the frame rate and your hardware and graphics settings, then opens a window to pick the kind (map issue, performance issue, bug, idea), give a title and describe it. Saves a ready-to-post GitHub issue (issue.md). Also Ctrl+Shift+E while driving." },
      { id: "profilerOpen", type: "feedback", what: "profiler", label: "Start profiler", name: "Performance profiler", desc: "Closes this page and records 30 s of frame times while you drive (every hitch with its place), then opens the same window as a performance issue. Also Ctrl+Shift+Y while driving." },
      { id: "reportPost", type: "text", name: "Posting it", desc: "After saving, the window shows the folder and opens the GitHub new-issue page. Edit issue.md and drag screenshot.jpg into the issue." },
    ],
  },
  {
    name: "About",
    items: [
      { id: "connection", type: "info", name: "Companion app", desc: "The connection to the BeamNG.Earth app (127.0.0.1:47613) that builds the world, and the session id it gave this game." },
      { id: "anchor", type: "info", name: "World origin", desc: "The current world origin (anchor) and its latitude / longitude. A rebase moves it under you." },
      { id: "player", type: "info", name: "Your position", desc: "Your latitude / longitude and the distance from the world origin." },
      { id: "weather", type: "info", name: "Weather data", desc: "Real weather from Open-Meteo.com, every 10 min or 25 km." },
      { id: "gravity", type: "info", name: "Gravity", desc: "The gravity in use: the real value (WGS84 normal plus EGM2008) or normal gravity by latitude." },
      { id: "deshadow", type: "info", name: "Imagery de-shadowing", desc: "The app's relighting of the shadows baked into the aerial photos." },
      { id: "colormatch", type: "info", name: "Imagery colour seams", desc: "The app's removal of the colour steps between photo flights, e.g. at state lines." },
      { id: "credits", type: "text", name: "Data credits now", desc: "The data credits for what is around you now, as drawn in the screen corner." },
      { id: "attribution", type: "text", name: "Data sources", desc: "Credits for the elevation and imagery sources in use." },
    ],
  },
  {
    name: "Developer",
    devPresets: true,
    items: [
      { id: "tint", type: "dev", name: "LOD tint", desc: "Tints every far tile by its level: green = matches the physics ground, cyan L0, blue L1, purple L2, orange to red coarser. Session only." },
      { id: "outlines", type: "dev", name: "Tile outlines", desc: "Outlines of the tiles near you, coloured by level or state: grey hidden-loaded, yellow in a crossover, magenta previous anchor, red doomed. Session only." },
      { id: "loading", type: "dev", name: "Loading heat", desc: "Queued tiles flash; the next 10 imports are numbered in the order they will load. Session only." },
      { id: "physics", type: "dev", name: "Physics block", desc: "The collidable terrain square, the pending one (dashed) and the terrain-follow prepare / commit squares. Session only." },
      { id: "physicsTerrain", type: "dev", name: "Show physics terrain", desc: "Renders the invisible physics terrain so it can be compared with the visible tiles. Session only." },
      { id: "rings", type: "dev", name: "Rings", desc: "Near-detail radii, the rebase and prefetch circles and the far-field square. Session only." },
      { id: "probe", type: "dev", name: "Under-car probe", desc: "A line at the car: physics ground vs the visible tile; red when the tile under the car is not the exact level or is more than 5 cm off. Session only." },
      { id: "stats", type: "dev", name: "Stats panel", desc: "Frame times, governor budget, imports and deletes per second, queue, objects, pops, failures, server backlog. Session only." },
      { id: "graphs", type: "dev", name: "Graphs panel", desc: "60 s plots of frame time, queue length, imports per second and speed. Session only." },
      { id: "map", type: "dev", name: "Mini-map panel", desc: "Top-down map of the tiles around you; click a tile to inspect it. Session only." },
      { id: "controls", type: "dev", name: "Controls panel", desc: "Overlay toggles, freeze LOD, force rebase, streaming budget, sky mode. Session only." },
      { id: "log", type: "dev", name: "Event log", desc: "The last 50 mod events (terrain commits, rebases, pops, failures, status). Session only." },
      { id: "devOutlineKm", type: "float", min: 0.5, max: 20, step: 0.5, default: 3, name: "Outline radius (km)", desc: "Tile outlines and loading heat are drawn within this distance of you." },
      { id: "devMapKm", type: "float", min: 1, max: 80, step: 1, default: 10, name: "Mini-map radius (km)", desc: "The mini-map's initial zoom." },
    ],
  },
]

// Each page's icon (the pause menu's side buttons take the icon's name, LayoutMenu's tabs the icon) and its button id
// (the first keeps the old Mods-tab button's id: settings.lua M.BUTTON_ID, pageExists)
export const PAGE_ICONS = { Graphics: "eyeOutlineOpened", World: "globe", Interface: "cogOutline",
  Multiplayer: "peopleOutline", Storage: "floppyDisk", Feedback: "bug", About: "info", Developer: "code" }
export const pageId = (name, i) => i === 0 ? "beamngEarth-settings" : "beamngEarth-settings-" + name.toLowerCase()

// The quality presets' cards (settings.lua PRESETS: what each sets)
export const qualityPresets = [
  { id: "low", name: "Low", short: "Thin forests, no grass, 40 km view", desc: "For older laptops and weak GPUs: fewer, nearer trees and buildings, no grass, coarser distant terrain, a lighter ground photo (1024), few lamp lights at night and a 40 km view distance." },
  { id: "medium", name: "Medium", short: "Lighter forests, light grass", desc: "Somewhat thinner and nearer forests, light grass and few lamp lights at night, the real horizon. The Steam Deck's preset." },
  { id: "high", name: "High", short: "The full look", desc: "Normal trees, buildings, terrain and grass, all lamp lights, the real horizon (BeamNG.Earth's look before presets existed)." },
  { id: "ultra", name: "Ultra", short: "Denser, farther forests", desc: "Denser and farther forests, buildings farther out, finer distant terrain, thick grass and more lamp lights at night. For strong GPUs." },
  { id: "extreme", name: "Extreme", short: "Everything at its most", desc: "Every setting at its most, out to the real horizon: the finest distant terrain, the sharpest ground photo (4096), the densest forests and buildings as far as they go, far grass and the most detailed objects. For the strongest PCs only: new places take longer to build and the frame rate drops." },
]

export const devPresets = [
  { id: "off", label: "Off" },
  { id: "lod", label: "LOD" },
  { id: "loading", label: "Loading" },
  { id: "physics", label: "Physics" },
  { id: "all", label: "All" },
]

export const config = ref({})
export const status = ref({})
export const dev = ref({ on: {}, lastPreset: "lod", active: false })
export const help = ref(null)          // the setting pointed at (the side card's help)
export const cacheConfirm = ref(false) // Clear cache: the confirmation is showing
export const cacheBusy = ref(false)    // Clear cache: sent, waiting for the server's answer
export const cacheResult = ref("")     // "Freed 1.2 GB" / an error
let cacheClearsAt = 0                  // status.cache.clears when the clear was sent
let cacheSentAt = 0
let lastWriteAt = 0 // a poll answered just after a uiSet may carry the value from before it
export const pathText = ref({ cache: "", beammp: "" }) // the typed paths
export const pathBusy = ref("")   // the path ('cache' / 'beammp') whose answer is awaited (its window open, or typed)
export const pathPick = ref(false) // ... from the system's window
export const pathMsg = ref({})    // path -> the server's last answer { ok, text }
let pathAnswers = 0               // status.paths.answers when it was sent
let pathSentAt = 0
export const optionConfig = { value: o => o.value, label: o => o.label }    // (BngSelect: our options' fields)
export const presetLine = computed(() => {
  const p = qualityPresets.find(q => q.id === config.value.quality)
  return p ? `${p.name}: ${p.desc}` : ""
})
export const visibleItems = category => category.items.filter(it => !it.showIf || it.showIf(config.value))
export const categoryNamed = name => settingsSchema.find(c => c.name === name) || settingsSchema[0]

// Extract global defaults (persisted settings only)
export const defaultSettings = {}
settingsSchema.forEach(cat => {
  cat.items.forEach(item => { if (item.default !== undefined && !item.virtual) defaultSettings[item.id] = item.default })
})

export const connectionText = computed(() => {
  const s = status.value
  if (!s.connection) return "Mod not loaded"
  let t = s.connection
  if (s.session) t += ` (session ${s.session})`
  if (s.lastError) t += ` - ${s.lastError}`
  return t
})

export const statusText = computed(() => status.value.status || "-")

// the connection banner's state line (core.banner(): starting / not running / lost / offline / degraded
// / connected, plus the edge guard's line) and its level (info / ok / warn / error)
export const bannerText = computed(() => (status.value.banner && status.value.banner.text) || "")
export const bannerLevel = computed(() => (status.value.banner && status.value.banner.level) || "info")
// a newer version on GitHub: { current, latest, url } (core.updateInfo)
export const update = computed(() => status.value.update || null)
export function openUpdate() {
  const url = update.value && update.value.url
  if (url) engineLua(`openWebBrowser(${JSON.stringify(url)})`)
}

export function fmt(v, digits) {
  return (typeof v === "number") ? v.toFixed(digits) : "-"
}

export function infoValue(item) {
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

function luaValue(val) {
  if (typeof val === "boolean") return val ? "true" : "false"
  if (typeof val === "number") return String(val)
  return JSON.stringify(String(val))
}

export function showWelcome() {
  engineLua("if beamngEarth_settings and beamngEarth_settings.showWelcome then beamngEarth_settings.showWelcome() end")
}

// Feedback buttons: settings.lua closes the menu, then opens the report window or starts the profiler
export function openFeedback(what) {
  engineLua(`if beamngEarth_settings and beamngEarth_settings.feedback then beamngEarth_settings.feedback(${JSON.stringify(what)}) end`)
}

export function engineLua(script, cb) {
  if (!window.bngApi || !window.bngApi.engineLua) return
  window.bngApi.engineLua(script, cb)
}

// The poll refreshes the values too: weather.lua / sky.lua switch Weather and Sky to Manual by
// themselves when they are changed in the game, and the dropdowns show it within 2 s.
export function applyState(state) {
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
export function pathNote(item) {
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

export function sendPath(item, pick, text) {
  pathMsg.value = { ...pathMsg.value, [item.what]: null }
  pathAnswers = (status.value.paths && status.value.paths.answers) || 0
  const script = `beamngEarth_settings and beamngEarth_settings.uiPathSet(${JSON.stringify(item.what)}, ${pick ? "true" : "false"}, ${JSON.stringify(text || "")}) or nil`
  engineLua(script, res => {
    if (res && res.ok) { pathBusy.value = item.what; pathPick.value = pick; pathSentAt = Date.now() }
    else pathMsg.value = { ...pathMsg.value, [item.what]: { ok: false, text: (res && res.error) || "mod not loaded" } }
  })
}

export function usePath(item) {
  const t = (pathText.value[item.what] || "").trim()
  if (t && !pathBusy.value) sendPath(item, false, t)
}

// typing in a text box: the game's keys off meanwhile
export function typing(on) { engineLua(`if setCEFTyping then setCEFTyping(${on ? "true" : "false"}) end`) }

export function refresh() {
  engineLua("beamngEarth_settings and beamngEarth_settings.uiState() or nil", applyState)
}

export function updateSetting(key) {
  const script = `beamngEarth_settings and beamngEarth_settings.uiSet(${JSON.stringify(key)}, ${luaValue(config.value[key])}) or nil`
  lastWriteAt = Date.now()
  engineLua(script, res => {
    if (res && res.value !== undefined) config.value[key] = res.value // the Lua side clamps
    if (res && res.quality) config.value.quality = res.quality         // (a graphics knob moved: Custom, or a preset)
  })
}

export function resetSetting(item) {
  config.value[item.id] = item.default
  updateSetting(item.id)
}

// A typed number (type "number"): applied on Enter or when the box loses focus; anything else puts the value back.
export function onNumberEnter(item, text) {
  const n = Number(String(text).trim())
  if (String(text).trim() === "" || !Number.isFinite(n)) return false
  config.value[item.id] = Math.max(item.min ?? -Infinity, Math.min(item.max ?? Infinity, n))
  updateSetting(item.id)
  return true
}

export function onSliderChange(item, val) {
  config.value[item.id] = val
  updateSetting(item.id)
}

export function onSwitchChange(item, val) {
  config.value[item.id] = val
  updateSetting(item.id)
}

export function onDevChange(item, val) {
  dev.value.on[item.id] = val
  engineLua(`beamngEarth_settings and beamngEarth_settings.uiDevSet(${JSON.stringify(item.id)}, ${luaValue(val)}) or nil`, state => {
    if (state) dev.value = state
  })
}

export function applyDevPreset(name) {
  engineLua(`beamngEarth_settings and beamngEarth_settings.uiDevPreset(${JSON.stringify(name)}) or nil`, state => {
    if (state) dev.value = state
  })
}

// Clear cache: the button asks first (cacheConfirm), Clear sends it; the poll sees the answer
export function askClearCache() {
  cacheResult.value = ""
  cacheConfirm.value = true
}

export function confirmClearCache() {
  cacheConfirm.value = false
  cacheClearsAt = (status.value.cache && status.value.cache.clears) || 0
  engineLua("beamngEarth_settings and beamngEarth_settings.uiCacheClear() or nil", res => {
    if (res && res.ok) { cacheBusy.value = true; cacheSentAt = Date.now() }
    else cacheResult.value = (res && res.error) || "mod not loaded"
  })
}

export function selectOption(item, val) {
  if (item.id === "grass") { setGrass(val); return }
  config.value[item.id] = val
  updateSetting(item.id)
}

// A select's value: the Grass control is two settings (groundCover on / off and groundCoverDensity)
export function valueOf(item) {
  if (item.id === "grass") return config.value.groundCover === false ? "off" : (config.value.groundCoverDensity || "normal")
  return config.value[item.id]
}

export function setGrass(val) {
  if (val === "off") {
    config.value.groundCover = false
    updateSetting("groundCover")
  } else {
    config.value.groundCover = true
    config.value.groundCoverDensity = val
    updateSetting("groundCoverDensity")
    updateSetting("groundCover")
  }
}

// A preset card: settings.lua setQuality sets its knobs and answers with every value
export function setQuality(id) {
  lastWriteAt = Date.now()
  config.value.quality = id
  engineLua(`beamngEarth_settings and beamngEarth_settings.uiSet("quality", ${JSON.stringify(id)}) or nil`, res => {
    if (res && res.settings) config.value = { ...defaultSettings, ...res.settings }
  })
}

export function presetName(id) {
  const p = qualityPresets.find(q => q.id === id)
  return p ? p.name : id
}

// the preset the first run's detection picked ("Steam Deck: medium")
export const detectedPick = computed(() => {
  const d = config.value.qualityDetected || ""
  const k = d.lastIndexOf(": ")
  return k >= 0 ? d.slice(k + 2) : ""
})

// Back: to the menu the page was opened from (the pause menu in a level, else the main menu)
export function close() {
  engineLua("if beamngEarth_settings and beamngEarth_settings.closeSettings then beamngEarth_settings.closeSettings() end")
}

// The poll, while any settings page is open (started / stopped by each: a count)
let users = 0
let pollTimer = null
export function startPolling() {
  if (users++ === 0) {
    if (!Object.keys(config.value).length) config.value = { ...defaultSettings }
    refresh()
    pollTimer = setInterval(refresh, 2000) // status lines while a page is open
  }
}
export function stopPolling() {
  if (--users <= 0) {
    users = 0
    if (pollTimer) clearInterval(pollTimer)
    pollTimer = null
  }
}
