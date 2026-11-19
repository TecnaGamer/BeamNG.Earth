// node mod/tests/test_ui_lifecycle.mjs: the UI entry point's load/unload (ui-vue/mods/beamngEarth/index.js) when its
// files go away or a Lua reply never comes (#1302). The game's UI mod loader (ui-vue/src/services/uiMods/manager.js)
// awaits each mod's onUnload/onLoad, one mod after another, and runs again on every UiModsChanged (BeamMP mounting and
// unmounting mods during a join): a load stuck behind a missing reply held the next unload/load, and with them the whole
// UI, until the 30 s lifecycle timeout. A superseded operation now gives way at once, a missing file ends the load, and
// unloading imports nothing (its modules may be unmounted).
import assert from "node:assert/strict"
import { readFileSync } from "node:fs"
import { fileURLToPath } from "node:url"

const SRC = process.env.UI_INDEX_JS   // (another version of index.js to check, e.g. a release's)
  || fileURLToPath(new URL("../beamng_earth/ui/ui-vue/mods/beamngEarth/index.js", import.meta.url))
const only = process.env.ONLY ? new Set(process.env.ONLY.split(",").map(Number)) : null
const run = n => !only || only.has(n)
const TIMEOUT_MS = 1500        // (the lifecycle timeout, shortened from 30 s for the test)
const QUICK_MS = 300           // "at once": well under the timeout

// The entry point with its imports routed to the test: "@/bridge" -> a stub `lua`, import("./x") -> H.imp("./x")
const source = readFileSync(SRC, "utf8")
  .replace(/^import \{ lua \} from "@\/bridge"$/m, "const { lua } = globalThis.__H")
  .replace(/LIFECYCLE_TIMEOUT_MS = 30_000/, `LIFECYCLE_TIMEOUT_MS = ${TIMEOUT_MS}`)
  .replace(/\bimport\(/g, "globalThis.__H.imp(")
assert.ok(source.includes("globalThis.__H.imp(") && source.includes(`= ${TIMEOUT_MS}`), "index.js rewritten for the test")
let copies = 0
async function evaluatedCopy() {      // a fresh evaluation, as the game's loader does on each reload
  copies += 1
  return import("data:text/javascript," + encodeURIComponent(source + `\n// copy ${copies}`))
}

const never = () => new Promise(() => {})
const ok = v => Promise.resolve(v)
const missing = path => Promise.reject(new Error(`404 /ui/ui-vue/mods/beamngEarth/${path.slice(2)}`))

// One scenario's world: window, the Lua bridge (each call recorded; replies chosen per function), the module files
function world({ replies = {}, files = {} } = {}) {
  const calls = []
  const imports = []
  const servers = { started: 0, stopped: 0 }
  const paste = { installed: 0, removed: 0 }
  const modules = {
    "./earthSettingsStore.js": () => ok({ settingsSchema: [{ name: "General" }], pageId: (n, i) => `beamngEarth-${i}`,
                                          PAGE_ICONS: {}, pageLabel: page => page.name,
                                          startLocalization: () => () => {}, readyLocalization: () => ok(), watchLocale: () => () => {} }),
    "./paste.js": () => ok({ installPaste() { paste.installed += 1 }, removePaste() { paste.removed += 1 } }),
    "./EarthMap.vue": () => ok({ default: {} }),
    "./EarthWelcome.vue": () => ok({ default: {} }),
    "./EarthSettings.vue": () => ok({ default: {} }),
    "./EarthMap3d.vue": () => ok({ default: {} }),
    "./earthServers.js": () => ok({ start() { servers.started += 1 }, stop() { servers.stopped += 1 } }),
    ...files,
  }
  const ext = name => new Proxy({}, { get: (_, fn) => (...args) => {
    calls.push(`${name}.${String(fn)}`)
    const r = replies[`${name}.${String(fn)}`]
    return r ? r(...args) : ok({ success: true })
  } })
  globalThis.window = {
    bngRoutes: { added: 0, removed: 0, add() { this.added += 1 }, remove() { this.removed += 1 } },
    bngApi: { engineLua(cmd) { calls.push(`engineLua ${cmd}`) } },
  }
  globalThis.__H = {
    lua: { extensions: new Proxy({}, { get: (_, name) => ext(String(name)) }) },
    imp: path => { imports.push(path); return (modules[path] || (() => missing(path)))() },
  }
  return { calls, imports, servers, paste }
}

async function timed(promise) {
  const t0 = Date.now()
  let error = null
  try { await promise } catch (e) { error = e }
  return { ms: Date.now() - t0, error }
}

// 1. normal load and unload: everything registered, then removed; unloading imports nothing
if (run(1)) {
  const w = world()
  const ui = await evaluatedCopy()
  const load = await timed(ui.onLoad())
  assert.equal(load.error, null)
  assert.ok(w.calls.includes("ui_pause_actions.registerModTab"), "pause tab registered")
  assert.ok(w.calls.includes("ui_router_routeManager.registerModRoutes"), "map routes registered")
  assert.equal(w.servers.started, 1)
  assert.equal(w.paste.installed, 1)
  const before = w.imports.length
  const unload = await timed(ui.onUnload())
  assert.equal(unload.error, null)
  assert.ok(w.calls.includes("ui_pause_actions.unregisterModTab"), "pause tab removed")
  assert.ok(w.calls.includes("ui_router_routeManager.unregisterModRoutes"), "map routes removed")
  assert.equal(w.servers.stopped, 1, "servers tab stopped")
  assert.equal(w.paste.removed, 1, "paste removed")
  assert.equal(w.imports.length, before, "unloading imports nothing (its files may be unmounted)")
}

// 2. the loader's reload (UiModsChanged) while our load waits on a Lua reply that never comes: the unload of the same
// copy gives way at once instead of after the timeout, and the load ends without an error for the loader to log
if (run(2)) {
  const w = world({ replies: { "ui_pause_actions.registerModTab": never } })
  const ui = await evaluatedCopy()
  const loading = timed(ui.onLoad())
  await new Promise(r => setTimeout(r, 20))
  const unload = await timed(ui.onUnload())
  assert.ok(unload.ms < QUICK_MS, `unload behind a stuck load: ${unload.ms} ms (timeout ${TIMEOUT_MS} ms)`)
  assert.equal(unload.error, null)
  assert.ok(w.calls.includes("ui_pause_actions.unregisterModTab"), "the stuck load's registrations cleaned up")
  const load = await loading
  assert.ok(load.ms < QUICK_MS + 50, `the superseded load ended: ${load.ms} ms`)
  assert.equal(load.error, null, "a superseded load is not an error")
}

// 3. a stuck load of one evaluated copy and the next copy's load (the server's copy replacing the player's): the new
// load starts at once and finishes
if (run(3)) {
  const stuck = { "ui_pause_actions.registerModTab": never }
  const w = world({ replies: stuck })
  const a = await evaluatedCopy()
  const loadingA = timed(a.onLoad())
  await new Promise(r => setTimeout(r, 20))
  delete stuck["ui_pause_actions.registerModTab"]
  const b = await evaluatedCopy()
  const loadB = await timed(b.onLoad())
  assert.ok(loadB.ms < QUICK_MS, `the next copy's load behind a stuck one: ${loadB.ms} ms`)
  assert.equal(loadB.error, null)
  assert.ok(w.calls.filter(c => c === "ui_router_routeManager.registerModRoutes").length === 1,
    "only the new copy registered its routes")
  const loadA = await loadingA
  assert.equal(loadA.error, null)
  // the old copy's unload afterwards (the loader drops it): leaves the new copy's registrations alone
  const n = w.calls.length
  await a.onUnload()
  assert.equal(w.calls.length, n, "the replaced copy's unload removes nothing of the new copy's")
}

// 4. our files unmounted (BeamMP switched our copy off, the loader still lists it): the load ends at the first missing
// file, quickly and quietly, without registering pages whose files are gone; unloading needs no file
if (run(4)) {
  const w = world({ files: { "./earthSettingsStore.js": () => missing("./earthSettingsStore.js") } })
  const ui = await evaluatedCopy()
  const load = await timed(ui.onLoad())
  assert.ok(load.ms < QUICK_MS, `load with the files gone: ${load.ms} ms`)
  assert.equal(load.error, null, "files gone: no error for the loader to log")
  assert.ok(!w.calls.includes("ui_pause_actions.registerModTab"), "no pause tab whose pages are gone")
  assert.ok(!w.calls.includes("ui_router_routeManager.registerModRoutes"), "no routes whose pages are gone")
  assert.equal(w.servers.started, 0, "no servers tab")
  const before = w.imports.length
  const unload = await timed(ui.onUnload())
  assert.ok(unload.ms < QUICK_MS && unload.error === null, `unload: ${unload.ms} ms`)
  assert.equal(w.imports.length, before, "unloading imports nothing")
}

// 5. a Lua reply that never comes with nothing else queued: the timeout still ends it (the backstop)
if (run(5)) {
  world({ replies: { "ui_pause_actions.registerModTab": never } })
  const ui = await evaluatedCopy()
  const load = await timed(ui.onLoad())
  assert.ok(load.ms >= TIMEOUT_MS - 50 && load.ms < TIMEOUT_MS + 500, `timeout backstop: ${load.ms} ms`)
  assert.ok(load.error && /timed out/.test(load.error.message), "the timeout is reported")
}

// 6. our files unmounted part way through the load (Codex review): what the load had registered so far (pause tab and
// buttons, paste listeners, routes, the page-exists flag) is rolled back before onLoad resolves
const rolledBack = (w, what) => {
  assert.ok(w.calls.includes("ui_pause_actions.unregisterModTab"), `${what}: pause tab removed`)
  assert.ok(w.calls.includes("ui_router_routeManager.unregisterModRoutes"), `${what}: routes removed`)
  assert.ok(w.calls.some(c => c.includes("setPageExists(false)")), `${what}: page-exists flag cleared`)
  assert.ok(globalThis.window.bngRoutes.removed >= 1, `${what}: UI routes removed`)
}
if (run(6)) {
  // after the pause tab: paste.js gone
  let w = world({ files: { "./paste.js": () => missing("./paste.js") } })
  let ui = await evaluatedCopy()
  let load = await timed(ui.onLoad())
  assert.ok(load.ms < QUICK_MS && load.error === null, `paste.js gone: ${load.ms} ms, ${load.error}`)
  assert.ok(w.calls.includes("ui_pause_actions.registerModButton"), "(the tab and buttons were registered first)")
  rolledBack(w, "paste.js gone")
  // after the routes: earthServers.js gone
  w = world({ files: { "./earthServers.js": () => missing("./earthServers.js") } })
  ui = await evaluatedCopy()
  load = await timed(ui.onLoad())
  assert.ok(load.ms < QUICK_MS && load.error === null, `earthServers.js gone: ${load.ms} ms, ${load.error}`)
  assert.ok(w.calls.includes("ui_router_routeManager.registerModRoutes"), "(the routes were registered first)")
  rolledBack(w, "earthServers.js gone")
  assert.equal(w.paste.removed, 1, "paste listeners removed")
  // the loader's unload afterwards: nothing left to remove, nothing imported
  const n = w.calls.length, before = w.imports.length
  const unload = await timed(ui.onUnload())
  assert.ok(unload.error === null && w.calls.length === n && w.imports.length === before, "unload afterwards: nothing more")
  // a rollback whose Lua replies never come still ends (no 30 s wait)
  w = world({ files: { "./earthServers.js": () => missing("./earthServers.js") },
              replies: { "ui_pause_actions.unregisterModTab": never } })
  ui = await evaluatedCopy()
  load = await timed(ui.onLoad())
  assert.ok(load.error === null && load.ms < TIMEOUT_MS, `rollback with a missing reply: ${load.ms} ms`)
}

console.log("test_ui_lifecycle: ok")
