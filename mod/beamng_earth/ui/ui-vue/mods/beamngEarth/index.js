import { lua } from "@/bridge"

// BeamNG.Earth: a button in the shared "Mods" tab of the Pause Menu opening the settings page
// (same pattern as the taa_standalone mod). Lua side: lua/ge/extensions/beamngEarth/settings.lua.
//
// The world map (EarthMap.vue): a full-screen route of its own ("beamngEarthMap", opened by the
// map key (M, the pause menu Map, or a bound key) through beamngEarth_settings.toggleMap). No Mods-tab button of its own
// (the user, 2026-10-16: "move the map out of the mods tab"; the stock Map entry opens it). The route follows ui/ui-vue/mods/README.md "Standalone routes"; any failure
// there is logged and leaves the settings button alone.
const MAP_SOURCE = "beamngEarth.routes"
function mapRoutes(EarthMap, EarthWelcome) {
  return [
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
  ]
}

function toLuaRoutes(records) {
  return records.map(record => ({ name: record.name, path: record.path, meta: record.meta }))
}

async function registerMapRoute() {
  try {
    if (!window.bngRoutes || !lua.extensions.ui_router_routeManager) return
    // imported here, not at the top: a map that fails to compile must not cost the settings button
    const routes = mapRoutes((await import("./EarthMap.vue")).default, (await import("./EarthWelcome.vue")).default)
    window.bngRoutes.add([{ path: MAP_SOURCE, routes }])
    const result = await lua.extensions.ui_router_routeManager.registerModRoutes(MAP_SOURCE, toLuaRoutes(routes))
    if (!result?.success) {
      window.bngRoutes.remove([MAP_SOURCE])
      console.error("BeamNG.Earth: map route not registered", result?.errors)
    }
  } catch (e) {
    console.error("BeamNG.Earth: map route failed", e)
  }
}

export async function onLoad() {
  await lua.extensions.ui_pause_actions.registerModButton({
    id: "beamngEarth-settings",
    tabId: "mods",
    label: "BeamNG.Earth",
    icon: "globe",
    componentName: "/ui/ui-vue/mods/beamngEarth/EarthSettings.vue",
  })
  // tell the mod the page exists: its imgui window becomes a compact HUD (settings.lua)
  if (window.bngApi && window.bngApi.engineLua) {
    window.bngApi.engineLua("if beamngEarth_settings then beamngEarth_settings.setPageExists(true) end")
  }
  // an older version's map button, if this UI session still has one
  try { await lua.extensions.ui_pause_actions.unregisterModButton("beamngEarth-map") } catch (e) { /* none */ }
  await registerMapRoute()
  // the BeamNG.Earth servers tab in BeamMP's server browser (earthServers.js); its failure costs nothing else
  try { (await import("./earthServers.js")).start() } catch (e) { console.error("BeamNG.Earth: servers tab failed", e) }
}

export async function onUnload() {
  try { (await import("./earthServers.js")).stop() } catch (e) { /* never started */ }
  await lua.extensions.ui_pause_actions.unregisterModButton("beamngEarth-settings")
  try {
    if (window.bngRoutes) window.bngRoutes.remove([MAP_SOURCE])
    if (lua.extensions.ui_router_routeManager) {
      await lua.extensions.ui_router_routeManager.unregisterModRoutes(MAP_SOURCE, { fallbackRoute: "play" })
    }
  } catch (e) {
    console.error("BeamNG.Earth: map unload failed", e)
  }
  if (window.bngApi && window.bngApi.engineLua) {
    window.bngApi.engineLua("if beamngEarth_settings then beamngEarth_settings.setPageExists(false) end")
  }
}
