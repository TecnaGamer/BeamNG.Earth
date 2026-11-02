// A "BeamNG.Earth" category in BeamMP's server browser (main menu > More > BeamMP), listing the BeamNG.Earth servers
// (the user, 2026-10-24: "you can add it as a new tab that just shows a list of .earth servers"; 2026-10-25: "use the
// official list (look at the other categories) and make the category have its own unique color and have my official
// server appear at the top of the list with a special color"). Those servers are Private in BeamMP, so players
// without BeamNG.Earth never see them; the list comes from our list service through beamngEarth_serverlist (Lua,
// polled here).
//
// BeamMP's own categories (Official, Featured, Partner, Favorites) are views of one page, its servers view
// (menu.beammp.servers/:view), over the one list in its shared state (beammpState.js). Ours is a view there too,
// "earth": the button goes into BeamMP's sidebar with the other categories when its page shows (a MutationObserver:
// BeamMP's sidebar has no way for another mod to add one, and its files are not ours to change), and while that view
// is open BeamMP's list holds our servers (its own list kept aside and put back on leaving), so the table, details,
// search, filters, sorting, favorites, recents and Connect are BeamMP's own. A list BeamMP receives meanwhile (its
// Refresh) is kept aside the same way, and ours asked again. Our rows get the category's colour, the official
// server's (serverlist.lua OFFICIAL, by address) its own and the top of the list. If BeamMP's page ever changes
// shape, the button just doesn't appear.
import { toRaw, watch } from "vue"
import { useBridge } from "@/bridge"

const BEAMMP_STATE = "/ui/ui-vue/mods/BeamMP/shared/beammpState.js"
const SERVERS_ROUTE = "menu.beammp.servers"
const VIEW = "earth"
const POLL_MS = 300
const BROWSER_ROUTES = new Set(["menu.beammp.servers", "menu.beammp.direct", "menu.beammp.current-server", "menu.beammp.tiles"])
// the category's colour (teal: none of BeamMP's own, orange / green / blue / yellow) and the official server's (violet)
const EARTH_RGB = "20, 184, 166"
const OFFICIAL_RGB = "168, 85, 247"

let observer = null
let style = null
let bmp = null              // BeamMP's state (useBeamMPState) once its page has shown
let stopWatches = []
let ours = []               // our servers, in BeamMP's server shape
let ourLists = new WeakSet()   // the (raw) arrays of ours put into BeamMP's list
let showing = false         // ours is in BeamMP's list
let kept = null             // BeamMP's own list while ours is shown
let hooking = null
let pollTimer = null
let officialNames = new Set()

const css = `
.beammp-route .sidebar .nav-btn.bnge-earth-tab { border-left: 0.22rem solid rgba(${EARTH_RGB}, 0.95); }
.beammp-route .sidebar .nav-btn.bnge-earth-tab:hover { border-color: rgba(${EARTH_RGB}, 0.8); background: rgba(${EARTH_RGB}, 0.2);
  border-left-color: rgba(${EARTH_RGB}, 0.95); }
.beammp-route .sidebar .nav-btn.bnge-earth-tab.bnge-active { border-color: rgba(${EARTH_RGB}, 0.9); background: rgba(${EARTH_RGB}, 0.28);
  box-shadow: inset 0.22rem 0 rgb(${EARTH_RGB}); }
.beammp-route .servers-wrap.bnge-earth-view tr.server-row { background: rgba(${EARTH_RGB}, 0.16);
  box-shadow: inset 0.22rem 0 rgba(${EARTH_RGB}, 0.95); }
.beammp-route .servers-wrap.bnge-earth-view tr.server-row:hover { background: rgba(${EARTH_RGB}, 0.26); }
.beammp-route .servers-wrap.bnge-earth-view tr.server-row.bnge-official { background: rgba(${OFFICIAL_RGB}, 0.26);
  box-shadow: inset 0.32rem 0 rgb(${OFFICIAL_RGB}); font-weight: 600; }
.beammp-route .servers-wrap.bnge-earth-view tr.server-row.bnge-official:hover { background: rgba(${OFFICIAL_RGB}, 0.36); }
`

const strip = name => String(name || "").replace(/\^[0-9a-frlmnop*]/gi, "")

// "a,b" as BeamMP's own list shows tags (beammpState formatServerTags; its colour themes left out)
const tagsList = tags => String(tags || "").split(",").map(t => t.trim()).filter(Boolean)
  .map(raw => { const parts = raw.split(":"); return { raw, text: parts[1] || raw, prefix: parts[0], theme: null } })

// One server of our list (serverlist.lua toServers) in the shape BeamMP's list holds (beammpState normalizeServer).
// The official ones sort first: BeamMP's views order by sortName.
function toBeamMP(s, i) {
  const name = s.name || `${s.ip}:${s.port}`
  const stripped = strip(name)
  const endpoint = `${s.ip}:${s.port}`
  return {
    ip: s.ip, port: s.port, sname: name, name, strippedName: stripped,
    sortName: (s.official ? "0 " : "1 ") + stripped.toLowerCase(),
    map: "BeamNG.Earth", mapName: "BeamNG.Earth",
    players: s.players, maxplayers: s.maxPlayers, playerslist: s.playerslist || "",
    version: s.version || "", location: s.location || "", owner: "",
    // the server's own description (ServerConfig.toml), else where its players are
    sdesc: s.desc || (s.place ? `The world around ${s.place}` : ""),
    tags: s.tags || "", tagsList: tagsList(s.tags), modlist: s.modlist || "", modstotalsize: s.modstotalsize || 0,
    modstotal: s.modstotal || 0, pass: !!s.pass,
    endpoint, id: `${endpoint}#bnge${i}`, bngeEarth: true, bngeOfficial: !!s.official,
  }
}

function inEarthView() {
  return !!bmp && bmp.state.view.value === VIEW && String(window.vueRouter?.currentRoute?.value?.name || "") === SERVERS_ROUTE
}

// ours into BeamMP's list (its own kept aside), or its own back
function place() {
  if (!bmp) return
  const list = bmp.state.servers
  const now = toRaw(list.value)
  if (inEarthView()) {
    if (!ourLists.has(now)) kept = now          // BeamMP's own (also one it received meanwhile)
    if (now !== ours) {
      ourLists.add(ours)
      list.value = ours
    }
    showing = true
  } else if (showing) {
    if (ourLists.has(now)) list.value = kept || []
    showing = false
    kept = null
  }
}

async function poll() {
  try {
    // (an expression evaluated with its value passed back, as BeamMP's own page asks Lua: the bridge's lua.extensions
    // proxy did not reach this extension)
    const st = await new Promise(resolve =>
      window.bngApi.engineLua("beamngEarth_serverlist and beamngEarth_serverlist.state()", resolve))
    if (st && st.state === "loading") pollTimer = setTimeout(poll, POLL_MS)
    const list = Array.isArray(st?.servers) ? st.servers : Object.values(st?.servers || {})
    ours = list.map(toBeamMP)
    officialNames = new Set(ours.filter(s => s.bngeOfficial).map(s => s.strippedName))
    place()
  } catch (e) {
    console.error("BeamNG.Earth: the server list", e)
  }
}

function refresh() {
  clearTimeout(pollTimer)
  window.bngApi.engineLua("if beamngEarth_serverlist then beamngEarth_serverlist.refresh() end")
  pollTimer = setTimeout(poll, 100)
}

// BeamMP's state, once: the view and its list watched
function hook() {
  if (!hooking) hooking = hookOnce().catch(e => { hooking = null; throw e })
  return hooking
}

async function hookOnce() {
  const mod = await import(BEAMMP_STATE)
  bmp = mod.useBeamMPState(useBridge().events)
  stopWatches.push(watch(bmp.state.view, view => {
    if (view === VIEW) refresh()
    place()
    mark()
  }))
  stopWatches.push(watch(bmp.state.servers, list => {
    // a list BeamMP received while ours shows (its Refresh, a login): kept aside, ours asked again
    if (inEarthView() && !ourLists.has(toRaw(list))) {
      place()
      refresh()
    }
  }))
  if (window.vueRouter?.afterEach) stopWatches.push(window.vueRouter.afterEach(() => { place(); mark() }))
  if (inEarthView()) { refresh(); place() }
}

// the category button lit in our view, the table marked as ours, the official server's rows marked
function mark() {
  const on = inEarthView()
  for (const b of document.querySelectorAll(".bnge-earth-tab")) {
    b.classList.toggle("bnge-active", on)
    b.classList.toggle("active", false)     // (BeamMP's orange "active" is for its own categories)
  }
  for (const w of document.querySelectorAll(".beammp-route .servers-wrap")) {
    if (w.classList.contains("bnge-earth-view") !== on) w.classList.toggle("bnge-earth-view", on)
  }
  if (!on) return
  for (const row of document.querySelectorAll(".beammp-route .servers-wrap tr.server-row")) {
    const title = row.querySelector(".title-cell .server-title-content")
    const official = !!title && officialNames.has(title.textContent.trim())
    if (row.classList.contains("bnge-official") !== official) row.classList.toggle("bnge-official", official)
  }
}

// BeamMP's sidebar on screen without our button: put it in, after its own categories
function inject() {
  const sidebar = document.querySelector(".beammp-route .sidebar, main.main-grid > aside.sidebar")
  if (!sidebar || sidebar.querySelector(".bnge-earth-tab") || !sidebar.querySelector('img[src*="BeamMP"]')) return
  const after = sidebar.querySelector(".nav-btn.category-favorite") || sidebar.querySelector(".nav-btn.category-partner")
  const spacer = sidebar.querySelector(".spacer")
  if (!after && !spacer) return
  const button = document.createElement("button")
  button.className = "nav-btn bnge-earth-tab"
  button.textContent = "BeamNG.Earth"
  button.title = "BeamNG.Earth servers. Server countries: IP Geolocation by DB-IP (db-ip.com, CC BY 4.0)"
  // BeamMP's styles are scoped to its own elements (Vue's data-v-* marks): the same marks give ours its look
  const sample = sidebar.querySelector(".nav-btn")
  if (sample) for (const a of sample.attributes) if (a.name.startsWith("data-v-")) button.setAttribute(a.name, "")
  button.addEventListener("click", async () => {
    // BeamMP's own gates first (its launcher, terms of service and login pages): the category opens from its browser
    const name = String(window.vueRouter?.currentRoute?.value?.name || "")
    if (!BROWSER_ROUTES.has(name)) return
    await hook()
    bmp.setView(VIEW)
    await window.vueRouter.replace({ name: SERVERS_ROUTE, params: { view: VIEW } })
    place()
    mark()
  })
  if (after) after.after(button)
  else sidebar.insertBefore(button, spacer)
}

export function start() {
  if (observer || typeof MutationObserver === "undefined") return
  style = document.createElement("style")
  style.textContent = css
  document.head.append(style)
  observer = new MutationObserver(() => {
    if (!document.querySelector(".beammp-route")) return
    inject()
    if (!bmp && document.querySelector(".beammp-route .sidebar")) hook().catch(e => console.error("BeamNG.Earth: BeamMP's state", e))
    mark()
  })
  // (class changes too: Vue sets a row's whole class list again when its selection changes)
  observer.observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ["class"] })
  inject()
}

export function stop() {
  if (observer) observer.disconnect()
  observer = null
  clearTimeout(pollTimer)
  for (const s of stopWatches) try { s() } catch (e) { /* gone */ }
  stopWatches = []
  if (bmp && showing && ourLists.has(toRaw(bmp.state.servers.value))) bmp.state.servers.value = kept || []
  showing = false
  kept = hooking = bmp = null
  if (style) style.remove()
  style = null
  for (const b of document.querySelectorAll(".bnge-earth-tab")) b.remove()
  for (const w of document.querySelectorAll(".bnge-earth-view")) w.classList.remove("bnge-earth-view")
}
