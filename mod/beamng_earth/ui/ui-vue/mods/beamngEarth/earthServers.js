// A "BeamNG.Earth" tab in BeamMP's server browser (main menu > More > BeamMP), listing the BeamNG.Earth servers (the
// user, 2026-10-24: "you can add it as a new tab that just shows a list of .earth servers"). Those servers are Private
// in BeamMP, so players without BeamNG.Earth never see them; the list comes from our list service through
// beamngEarth_serverlist (Lua, polled here).
//
// BeamMP's sidebar is fixed in its layout (BeamMPMain.vue) with no way for another mod to add a tab, and its files are
// not ours to change: the button is put into its sidebar when BeamMP's page shows (a MutationObserver), and the list
// is drawn in BeamMP's content panel over its own view, so BeamMP's sidebar and its "connecting / downloading mods"
// overlay stay. Joining goes through BeamMP's own connectToServer (its overlay, recents and mod downloads as for any
// server). If BeamMP's page ever changes shape, the button just doesn't appear.
import { useBridge } from "@/bridge"

const BEAMMP_STATE = "/ui/ui-vue/mods/BeamMP/shared/beammpState.js"
const POLL_MS = 300
const BROWSER_ROUTES = new Set(["menu.beammp.servers", "menu.beammp.direct", "menu.beammp.current-server", "menu.beammp.tiles"])
let observer = null
let panel = null            // our list's element while the tab is open
let pollTimer = null

const css = `
.bnge-servers { padding: 1rem 1.25rem; color: #fff; font-family: inherit; height: 100%; overflow: auto; }
.bnge-servers h2 { margin: 0 0 .25rem; font-size: 1.4rem; }
.bnge-servers .bnge-note { opacity: .75; margin: 0 0 1rem; font-size: .9rem; }
.bnge-servers .bnge-bar { display: flex; gap: .5rem; align-items: center; margin-bottom: .75rem; }
.bnge-servers button { background: rgba(255,255,255,.12); color: #fff; border: 0; border-radius: .25rem;
  padding: .4rem .9rem; cursor: pointer; font: inherit; }
.bnge-servers button:hover { background: rgba(255,103,0,.8); }
.bnge-servers table { width: 100%; border-collapse: collapse; }
.bnge-servers th { text-align: left; font-weight: 600; opacity: .7; padding: .35rem .5rem; font-size: .85rem; }
.bnge-servers td { padding: .5rem; border-top: 1px solid rgba(255,255,255,.08); }
.bnge-servers tr:hover td { background: rgba(255,255,255,.05); }
.bnge-servers .bnge-empty { opacity: .75; padding: 1rem .5rem; }
`

function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag)
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "onclick") e.addEventListener("click", v)
    else e.setAttribute(k, v)
  }
  for (const k of kids) e.append(k instanceof Node ? k : document.createTextNode(String(k)))
  return e
}

async function join(server) {
  try {
    const mod = await import(BEAMMP_STATE)
    const { events } = useBridge()
    const st = mod.useBeamMPState(events)
    closePanel()                // BeamMP's own view back under its overlay
    await st.connectToServer(server.ip, server.port, server.name)
  } catch (e) {
    console.error("BeamNG.Earth: joining through BeamMP failed, direct connect instead", e)
    window.bngApi.engineLua(`MPCoreNetwork.connectToServer(${window.bngApi.serializeToLua(server.ip)}, ${Number(server.port)}, ${window.bngApi.serializeToLua(server.name)})`)
  }
}

function render(st) {
  if (!panel) return
  const body = panel.querySelector(".bnge-body")
  body.replaceChildren()
  if (st.state === "loading" && !(st.servers || []).length) {
    body.append(el("div", { class: "bnge-empty" }, "Loading BeamNG.Earth servers ..."))
    return
  }
  if (st.state === "error") body.append(el("div", { class: "bnge-empty" }, `Could not load the list: ${st.error}`))
  const servers = st.servers || []
  if (!servers.length) {
    if (st.state !== "error") body.append(el("div", { class: "bnge-empty" }, "No BeamNG.Earth servers are running right now."))
    return
  }
  const table = el("table", {},
    el("thead", {}, el("tr", {}, el("th", {}, "Server"), el("th", {}, "Players"), el("th", {}, "Version"), el("th", {}, ""))))
  const tb = el("tbody")
  for (const s of servers) {
    tb.append(el("tr", {},
      el("td", {}, s.name || `${s.ip}:${s.port}`, s.place ? el("div", { class: "bnge-note" }, s.place) : ""),
      el("td", {}, `${s.players}${s.maxPlayers ? " / " + s.maxPlayers : ""}`),
      el("td", {}, s.version || ""),
      el("td", {}, el("button", { onclick: () => join(s) }, "Join"))))
  }
  table.append(tb)
  body.append(table)
}

async function poll() {
  try {
    // (an expression evaluated with its value passed back, as BeamMP's own page asks Lua: the bridge's lua.extensions
    // proxy did not reach this extension)
    const st = await new Promise(resolve =>
      window.bngApi.engineLua("beamngEarth_serverlist and beamngEarth_serverlist.state()", resolve))
    render(st || {})
    if (st && st.state === "loading") pollTimer = setTimeout(poll, POLL_MS)
  } catch (e) {
    render({ state: "error", error: "BeamNG.Earth is not loaded" })
  }
}

function refresh() {
  clearTimeout(pollTimer)
  window.bngApi.engineLua("if beamngEarth_serverlist then beamngEarth_serverlist.refresh() end")
  pollTimer = setTimeout(poll, 100)
}

function openPanel(content, button) {
  if (panel) return
  for (const c of content.children) {
    c.dataset.bngeHidden = c.style.display || "-"
    c.style.display = "none"
  }
  panel = el("div", { class: "bnge-servers" },
    el("style", {}, css),
    el("h2", {}, "BeamNG.Earth servers"),
    el("p", { class: "bnge-note" }, "Servers running the real world. They only show here, for players with BeamNG.Earth."),
    el("div", { class: "bnge-bar" }, el("button", { onclick: refresh }, "Refresh")),
    el("div", { class: "bnge-body" }))
  content.append(panel)
  for (const b of button.parentElement.querySelectorAll(".nav-btn")) b.classList.remove("active")
  button.classList.add("active")
  refresh()
}

function closePanel() {
  clearTimeout(pollTimer)
  if (!panel) return
  const content = panel.parentElement
  panel.remove()
  panel = null
  if (content) for (const c of content.children) {
    if (c.dataset.bngeHidden) { c.style.display = c.dataset.bngeHidden === "-" ? "" : c.dataset.bngeHidden; delete c.dataset.bngeHidden }
  }
  const ours = document.querySelector(".bnge-earth-tab")
  if (ours) ours.classList.remove("active")
}

// BeamMP's sidebar on screen without our button: put it in (before the gap above BeamMP's own links)
function inject() {
  const sidebar = document.querySelector(".beammp-route .sidebar, main.main-grid > aside.sidebar")
  if (!sidebar || sidebar.querySelector(".bnge-earth-tab") || !sidebar.querySelector('img[src*="BeamMP"]')) return
  const content = sidebar.parentElement && sidebar.parentElement.querySelector("section.content")
  const spacer = sidebar.querySelector(".spacer")
  if (!content || !spacer) return
  const button = el("button", { class: "nav-btn bnge-earth-tab" }, "BeamNG.Earth")
  // BeamMP's styles are scoped to its own elements (Vue's data-v-* marks): the same marks give ours its look
  const sample = sidebar.querySelector(".nav-btn")
  if (sample) for (const a of sample.attributes) if (a.name.startsWith("data-v-")) button.setAttribute(a.name, "")
  button.addEventListener("click", () => {
    // BeamMP's own gates first (its launcher, terms of service and login pages): the list opens over its browser only
    const name = String(window.vueRouter?.currentRoute?.value?.name || "")
    if (!BROWSER_ROUTES.has(name)) return
    openPanel(content, button)
  })
  sidebar.insertBefore(button, spacer)
  // any of BeamMP's own tabs: its view back
  sidebar.addEventListener("click", e => {
    const b = e.target.closest(".nav-btn")
    if (b && b !== button) closePanel()
  }, true)
}

export function start() {
  if (observer || typeof MutationObserver === "undefined") return
  observer = new MutationObserver(() => {
    inject()
    if (panel && !document.body.contains(panel)) { panel = null; clearTimeout(pollTimer) }   // BeamMP's page left
  })
  observer.observe(document.body, { childList: true, subtree: true })
  inject()
}

export function stop() {
  if (observer) observer.disconnect()
  observer = null
  closePanel()
  for (const b of document.querySelectorAll(".bnge-earth-tab")) b.remove()
}
