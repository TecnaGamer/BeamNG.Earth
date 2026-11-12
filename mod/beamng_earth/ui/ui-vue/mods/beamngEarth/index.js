import { lua } from "@/bridge"

// BeamNG.Earth: a tab of its own in the Pause Menu (the user, 2026-10-28: "its own tab in the pause menu like where
// BeamMP puts it ... and have each page listed in the menu on the side"): ui_pause_actions.registerModTab, then one
// side button per settings page (EarthSettingsPage.vue with that page; EarthSettingsSide.vue as the tab's second card).
// The same pages as a full page of their own (EarthSettings.vue, a route: the main menu). Lua side:
// lua/ge/extensions/beamngEarth/settings.lua.
//
// The world map (EarthMap.vue): a full-screen route of its own ("beamngEarthMap", opened by the
// map key (M, the pause menu Map, or a bound key) through beamngEarth_settings.toggleMap). No pause-menu button of its own
// (the user, 2026-10-16: "move the map out of the mods tab"; the stock Map entry opens it). The route follows ui/ui-vue/mods/README.md "Standalone routes"; any failure
// there is logged and leaves the settings tab alone.
const TAB_ID = "beamngEarth"
const MOD_ROOT = "/ui/ui-vue/mods/beamngEarth"
const MAP_SOURCE = "beamngEarth.routes"
// The mod loader may overlap async load/unload calls, including calls from different evaluated copies. Finish each
// operation before starting the next, and let only the latest loaded copy unregister our shared UI sources.
const LIFECYCLE = Symbol.for("beamngEarth.uiLifecycle")
const LIFECYCLE_TIMEOUT_MS = 30_000
const owner = {}
let serversTab = null
function enqueue(label, operation) {
  const lifecycle = window[LIFECYCLE] ||= { queue: Promise.resolve(), owner: null }
  const request = { owner }
  if (label === "load") lifecycle.desired = request
  else if (lifecycle.desired?.owner === owner) lifecycle.desired = null
  const result = lifecycle.queue.then(async () => {
    let expire
    const expired = new Promise((_, reject) => { expire = reject })
    const run = { active: true, expired, timedOut: false, retried: false,
      error: new Error(`BeamNG.Earth: UI ${label} timed out after ${LIFECYCLE_TIMEOUT_MS} ms`) }
    run.onReply = () => {
      // A backgrounded game can deliver a valid callback after the deadline. Retry the still-desired load
      // from the beginning; never resume the expired continuation or revive an unloaded/replaced owner.
      if (!run.timedOut || run.retried || lifecycle.desired !== request) return
      run.retried = true
      enqueue(label, operation).catch(e => console.error("BeamNG.Earth: UI recovery failed", e))
    }
    const timer = setTimeout(() => { run.active = false; run.timedOut = true; expire(run.error) }, LIFECYCLE_TIMEOUT_MS)
    try {
      return await Promise.race([operation(lifecycle, run), expired])
    } finally {
      run.active = false
      clearTimeout(timer)
    }
  })
  lifecycle.queue = result.catch(() => {}) // rejected or missing replies must not block later loads/cleanup
  return result
}
function check(run) {
  if (!run.active) throw run.error
}
async function step(run, action) {
  check(run)
  const pending = Promise.resolve(action())
  pending.then(run.onReply, run.onReply)
  const result = await Promise.race([pending, run.expired])
  // Timing out does not cancel a Lua command or import. Its late result must not resume stale UI work.
  check(run)
  return result
}
function mapRoutes(EarthMap, EarthWelcome, EarthSettings, EarthMap3d) {
  return [
  {
    // the full settings page (the user, 2026-10-28: "a whole separate page like how BeamMP's settings page is"):
    // the main menu's way in (beamngEarth_settings.openSettings); in a level the pause menu's tab has the same pages
    path: "/beamng-earth-settings",
    name: "beamngEarthSettings",
    component: EarthSettings,
    meta: {
      luaRoute: {
        title: "BeamNG.Earth settings",
        backTarget: "menu",
      },
    },
  },
  {
    // the one-time welcome (settings.lua shows it once per WELCOME_VERSION in our level)
    path: "/beamng-earth-welcome",
    name: "beamngEarthWelcome",
    component: EarthWelcome,
    meta: {
      luaRoute: {
        title: "Welcome to BeamNG.Earth",
        backTarget: "play",
      },
    },
  },
  {
    path: "/beamng-earth-map",
    name: "beamngEarthMap",
    component: EarthMap,
    meta: {
      luaRoute: {
        title: "BeamNG.Earth map",
        backTarget: "play",
      },
    },
  },
  {
    // the 3D map screen (map3d.lua: transparent over the game, the orbit camera behind it)
    path: "/beamng-earth-map3d",
    name: "beamngEarthMap3d",
    component: EarthMap3d,
    meta: {
      luaRoute: {
        title: "BeamNG.Earth 3D map",
        backTarget: "play",
      },
    },
  },
  ]
}

function toLuaRoutes(records) {
  return records.map(record => ({ name: record.name, path: record.path, meta: record.meta }))
}

// The pause menu's BeamNG.Earth tab: the tab first (its buttons need it), then a side button per settings page
async function registerPauseTab(run) {
  try {
    const store = await step(run, () => import("./earthSettingsStore.js"))
    await step(run, () => lua.extensions.ui_pause_actions.registerModTab({
      id: TAB_ID,
      label: "BeamNG.Earth",
      icon: "globe",
      card2ComponentName: `${MOD_ROOT}/EarthSettingsSide.vue`,
    }))
    for (const [i, page] of store.settingsSchema.entries()) {
      await step(run, () => lua.extensions.ui_pause_actions.registerModButton({
        id: store.pageId(page.name, i),
        tabId: TAB_ID,
        label: page.name,
        icon: store.PAGE_ICONS[page.name] || "cogOutline",
        componentName: `${MOD_ROOT}/EarthSettingsPage.vue`,
        props: { page: page.name },
      }))
    }
  } catch (e) {
    if (!run.active) throw e
    console.error("BeamNG.Earth: pause menu tab failed", e)
  }
}

async function registerMapRoute(run) {
  try {
    check(run)
    if (!window.bngRoutes || !lua.extensions.ui_router_routeManager) return
    // imported here, not at the top: a map that fails to compile must not cost the settings button
    const routes = mapRoutes((await step(run, () => import("./EarthMap.vue"))).default,
                             (await step(run, () => import("./EarthWelcome.vue"))).default,
                             (await step(run, () => import("./EarthSettings.vue"))).default,
                             (await step(run, () => import("./EarthMap3d.vue"))).default)
    window.bngRoutes.add([{ path: MAP_SOURCE, routes }])
    const result = await step(run, () => lua.extensions.ui_router_routeManager.registerModRoutes(MAP_SOURCE, toLuaRoutes(routes)))
    if (!result?.success) {
      window.bngRoutes.remove([MAP_SOURCE])
      console.error("BeamNG.Earth: map route not registered", result?.errors)
    }
  } catch (e) {
    if (!run.active) throw e
    console.error("BeamNG.Earth: map route failed", e)
  }
}

async function loadUi(run) {
  // the old shared-Mods-tab button, if this UI session still has one (before the tab: its id is the first page's)
  try { await step(run, () => lua.extensions.ui_pause_actions.unregisterModButton("beamngEarth-settings")) }
  catch (e) { if (!run.active) throw e /* otherwise no legacy button */ }
  await registerPauseTab(run)
  check(run)
  // tell the mod the page exists: its imgui window becomes a compact HUD (settings.lua)
  if (window.bngApi && window.bngApi.engineLua) {
    window.bngApi.engineLua("if beamngEarth_settings then beamngEarth_settings.setPageExists(true) end")
  }
  // an older version's map button, if this UI session still has one
  try { await step(run, () => lua.extensions.ui_pause_actions.unregisterModButton("beamngEarth-map")) }
  catch (e) { if (!run.active) throw e /* otherwise no legacy button */ }
  await registerMapRoute(run)
  // the BeamNG.Earth servers tab in BeamMP's server browser (earthServers.js); its failure costs nothing else
  try {
    serversTab = await step(run, () => import("./earthServers.js"))
    serversTab.start()
  } catch (e) {
    if (!run.active) throw e
    console.error("BeamNG.Earth: servers tab failed", e)
  }
}

async function unloadUi(run) {
  check(run)
  try { serversTab?.stop() } catch (e) { /* never started */ }
  serversTab = null
  // Release local state first, and send both independent Lua removals without waiting on either callback.
  // A missing pause-tab reply must not prevent route cleanup (or a later UI load).
  try {
    if (window.bngRoutes) window.bngRoutes.remove([MAP_SOURCE])
  } catch (e) { console.error("BeamNG.Earth: map unload failed", e) }
  if (window.bngApi && window.bngApi.engineLua) {
    window.bngApi.engineLua("if beamngEarth_settings then beamngEarth_settings.setPageExists(false) end")
  }
  const removals = [step(run, () => lua.extensions.ui_pause_actions.unregisterModTab(TAB_ID)).catch(e => {
    if (!run.active) throw e
    console.error("BeamNG.Earth: pause menu unload failed", e)
  })]
  if (lua.extensions.ui_router_routeManager) {
    removals.push(step(run, () => lua.extensions.ui_router_routeManager.unregisterModRoutes(MAP_SOURCE, { fallbackRoute: "play" })).catch(e => {
      if (!run.active) throw e
      console.error("BeamNG.Earth: map unload failed", e)
    }))
  }
  await Promise.all(removals)
}

export function onLoad() {
  return enqueue("load", async (lifecycle, run) => {
    // Retire the old browser before any awaited setup: a replacement that times out must not leave it alive.
    const previousBrowser = window[Symbol.for("beamngEarth.servers")]
    if (typeof previousBrowser === "function") previousBrowser()
    lifecycle.owner = owner
    await loadUi(run)
  })
}

export function onUnload() {
  return enqueue("unload", async (lifecycle, run) => {
    if (lifecycle.owner !== owner) return
    lifecycle.owner = null
    await unloadUi(run)
  })
}
