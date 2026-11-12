// Real Vue scheduling and DOM observers; only the game's bridge/router and clock are stubbed.
// npm ci --prefix mod/tests && npm test --prefix mod/tests
import assert from "node:assert/strict"
import { readFile } from "node:fs/promises"
import path from "node:path"
import { fileURLToPath } from "node:url"
import vm from "node:vm"
import test from "node:test"
import { JSDOM } from "jsdom"
import { nextTick, ref, toRaw, watch } from "vue"

const root = process.env.BNE_UI_SOURCE_ROOT || fileURLToPath(new URL("../../", import.meta.url))
const sourceDir = path.join(root, "mod/beamng_earth/ui/ui-vue/mods/beamngEarth")
const beammpPath = "/ui/ui-vue/mods/BeamMP/shared/beammpState.js"
const deferred = () => {
  let resolve
  const promise = new Promise(r => { resolve = r })
  return { promise, resolve }
}
const publicList = [{ name: "Public server", ip: "192.0.2.1", port: 30814 }]
const earthList = name => ({ state: "ready", servers: [{ name, ip: "192.0.2.2", port: 30814, official: true,
  players: 2, maxPlayers: 8, desc: "Description", tags: "region:EU", pass: true, modlist: "earth.zip" }] })
const markup = `<main class="beammp-route main-grid"><aside class="sidebar">
  <img src="BeamMP/logo.png"><button class="nav-btn category-favorite" data-v-test>Favorites</button>
  <div class="spacer"></div></aside><section class="content"><div class="servers-wrap">
  <table><tbody><tr class="server-row"><td class="title-cell"><span class="server-title-content">Earth</span>
  </td></tr></tbody></table></div></section></main>`

async function harness(t, { dom = true, view = "all" } = {}) {
  const browser = new JSDOM(dom ? markup : "", { url: "http://localhost/" })
  const { window } = browser
  const timers = new Map(), replies = [], hooks = new Set(), loaded = new Map(), errors = []
  const tabs = new Set(), buttons = new Set(), routes = new Map(), events = []
  const state = { view: ref(view), servers: ref(publicList.slice()) }
  const h = { window, document: window.document, timers, replies, hooks, state, errors, tabs, buttons, routes, events,
    watchers: 0, observers: 0, imports: 0, refreshes: 0, navigations: 0, pageExists: false }
  let timerId = 0, now = 0
  window.vueRouter = {
    currentRoute: ref({ name: "menu.beammp.servers" }),
    afterEach(fn) { hooks.add(fn); return () => hooks.delete(fn) },
    async replace(route) { h.navigations++; h.route(route.name) },
  }
  window.bngRoutes = {
    add(groups) { for (const g of groups) routes.set(g.path, g.routes) },
    remove(sources) { for (const s of sources) routes.delete(s) },
  }
  window.bngApi = { engineLua(code, callback) {
    if (callback) replies.push(callback)
    else if (code.includes("serverlist.refresh")) h.refreshes++
    else if (code.includes("setPageExists")) {
      h.pageExists = code.includes("(true)"); events.push(`page-exists:${h.pageExists}`)
    }
  } }
  const lua = { extensions: {
    ui_pause_actions: {
      async registerModTab(tab) {
        events.push("register-tab-start")
        tabs.add(tab.id); events.push("register-tab-end")
        // Lua applies these synchronous changes before its callback reaches JavaScript.
        if (h.pauseGate) await h.pauseGate.promise
      },
      async registerModButton(button) { buttons.add(button.id); events.push(`register-button:${button.id}`) },
      async unregisterModButton(id) {
        buttons.delete(id)
        if (h.unregisterButtonGate) await h.unregisterButtonGate.promise
      },
      async unregisterModTab(id) {
        events.push("unregister-tab")
        if (h.failUnregister) throw Error("bridge unavailable")
        tabs.delete(id); buttons.clear()
        if (h.unregisterGate) await h.unregisterGate.promise
      },
    },
    ui_router_routeManager: {
      async registerModRoutes() {
        events.push("register-routes")
        return h.mapGate ? await h.mapGate.promise : { success: true }
      },
      async unregisterModRoutes() {
        events.push("unregister-routes")
        if (h.unregisterRoutesGate) await h.unregisterRoutesGate.promise
      },
    },
  } }
  class Observer extends window.MutationObserver {
    observe(...args) { if (!this.observing) h.observers++; this.observing = true; return super.observe(...args) }
    disconnect() { if (this.observing) h.observers--; this.observing = false; return super.disconnect() }
  }
  const context = vm.createContext({ window, document: window.document, MutationObserver: Observer,
    console: { error: (...args) => errors.push(args), warn: () => {} },
    setTimeout(fn, delay = 0) { timers.set(++timerId, { fn, due: now + delay }); return timerId },
    clearTimeout(id) { timers.delete(id) } })
  async function synthetic(id, values) {
    const m = new vm.SyntheticModule(Object.keys(values), function() {
      for (const [key, value] of Object.entries(values)) this.setExport(key, value)
    }, { context, identifier: id })
    await m.link(() => {}); await m.evaluate()
    return m
  }
  const vue = await synthetic("vue", { toRaw, watch(...args) {
    const stop = watch(...args); h.watchers++
    let stopped = false
    return () => { if (!stopped) { stopped = true; h.watchers--; stop() } }
  } })
  const bridge = await synthetic("bridge", { lua, useBridge: () => ({ events: {} }) })
  const beammp = await synthetic("beammp", { useBeamMPState: () => ({ state, setView: view => { state.view.value = view } }) })
  const store = await synthetic("settings", { settingsSchema: [{ name: "General" }, { name: "World" }],
    pageId: (_, i) => `page-${i}`, PAGE_ICONS: {} })
  const component = await synthetic("component", { default: {} })
  async function dependency(specifier, copy) {
    if (specifier === "vue") return vue
    if (specifier === "@/bridge") return bridge
    if (specifier === beammpPath) {
      h.imports++
      if (h.importGate) await h.importGate.promise
      return beammp
    }
    if (specifier === "./earthSettingsStore.js") return store
    if (specifier.endsWith(".vue")) {
      if (h.componentGate) await h.componentGate.promise
      return component
    }
    if (specifier === "./earthServers.js") return module("earthServers.js", `${copy}/graph-${h.graphRevision || 0}`)
    throw Error(`Unexpected game dependency: ${specifier}`)
  }
  async function module(file, copy) {
    const key = `${copy}/${file}`
    if (loaded.has(key)) return loaded.get(key)
    const m = new vm.SourceTextModule(await readFile(path.join(sourceDir, file), "utf8"), {
      context, identifier: key, importModuleDynamically: specifier => dependency(specifier, copy),
    })
    loaded.set(key, m)
    await m.link(specifier => dependency(specifier, copy)); await m.evaluate()
    return m
  }
  h.load = async (file = "earthServers.js", copy = "a") => (await module(file, copy)).namespace
  h.settle = async () => { for (let i = 0; i < 24; i++) { await Promise.resolve(); await nextTick() } }
  h.route = name => { window.vueRouter.currentRoute.value = { name }; for (const fn of hooks) fn() }
  h.runTimer = () => {
    const entry = [...timers].sort((a, b) => a[1].due - b[1].due)[0]
    assert.ok(entry, "a poll is scheduled")
    now = entry[1].due; timers.delete(entry[0]); return entry[1].fn()
  }
  h.advance = async ms => {
    const target = now + ms
    while ([...timers.values()].some(timer => timer.due <= target)) {
      h.runTimer(); await h.settle()
    }
    now = target
  }
  h.respond = async (data, index = 0) => {
    assert.ok(replies[index], "a Lua request is waiting")
    replies.splice(index, 1)[0](data); await h.settle()
  }
  h.poll = async data => { h.runTimer(); await h.respond(data) }
  h.styles = () => window.document.head.querySelectorAll("style").length
  h.assertStopped = () => {
    assert.equal(h.watchers, 0); assert.equal(h.observers, 0); assert.equal(hooks.size, 0)
    assert.equal(timers.size, 0); assert.equal(h.styles(), 0)
    assert.equal(window.document.querySelectorAll(".bnge-earth-tab").length, 0)
  }
  t.after(() => {
    for (const [key, m] of loaded) if (key.endsWith("earthServers.js")) m.namespace.stop()
    browser.window.close()
  })
  return h
}

test("starting twice is idempotent; a BeamMP page added later is hooked once", async t => {
  const h = await harness(t, { dom: false }), tab = await h.load()
  tab.start(); tab.start(); await h.settle()
  assert.equal(h.imports, 0)
  h.document.body.innerHTML = markup; await h.settle()
  assert.equal(h.watchers, 2); assert.equal(h.observers, 1); assert.equal(h.styles(), 1)
  assert.equal(h.document.querySelectorAll(".bnge-earth-tab").length, 1)
  tab.stop(); tab.stop(); h.assertStopped()
})

test("two cache-busted copies in Earth view cannot start a reactive list feedback loop", async t => {
  const h = await harness(t, { view: "earth" })
  const a = await h.load(), b = await h.load("earthServers.js", "b")
  a.start(); await h.settle(); await h.poll(earthList("Old Earth"))
  b.start(); h.document.body.append(h.document.createElement("div")); await h.settle()
  await h.poll(earthList("Earth"))
  assert.equal(h.watchers, 2); assert.equal(h.observers, 1); assert.equal(h.hooks.size, 1)
  assert.equal(h.state.servers.value[0].name, "Earth")
  a.stop(); await h.settle()
  assert.equal(h.watchers, 2); assert.equal(h.styles(), 1)
  b.stop(); await h.settle(); h.assertStopped()
  assert.deepEqual(toRaw(h.state.servers.value), publicList)
})

test("Earth rows preserve BeamMP metadata and restore the latest public list on leaving", async t => {
  const h = await harness(t), tab = await h.load()
  tab.start(); await h.settle(); h.state.view.value = "earth"; await h.settle()
  await h.poll(earthList("Earth"))
  h.document.querySelector(".server-title-content").textContent = "Earth"; await h.settle()
  const row = h.state.servers.value[0]
  assert.equal(row.pass, true); assert.equal(row.sdesc, "Description"); assert.equal(row.players, 2)
  assert.equal(row.tagsList[0].text, "EU"); assert.equal(row.modlist, "earth.zip")
  assert.equal(h.document.querySelector(".server-row").classList.contains("bnge-official"), true)
  const latest = [{ name: "New public list" }]
  h.state.servers.value = latest; await h.settle()
  assert.equal(h.state.servers.value[0].name, "Earth")
  h.state.view.value = "favorites"; await h.settle()
  assert.equal(toRaw(h.state.servers.value), latest)
})

test("leaving the browser for a download route restores BeamMP without navigating", async t => {
  const h = await harness(t, { view: "earth" }), tab = await h.load()
  tab.start(); await h.settle(); h.runTimer()
  h.route("menu.beammp.downloading"); await h.respond(earthList("Late Earth"))
  assert.deepEqual(toRaw(h.state.servers.value), publicList); assert.equal(h.navigations, 0)
})

test("a stopped tab cannot attach watchers when its BeamMP import finishes", async t => {
  const h = await harness(t), tab = await h.load()
  h.importGate = deferred(); tab.start(); await h.settle(); assert.equal(h.imports, 1)
  tab.stop(); h.importGate.resolve(); await h.settle(); h.assertStopped()
})

test("stop/start invalidates an import from the previous run of the same module", async t => {
  const h = await harness(t), tab = await h.load()
  h.importGate = deferred(); tab.start(); await h.settle(); tab.stop(); tab.start(); await h.settle()
  h.importGate.resolve(); await h.settle()
  assert.equal(h.watchers, 2); assert.equal(h.hooks.size, 1)
})

test("a button click awaiting the BeamMP import cannot navigate after unload", async t => {
  const h = await harness(t), tab = await h.load()
  h.importGate = deferred(); tab.start(); await h.settle()
  h.document.querySelector(".bnge-earth-tab").click()
  tab.stop(); h.importGate.resolve(); await h.settle()
  assert.equal(h.navigations, 0); assert.equal(h.state.view.value, "all"); h.assertStopped()
})

for (const state of ["loading", "ready"]) test(`a ${state} Lua reply after stop cannot restart polling`, async t => {
  const h = await harness(t, { view: "earth" }), tab = await h.load()
  tab.start(); await h.settle(); h.runTimer(); tab.stop()
  await h.respond({ ...earthList("Stale Earth"), state }); h.assertStopped()
  assert.deepEqual(toRaw(h.state.servers.value), publicList)
})

test("a late reply from the previous owner cannot overwrite the new owner's list", async t => {
  const h = await harness(t, { view: "earth" }), a = await h.load(), b = await h.load("earthServers.js", "b")
  a.start(); await h.settle(); h.runTimer()
  b.start(); await h.settle(); h.runTimer()
  await h.respond(earthList("Earth"), 1); await h.respond({ ...earthList("Stale"), state: "loading" })
  assert.equal(h.state.servers.value[0].name, "Earth"); assert.equal(h.timers.size, 0)
})

test("an older poll response cannot replace a newer refresh response", async t => {
  const h = await harness(t, { view: "earth" }), tab = await h.load()
  tab.start(); await h.settle(); h.runTimer()
  h.state.servers.value = [{ name: "Public refresh" }]; await h.settle(); h.runTimer()
  await h.respond(earthList("Newest"), 1); await h.respond(earthList("Older"))
  assert.equal(h.state.servers.value[0].name, "Newest")
})

test("Lua's empty table is an empty server list, with one loading poll at a time", async t => {
  const h = await harness(t, { view: "earth" }), tab = await h.load()
  tab.start(); await h.settle(); await h.poll({ state: "loading", servers: {} })
  assert.equal(h.timers.size, 1); assert.equal(h.state.servers.value.length, 0)
  await h.poll({ state: "ready", servers: {} }); assert.equal(h.timers.size, 0); assert.deepEqual(h.errors, [])
})

test("the Earth button respects BeamMP login and launcher routes", async t => {
  const h = await harness(t), tab = await h.load()
  tab.start(); await h.settle(); h.route("menu.beammp.login")
  h.document.querySelector(".bnge-earth-tab").click(); await h.settle()
  assert.equal(h.state.view.value, "all"); assert.equal(h.navigations, 0)
  h.route("menu.beammp.direct"); h.document.querySelector(".bnge-earth-tab").click(); await h.settle()
  assert.equal(h.state.view.value, "earth"); assert.equal(h.navigations, 1)
})

test("50 UI reloads leave one observer, one watcher pair and one router hook", async t => {
  const h = await harness(t, { view: "earth" })
  for (let i = 0; i < 50; i++) {
    const tab = await h.load("earthServers.js", String(i)); tab.start(); await h.settle()
    assert.equal(h.watchers, 2); assert.equal(h.observers, 1); assert.equal(h.styles(), 1)
  }
})

test("unload during an awaited UI load finishes with no registered UI", async t => {
  const h = await harness(t), ui = await h.load("index.js")
  h.pauseGate = deferred()
  const loading = ui.onLoad(); await h.settle()
  const unloading = ui.onUnload(); await h.settle()
  h.pauseGate.resolve(); await Promise.all([loading, unloading]); await h.settle()
  assert.equal(h.tabs.size, 0); assert.equal(h.routes.size, 0); assert.equal(h.pageExists, false); h.assertStopped()
  assert.ok(h.events.indexOf("register-tab-end") < h.events.indexOf("unregister-tab"))
})

test("an old module's late unload cannot unregister a newer UI owner", async t => {
  const h = await harness(t), a = await h.load("index.js"), b = await h.load("index.js", "b")
  await a.onLoad(); await b.onLoad(); await a.onUnload(); await h.settle()
  assert.equal(h.tabs.size, 1); assert.equal(h.routes.size, 1); assert.equal(h.pageExists, true)
  assert.equal(h.watchers, 2); assert.equal(h.styles(), 1)
  await b.onUnload(); await h.settle(); h.assertStopped()
})

test("overlapping module loads and a stale unload retain the final registration", async t => {
  const h = await harness(t), a = await h.load("index.js"), b = await h.load("index.js", "b")
  h.pauseGate = deferred()
  const first = a.onLoad(); await h.settle()
  const second = b.onLoad(), stale = a.onUnload(); await h.settle()
  h.pauseGate.resolve(); await Promise.all([first, second, stale]); await h.settle()
  assert.equal(h.tabs.size, 1); assert.equal(h.routes.size, 1); assert.equal(h.pageExists, true)
  assert.equal(h.watchers, 2); assert.equal(h.styles(), 1)
  await b.onUnload(); await h.settle(); h.assertStopped()
})

test("graph invalidation before onUnload still stops the originally loaded browser module", async t => {
  const h = await harness(t), ui = await h.load("index.js")
  await ui.onLoad(); await h.settle(); assert.equal(h.watchers, 2)
  // The game's refreshVueModEntries resets the mod graph before runModEntryPoints calls the old onUnload.
  h.graphRevision = 1
  await ui.onUnload(); await h.settle(); h.assertStopped()
})

test("repeated load/unload calls on the same entry point are idempotent", async t => {
  const h = await harness(t), ui = await h.load("index.js")
  await Promise.all([ui.onLoad(), ui.onLoad()]); await h.settle()
  assert.equal(h.watchers, 2); assert.equal(h.tabs.size, 1)
  await Promise.all([ui.onUnload(), ui.onUnload()]); await h.settle(); h.assertStopped()
})

test("a failed pause-tab unload still releases routes and allows the next UI load", async t => {
  const h = await harness(t), a = await h.load("index.js"), b = await h.load("index.js", "b")
  await a.onLoad(); h.failUnregister = true
  await a.onUnload(); await h.settle(); h.assertStopped()
  assert.equal(h.routes.size, 0); assert.equal(h.pageExists, false)
  h.failUnregister = false; await b.onLoad(); await h.settle()
  assert.equal(h.tabs.size, 1); assert.equal(h.routes.size, 1); assert.equal(h.watchers, 2)
  await b.onUnload(); await h.settle(); h.assertStopped()
})

const outcome = promise => {
  const result = { state: "pending" }
  promise.then(() => { result.state = "resolved" }, error => { result.state = "rejected"; result.error = error })
  return result
}
const assertCurrentUi = h => {
  assert.equal(h.tabs.size, 1); assert.equal(h.routes.size, 1); assert.equal(h.pageExists, true)
  assert.equal(h.watchers, 2); assert.equal(h.observers, 1); assert.equal(h.styles(), 1)
}

test("a hung initial bridge call releases the queue without restarting after its late reply", async t => {
  const h = await harness(t), a = await h.load("index.js"), b = await h.load("index.js", "b")
  const gate = h.unregisterButtonGate = deferred()
  const first = outcome(a.onLoad()); await h.settle()
  h.unregisterButtonGate = null
  const nextLoad = b.onLoad(), next = outcome(nextLoad); await h.settle(); await h.advance(45_000)
  assert.equal(first.state, "rejected"); assert.match(first.error.message, /timed out/i)
  await nextLoad; await h.settle()
  assert.equal(next.state, "resolved"); assertCurrentUi(h)
  const events = h.events.slice()
  gate.resolve(); await h.settle()
  assert.deepEqual(h.events, events); assertCurrentUi(h)
  await b.onUnload(); h.assertStopped()
})

test("a hung tab registration cannot resume an old load over the next UI owner", async t => {
  const h = await harness(t), a = await h.load("index.js"), b = await h.load("index.js", "b")
  const gate = h.pauseGate = deferred()
  const first = outcome(a.onLoad()); await h.settle()
  h.pauseGate = null
  const nextLoad = b.onLoad(), next = outcome(nextLoad); await h.settle(); await h.advance(45_000)
  assert.equal(first.state, "rejected"); await nextLoad; await h.settle()
  assert.equal(next.state, "resolved"); assertCurrentUi(h)
  const events = h.events.slice()
  gate.resolve(); await h.settle()
  assert.deepEqual(h.events, events); assertCurrentUi(h)
  await a.onUnload(); assertCurrentUi(h)
  await b.onUnload(); h.assertStopped()
})

test("a hung unload releases the queue and its late reply cannot remove the new UI", async t => {
  const h = await harness(t), a = await h.load("index.js"), b = await h.load("index.js", "b")
  await a.onLoad()
  const gate = h.unregisterGate = deferred()
  const unloading = outcome(a.onUnload()); await h.settle()
  h.unregisterGate = null
  const nextLoad = b.onLoad(), next = outcome(nextLoad); await h.settle(); await h.advance(45_000)
  assert.equal(unloading.state, "rejected"); await nextLoad; await h.settle()
  assert.equal(next.state, "resolved"); assertCurrentUi(h)
  const events = h.events.slice()
  gate.resolve(); await h.settle()
  assert.deepEqual(h.events, events); assertCurrentUi(h)
  await b.onUnload(); h.assertStopped()
})

test("a late failed route reply cannot remove routes registered by the next UI owner", async t => {
  const h = await harness(t), a = await h.load("index.js"), b = await h.load("index.js", "b")
  const gate = h.mapGate = deferred()
  const first = outcome(a.onLoad()); await h.settle()
  h.mapGate = null
  const nextLoad = b.onLoad(), next = outcome(nextLoad); await h.settle(); await h.advance(45_000)
  assert.equal(first.state, "rejected"); await nextLoad; await h.settle()
  assert.equal(next.state, "resolved"); assertCurrentUi(h)
  const events = h.events.slice()
  gate.resolve({ success: false }); await h.settle()
  assert.deepEqual(h.events, events); assertCurrentUi(h)
  await b.onUnload(); h.assertStopped()
})

test("a hanging component import cannot register or start an expired UI load", async t => {
  const h = await harness(t), a = await h.load("index.js"), b = await h.load("index.js", "b")
  const gate = h.componentGate = deferred()
  const first = outcome(a.onLoad()); await h.settle()
  h.componentGate = null
  const nextLoad = b.onLoad(), next = outcome(nextLoad); await h.settle(); await h.advance(45_000)
  assert.equal(first.state, "rejected"); await nextLoad; await h.settle()
  assert.equal(next.state, "resolved"); assertCurrentUi(h)
  const events = h.events.slice()
  gate.resolve(); await h.settle()
  assert.deepEqual(h.events, events); assertCurrentUi(h)
  await b.onUnload(); h.assertStopped()
})

test("a hung unload cleans local UI and attempts both Lua removals before timing out", async t => {
  const h = await harness(t), ui = await h.load("index.js")
  await ui.onLoad()
  h.unregisterGate = deferred(); h.unregisterRoutesGate = deferred()
  const unloading = outcome(ui.onUnload()); await h.settle()
  assert.equal(h.routes.size, 0); assert.equal(h.pageExists, false)
  assert.ok(h.events.includes("unregister-routes"))
  await h.advance(60_000)
  assert.equal(unloading.state, "rejected"); h.assertStopped()
  h.unregisterGate = null; h.unregisterRoutesGate = null
  await ui.onLoad(); assertCurrentUi(h)
  await ui.onUnload(); h.assertStopped()
})

test("a timed-out current load recovers with a fresh operation when its delayed reply arrives", async t => {
  const h = await harness(t), ui = await h.load("index.js")
  const gate = h.pauseGate = deferred()
  const loading = outcome(ui.onLoad()); await h.settle(); await h.advance(45_000)
  assert.equal(loading.state, "rejected"); assert.equal(h.styles(), 0)
  h.pauseGate = null; gate.resolve(); await h.settle()
  await h.window[Symbol.for("beamngEarth.uiLifecycle")].queue
  assertCurrentUi(h)
  // Two pages are registered once by the recovery, rather than by both the old and new continuations.
  assert.equal(h.events.filter(e => e.startsWith("register-button:")).length, 2)
  await ui.onUnload(); h.assertStopped()
})

test("a delayed reply cannot revive a timed-out load after an explicit unload", async t => {
  const h = await harness(t), ui = await h.load("index.js")
  const gate = h.pauseGate = deferred()
  const loading = outcome(ui.onLoad()); await h.settle(); await h.advance(45_000)
  assert.equal(loading.state, "rejected")
  await ui.onUnload(); h.assertStopped()
  const events = h.events.slice()
  h.pauseGate = null; gate.resolve(); await h.settle()
  await h.window[Symbol.for("beamngEarth.uiLifecycle")].queue
  assert.deepEqual(h.events, events); h.assertStopped()
})

test("a stale owner's unload cannot cancel recovery of the current desired load", async t => {
  const h = await harness(t), a = await h.load("index.js"), b = await h.load("index.js", "b")
  await a.onLoad()
  const gate = h.pauseGate = deferred()
  const loading = outcome(b.onLoad()); await h.settle()
  const stale = outcome(a.onUnload()); await h.settle(); await h.advance(45_000)
  assert.equal(loading.state, "rejected"); assert.equal(stale.state, "resolved")
  h.pauseGate = null; gate.resolve(); await h.settle()
  await h.window[Symbol.for("beamngEarth.uiLifecycle")].queue
  assertCurrentUi(h)
  await b.onUnload(); h.assertStopped()
})

test("a replacement load stops the old browser even if its own bridge reply never arrives", async t => {
  const h = await harness(t), a = await h.load("index.js"), b = await h.load("index.js", "b")
  await a.onLoad()
  h.pauseGate = deferred()
  const loading = outcome(b.onLoad()); await h.settle(); await h.advance(45_000)
  assert.equal(loading.state, "rejected")
  await b.onUnload(); h.assertStopped()
  await a.onUnload(); h.assertStopped()
})
