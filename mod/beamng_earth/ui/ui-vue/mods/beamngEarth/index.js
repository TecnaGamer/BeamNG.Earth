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
async function registerPauseTab() {
  try {
    const store = await import("./earthSettingsStore.js")
    await lua.extensions.ui_pause_actions.registerModTab({
      id: TAB_ID,
      label: "BeamNG.Earth",
      icon: "globe",
      card2ComponentName: `${MOD_ROOT}/EarthSettingsSide.vue`,
    })
    for (const [i, page] of store.settingsSchema.entries()) {
      await lua.extensions.ui_pause_actions.registerModButton({
        id: store.pageId(page.name, i),
        tabId: TAB_ID,
        label: page.name,
        icon: store.PAGE_ICONS[page.name] || "cogOutline",
        componentName: `${MOD_ROOT}/EarthSettingsPage.vue`,
        props: { page: page.name },
      })
    }
  } catch (e) {
    console.error("BeamNG.Earth: pause menu tab failed", e)
  }
}

async function registerMapRoute() {
  try {
    if (!window.bngRoutes || !lua.extensions.ui_router_routeManager) return
    // imported here, not at the top: a map that fails to compile must not cost the settings button
    const routes = mapRoutes((await import("./EarthMap.vue")).default, (await import("./EarthWelcome.vue")).default,
                             (await import("./EarthSettings.vue")).default, (await import("./EarthMap3d.vue")).default)
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
  // the old shared-Mods-tab button, if this UI session still has one (before the tab: its id is the first page's)
  try { await lua.extensions.ui_pause_actions.unregisterModButton("beamngEarth-settings") } catch (e) { /* none */ }
  await registerPauseTab()
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
  await lua.extensions.ui_pause_actions.unregisterModTab(TAB_ID)       // (its buttons with it)
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
