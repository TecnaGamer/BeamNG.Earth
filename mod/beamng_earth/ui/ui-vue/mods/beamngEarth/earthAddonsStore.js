// The Add-ons page's state (EarthAddons.vue). Lua side: settings.lua's uiAddon* functions -> addons.lua; the server's
// addons.py lists the curated add-ons, sends an add-on's README and changelog as safe HTML (our own tags only, links
// as indexes into a list of https URLs) and downloads a release; addons.lua hands the file to the game's mod manager.
// The game's UI browser cannot fetch anything itself: everything comes through Lua.
import { ref, computed } from "vue"
import { engineLua, openLink } from "./earthSettingsStore.js"

export const cards = ref([])
export const loaded = ref(false)
export const listError = ref("")
export const connected = ref(false)
export const inMp = ref(false)
export const openId = ref("")          // the add-on whose page is open ("" = the list)
export const detail = ref(null)        // its README and changelog (the server's addon_detail)
export const consentFor = ref("")      // the add-on whose notice is showing (Install pressed the first time)
export const actionMsg = ref({})       // id -> the last refusal from Lua ("needs your agreement first", ...)
export const adding = ref(null)        // Add from GitHub: { state, text, id } (addons.lua)
let detailSerial = -1
let timer = null
let users = 0

export const openCard = computed(() => cards.value.find(c => c.id === openId.value) || null)

function luaStr(s) { return JSON.stringify(String(s)) }

function call(script, cb) {
  // (with a callback the game evaluates an expression; without one it runs a statement, and "a and b or nil" alone is a
  // Lua syntax error: opening an add-on showed a Lua error, 2026-11-07)
  if (cb) engineLua(`beamngEarth_settings and beamngEarth_settings.${script} or nil`, cb)
  else engineLua(`if beamngEarth_settings then beamngEarth_settings.${script} end`)
}

export function poll() {
  call("uiAddons()", res => {
    if (!res || res.ok === false) {
      listError.value = (res && res.error) || "BeamNG.Earth is not loaded"
      return
    }
    cards.value = Array.isArray(res.addons) ? res.addons : []
    loaded.value = res.loaded === true
    listError.value = res.error || ""
    connected.value = res.connected === true
    inMp.value = res.mp === true
    adding.value = res.adding || null
    const c = openCard.value
    if (c && c.detailSerial !== detailSerial) fetchDetail()
  })
}

function fetchDetail() {
  const id = openId.value
  if (!id) return
  call(`uiAddonDetail(${luaStr(id)})`, res => {
    if (res && res.id === id) {
      detail.value = res
      detailSerial = (openCard.value && openCard.value.detailSerial) || 0
    }
  })
}

export function refresh() {
  call("uiAddonsRefresh()", () => setTimeout(poll, 300))
  if (openId.value) call(`uiAddonOpen(${luaStr(openId.value)}, true)`)
}

export function open(id) {
  openId.value = id
  detail.value = null
  detailSerial = -1
  call(`uiAddonOpen(${luaStr(id)}, false)`)
}

export function back() {
  openId.value = ""
  detail.value = null
  consentFor.value = ""
}

// Install / Update: the first time for an add-on, its notice first (the author, not made by BeamNG.Earth)
export function install(card) {
  if (!card.consented && consentFor.value !== card.id) {
    consentFor.value = card.id
    return
  }
  const agreed = consentFor.value === card.id
  consentFor.value = ""
  call(`uiAddonInstall(${luaStr(card.id)}, ${agreed ? "true" : "false"})`, res => answer(card.id, res))
}

export function cancelConsent() { consentFor.value = "" }

// Add from GitHub: the pasted repository put on the player's own list (the server reads it on GitHub)
export function addRepo(text) {
  const t = String(text || "").trim()
  if (!t) return
  call(`uiAddonAdd(${luaStr(t)})`, res => {
    if (res && res.ok === false) adding.value = { state: "error", text: res.error || "Could not add it" }
    setTimeout(poll, 300)
  })
}

// Options tab: the values as a Lua table (the game's jsonDecode); the server checks each against its option
export function saveOptions(card, values) {
  const json = JSON.stringify(values || {})
  call(`uiAddonOptions(${luaStr(card.id)}, jsonDecode(${luaStr(json)}))`, res => answer(card.id, res))
}

export function forget(card) {
  call(`uiAddonForget(${luaStr(card.id)})`, res => {
    answer(card.id, res)
    back()
  })
}

export function manage(card, op) {
  call(`uiAddonManage(${luaStr(card.id)}, ${luaStr(op)})`, res => answer(card.id, res))
}

function answer(id, res) {
  actionMsg.value = { ...actionMsg.value, [id]: res && res.ok === false ? (res.error || "Could not start") : "" }
  setTimeout(poll, 200)
}

// A link in the README / changelog: <a class="md-link" data-i="k"> -> links[k], opened in the browser by the server
export function onTextClick(ev, links) {
  const a = ev.target && ev.target.closest ? ev.target.closest("a.md-link") : null
  if (!a) return
  ev.preventDefault()
  const i = Number(a.getAttribute("data-i"))
  const url = Array.isArray(links) ? links[i] : null
  if (typeof url === "string") openLink(url)
}

export function openRepo(card) { if (card && card.url) openLink(card.url) }
export function openDownload(url) { openLink(url) }

// The card's state line: "Installed build-141 (update: build-142)", "Not installed", ...
export function stateLine(c) {
  if (c.kind === "companion") {
    if (!c.installed) return c.latest ? `Runs beside the game (latest: ${c.latest})` : "Runs beside the game"
    let t = `Installed ${c.version || ""}`.trim() + (c.running ? ", running" : ", not running")
    if (c.update && c.latest) t += ` (update: ${c.latest})`
    return t
  }
  if (c.kind !== "beamng-mod") return "Download"
  if (!c.installed) return c.latest ? `Not installed (latest: ${c.latest})` : "Not installed"
  let t = `Installed ${c.version || ""}`.trim()
  if (c.active === false) t += ", turned off"
  if (c.update && c.latest) t += ` (update: ${c.latest})`
  return t
}

// A list from Lua: an array, or (Lua's tables reach the page as objects when empty or sparse) the object's values
export function lines(v) {
  if (Array.isArray(v)) return v
  if (v && typeof v === "object") return Object.values(v).filter(x => typeof x === "string")
  return []
}

export function size(n) {
  if (!n) return ""
  if (n >= 1 << 20) return `${(n / (1 << 20)).toFixed(1)} MB`
  return `${Math.max(1, Math.round(n / 1024))} KB`
}

export function day(iso) {
  const d = typeof iso === "string" ? iso.slice(0, 10) : ""
  return d
}

export function progressText(st) {
  if (!st) return ""
  if (st.state === "progress" && st.total > 0) return `${st.text} ${Math.floor((100 * st.done) / st.total)} %`
  return st.text || ""
}

export function start() {
  if (users++ === 0) {
    poll()
    timer = setInterval(poll, 1500)
  }
}

export function stop() {
  if (--users <= 0) {
    users = 0
    if (timer) clearInterval(timer)
    timer = null
  }
}
