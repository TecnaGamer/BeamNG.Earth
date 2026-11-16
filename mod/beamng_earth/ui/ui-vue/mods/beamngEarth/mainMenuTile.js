// BeamNG.Earth's tile on the main menu, in the place of the stock "Quick-Start Experiences" tile (the big top-left
// one): anyone with BeamNG.Earth has done Quick-Start or never will. A click starts BeamNG.Earth in freeroam at
// "Where you left off" (levelinfo.lua's spawn_default), the same call the level list's Start button makes
// (levelTiles.lua: freeroam_freeroam.startFreeroamByName(levelName, spawnPointName)). Quick-Start stays reachable
// from the menu's Discover page.
//
// How: the stock tile (MainGridView.vue `.tile-experiences`, a Huge2) is cloned, so the copy keeps the stock look
// (its scoped styles, the skew, the hover), then given Earth's preview picture, the globe icon and the title, with no
// "New!" tag, and the stock tile is hidden. Nothing else on the menu is touched (other mods' cards, e.g. RLS's or
// Hustle on Earth's, sit outside this row). Plain DOM: any failure leaves the stock tile where it was.
const TAG = "[beamngEarth tile]"
export const TILE_ID = "beamngEarth-mainmenu-tile"
export const MENU_ROUTES = new Set(["menu", "menu.others", "menu.discover"])
export const LEVEL = "beamng_earth"
export const SPAWN = "spawn_default"
export const PREVIEW = "/levels/beamng_earth/beamng_earth_preview.jpg"
export const TITLE = "BeamNG.Earth"
const GLOBE_GLYPH = "\uECA2" // bngIcons "globe" (assets/fonts/bngIcons/bngIcons.js)
const HIDDEN_ATTR = "data-beamng-earth-hidden"

// the Lua the click runs: the level list's own start, at the "Where you left off" spawn point
export const START_LUA = `freeroam_freeroam.startFreeroamByName(${JSON.stringify(LEVEL)}, ${JSON.stringify(SPAWN)})`
// one quick connect to the companion server's local port (the game allows sockets to the local host)
export const SERVER_LUA = "(function() local ok, up = pcall(function() local s = require('socket').tcp() " +
  "s:settimeout(0.25) local r = s:connect('127.0.0.1', 47613) s:close() return r == 1 end) " +
  "return (ok and up == true) and 'up' or 'down' end)()"
const SERVER_POLL_MS = 3000

export function isMenuRoute(route) { return MENU_ROUTES.has(route && route.name) }

let stopWatch = null
let observer = null
let tile = null
let serverUp = null

function api() { return window.bngApi || window.beamng }

let pollTimer = null
function checkServer() {
  const a = api()
  if (!a || !a.engineLua) return
  try {
    a.engineLua(SERVER_LUA, up => {
      serverUp = up === "up"
      paintServer()
    })
  } catch (e) { /* no answer: no note */ }
}
// while the menu is up: the label follows the server (started or stopped with the menu open)
function startPolling() {
  if (pollTimer) return
  checkServer()
  pollTimer = setInterval(checkServer, SERVER_POLL_MS)
}
function stopPolling() { pollTimer && clearInterval(pollTimer); pollTimer = null }

function paintServer() {
  if (!tile) return
  let note = tile.querySelector(".bne-server-note")
  if (serverUp !== false) { note && note.remove(); return }
  if (!note) {
    note = document.createElement("div")
    note.className = "bne-server-note"
    note.style.cssText = "margin-left:auto;align-self:center;padding:.2em .6em;border-radius:.25em;" +
      "background:rgba(0,0,0,.65);color:#ffb08a;font-size:.55em;font-style:italic;white-space:nowrap"
    note.textContent = "Server not running"
    ;(tile.querySelector(".label-container") || tile).append(note)
  }
}

// Started from the menu, the level loads behind it and the UI stays on the menu route (the level list leaves the
// menu first). So: once the game state is freeroam, go to the play route, as the level list's start ends up.
const STATE_LUA = "(core_gamestate and core_gamestate.state and core_gamestate.state.state) or ''"
function start() {
  const a = api()
  if (!a || !a.engineLua) return
  a.engineLua(START_LUA)
  const t0 = Date.now()
  const poll = () => a.engineLua(STATE_LUA, st => {
    const r = window.vueRouter
    if (!r || !isMenuRoute(r.currentRoute.value)) return          // the player went elsewhere: leave it
    if (st === "freeroam") { r.push({ name: "play" }); return }
    if (Date.now() - t0 < 180000) setTimeout(poll, 500)
  })
  setTimeout(poll, 1500)
}

// the copy of the stock tile, made Earth's
export function makeTile(stock) {
  const t = stock.cloneNode(true)
  t.id = TILE_ID
  t.classList.remove("tile-experiences")
  t.classList.add("tile-experiences-earth")
  t.removeAttribute(HIDDEN_ATTR)
  t.removeAttribute("bng-scoped-nav-autofocus")
  t.style.removeProperty("display")
  t.style.flexGrow = "4"   // the stock .tile-experiences width (the class is dropped so the stock selectors miss the copy)
  t.querySelectorAll(".tag").forEach(e => e.remove())
  t.classList.add("has-image")
  t.style.setProperty("--artwork-image", `url('${PREVIEW}')`)
  if (!t.querySelector(".artwork-image")) {
    const art = t.querySelector(".artwork")
    if (art) { const z = document.createElement("div"); z.className = "zoom artwork-image"; art.prepend(z) }
  }
  const icon = t.querySelector(".icon-inline")
  if (icon) icon.textContent = GLOBE_GLYPH
  const text = t.querySelector(".label-container .text")
  if (text) text.textContent = TITLE
  t.setAttribute("title", "Start BeamNG.Earth in freeroam, where you left off")
  t.addEventListener("click", start)
  t.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); start() } })
  return t
}

function mount() {
  const stock = document.querySelector(".mainmenu-container .stage > .primary .row-big > .tile-experiences")
  if (!stock) return false
  if (tile && tile.isConnected && tile.previousElementSibling === stock) return true
  if (tile) tile.remove()
  tile = makeTile(stock)
  stock.setAttribute(HIDDEN_ATTR, "1")
  stock.style.display = "none"
  stock.after(tile)
  paintServer()
  return true
}

function unmount() {
  if (tile) tile.remove()
  tile = null
  document.querySelectorAll(`[${HIDDEN_ATTR}]`).forEach(e => { e.style.removeProperty("display"); e.removeAttribute(HIDDEN_ATTR) })
}

function watchDom() {
  if (observer) return
  // the menu re-renders its rows (route changes inside it, a language change): put the tile back each time
  observer = new MutationObserver(() => { try { mount() } catch (e) { console.error(TAG, e) } })
  observer.observe(document.body, { childList: true, subtree: true })
}
function unwatchDom() { observer && observer.disconnect(); observer = null }

function onRoute(route) {
  try {
    if (!isMenuRoute(route)) { stopPolling(); unwatchDom(); unmount(); return }
    mount()
    watchDom()
    startPolling()
  } catch (e) { console.error(TAG, e); stopPolling(); unwatchDom(); unmount() }
}

export async function startTile(waitMs = 60000) {
  const t0 = Date.now()
  while (!(window.Vue && window.vueRouter)) {
    if (Date.now() - t0 > waitMs) { console.error(TAG, "no router: tile not added"); return }
    await new Promise(r => setTimeout(r, 100))
  }
  stopTile()
  stopWatch = window.Vue.watch(window.vueRouter.currentRoute, onRoute, { immediate: true, flush: "post" })
}

export function stopTile() {
  try { stopWatch && stopWatch() } catch (e) { /* already gone */ }
  stopWatch = null
  stopPolling()
  unwatchDom()
  unmount()
}
