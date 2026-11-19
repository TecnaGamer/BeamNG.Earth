// BeamNG.Earth's settings: the schema, the presets and the state shared by the settings pages (the pause menu's
// BeamNG.Earth tab: EarthSettingsPage.vue per page, EarthSettingsSide.vue beside them; and the full page
// EarthSettings.vue, a route of its own for the main menu). One state for all of them (this module is loaded once):
// the setting pointed at in a page shows its help in the side card. Lua side: lua/ge/extensions/beamngEarth/settings.lua.
import { ref, computed } from "vue"
import { t, startLocalization, watchLocale, readyLocalization, number } from "./i18n.js"
export { startLocalization, watchLocale, readyLocalization, t as translateUI }
import { restartView } from "./earthRestart.js"

// The settings the world server reads only when it starts (server/srvrestart.py): the page offers "Restart world server"
// once one of them differs from the value the server started with (earthRestart.js)
const RESTART_NOTE = "settings.note.serverRestart"
// Mirrors lua/ge/extensions/beamngEarth/settings.lua SCHEMA (the Lua side validates and clamps). Sections down the
// left; Graphics first (the quality presets: settings.lua PRESETS).
export const settingsSchema = [
  {
    name: "Graphics", label: "settings.page.graphics",
    presets: true,
    intro: "settings.intro.graphics",
    items: [
      { id: "terrainDetail", type: "select", default: "normal", name: "settings.item.terrainDetail.name", desc: "settings.item.terrainDetail.desc",
        options: [ { label: "settings.option.low", value: "low" }, { label: "settings.option.normal", value: "normal" }, { label: "settings.option.high", value: "high" }, { label: "settings.option.ultra", value: "ultra" }, { label: "settings.option.extreme", value: "extreme" } ] },
      { id: "lodPx", type: "float", min: 0, max: 8, step: 0.5, default: 0, name: "settings.item.lodPx.name", desc: "settings.item.lodPx.desc" },
      { id: "groundPhoto", type: "select", default: "2048", name: "settings.item.groundPhoto.name", desc: "settings.item.groundPhoto.desc",
        note: "settings.item.groundPhoto.note",
        options: [ { label: "settings.option.1024light", value: "1024" }, { label: "settings.option.2048default", value: "2048" }, { label: "settings.option.4096sharpest", value: "4096" } ] },
      { id: "groundGrid", type: "select", default: "1", name: "settings.item.groundGrid.name", desc: "settings.item.groundGrid.desc",
        note: RESTART_NOTE,
        options: [ { label: "settings.option.1mdefault", value: "1" }, { label: "settings.option.2mlighter", value: "2" } ] },
      { id: "terrainShadows", type: "bool", default: true, name: "settings.item.terrainShadows.name", desc: "settings.item.terrainShadows.desc" },
      { id: "trees", type: "select", default: "normal", name: "settings.item.trees.name", desc: "settings.item.trees.desc",
        options: [ { label: "settings.option.lowest", value: "lowest" }, { label: "settings.option.low", value: "low" }, { label: "settings.option.medium", value: "medium" }, { label: "settings.option.normal", value: "normal" }, { label: "settings.option.high", value: "high" }, { label: "settings.option.ultra", value: "ultra" }, { label: "settings.option.extreme", value: "extreme" } ] },
      { id: "treeDistance", type: "select", default: "normal", name: "settings.item.treeDistance.name", desc: "settings.item.treeDistance.desc",
        options: [ { label: "settings.option.nearest", value: "nearest" }, { label: "settings.option.near", value: "near" }, { label: "settings.option.medium", value: "medium" }, { label: "settings.option.normal", value: "normal" }, { label: "settings.option.far", value: "far" }, { label: "settings.option.farther", value: "farther" }, { label: "settings.option.max", value: "max" } ] },
      { id: "buildingDistance", type: "select", default: "normal", name: "settings.item.buildingDistance.name", desc: "settings.item.buildingDistance.desc",
        options: [ { label: "settings.option.near", value: "near" }, { label: "settings.option.normal", value: "normal" }, { label: "settings.option.far", value: "far" }, { label: "settings.option.max", value: "max" } ] },
      { id: "objectDetail", type: "select", default: "game", name: "settings.item.objectDetail.name", desc: "settings.item.objectDetail.desc",
        options: [ { label: "settings.option.gamesetting", value: "game" }, { label: "settings.option.low", value: "low" }, { label: "settings.option.normal", value: "normal" }, { label: "settings.option.high", value: "high" }, { label: "settings.option.ultra", value: "ultra" }, { label: "settings.option.extreme", value: "extreme" } ] },
      { id: "grass", type: "select", virtual: true, default: "normal", name: "settings.item.grass.name", desc: "settings.item.grass.desc",
        options: [ { label: "settings.option.off", value: "off" }, { label: "settings.option.low", value: "low" }, { label: "settings.option.normal", value: "normal" }, { label: "settings.option.high", value: "high" } ] },
      { id: "grassDistance", type: "select", default: "normal", name: "settings.item.grassDistance.name", desc: "settings.item.grassDistance.desc", showIf: c => c.groundCover !== false,
        options: [ { label: "settings.option.near", value: "near" }, { label: "settings.option.normal", value: "normal" }, { label: "settings.option.far", value: "far" } ] },
      { id: "rocks", type: "bool", default: true, name: "settings.item.rocks.name", desc: "settings.item.rocks.desc" },
      { id: "nightLights", type: "select", default: "normal", name: "settings.item.nightLights.name", desc: "settings.item.nightLights.desc",
        options: [ { label: "settings.option.few", value: "few" }, { label: "settings.option.normal", value: "normal" }, { label: "settings.option.many", value: "many" } ] },
      { id: "roadDetail", type: "select", default: "full", name: "settings.item.roadDetail.name", desc: "settings.item.roadDetail.desc",
        options: [ { label: "settings.option.off", value: "off" }, { label: "settings.option.basic", value: "basic" }, { label: "settings.option.full", value: "full" } ] },
      { id: "distantTraffic", type: "bool", default: false, name: "settings.item.distantTraffic.name", desc: "settings.item.distantTraffic.desc" },
      { id: "distantTrafficWithoutTraffic", type: "bool", default: false, name: "settings.item.distantTrafficWithoutTraffic.name", desc: "settings.item.distantTrafficWithoutTraffic.desc", showIf: c => c.distantTraffic === true },
      { id: "distantTrafficQuality", type: "select", default: "high", name: "settings.item.distantTrafficQuality.name", desc: "settings.item.distantTrafficQuality.desc", showIf: c => c.distantTraffic === true,
        options: [ { label: "settings.option.low", value: "low" }, { label: "settings.option.medium", value: "medium" }, { label: "settings.option.high", value: "high" } ] },
      { id: "viewDistanceMode", type: "select", default: "horizon", name: "settings.item.viewDistanceMode.name", desc: "settings.item.viewDistanceMode.desc",
        options: [ { label: "settings.option.realhorizon", value: "horizon" }, { label: "settings.option.fixeddistance", value: "fixed" } ] },
      { id: "viewDistanceKm", type: "float", min: 1, max: 1000, step: 1, default: 100, name: "settings.item.viewDistanceKm.name", desc: "settings.item.viewDistanceKm.desc", showIf: c => c.viewDistanceMode === "fixed" },
    ],
  },
  {
    name: "World", label: "settings.page.world",
    items: [
      { id: "skyMode", type: "select", default: "real", name: "settings.item.skyMode.name", desc: "settings.item.skyMode.desc",
        note: "settings.item.skyMode.note",
        options: [ { label: "settings.option.realtime", value: "real" }, { label: "settings.option.manual", value: "manual" } ] },
      { id: "weatherMode", type: "select", default: "real", name: "settings.item.weatherMode.name", desc: "settings.item.weatherMode.desc",
        note: "settings.item.weatherMode.note",
        options: [ { label: "settings.option.realweather", value: "real" }, { label: "settings.option.manual", value: "manual" } ] },
      { id: "clouds", type: "bool", default: true, name: "settings.item.clouds.name", desc: "settings.item.clouds.desc" },
      { id: "realGravity", type: "bool", default: true, name: "settings.item.realGravity.name", desc: "settings.item.realGravity.desc" },
      { id: "lowFuelPrompt", type: "bool", default: true, name: "settings.item.lowFuelPrompt.name", desc: "settings.item.lowFuelPrompt.desc" },
      { id: "roads", type: "bool", default: true, name: "settings.item.roads.name", desc: "settings.item.roads.desc" },
      { id: "highwaySigns", type: "bool", default: true, name: "settings.item.highwaySigns.name", desc: "settings.item.highwaySigns.desc" },
      { id: "models3dmr", type: "bool", default: true, name: "settings.item.models3dmr.name", desc: "settings.item.models3dmr.desc",
        note: RESTART_NOTE },
      { id: "directionSigns", type: "bool", default: true, name: "settings.item.directionSigns.name", desc: "settings.item.directionSigns.desc" },
      { id: "germanJunctionSigns", type: "bool", default: false, name: "settings.item.germanJunctionSigns.name", desc: "settings.item.germanJunctionSigns.desc" },
      { id: "softDrag", type: "bool", default: false, name: "settings.item.softDrag.name", desc: "settings.item.softDrag.desc" },
      { id: "deckGuard", type: "bool", default: true, name: "settings.item.deckGuard.name", desc: "settings.item.deckGuard.desc" },
      { id: "rumbleStrips", type: "bool", default: true, name: "settings.item.rumbleStrips.name", desc: "settings.item.rumbleStrips.desc" },
      { id: "townSigns", type: "bool", default: true, name: "settings.item.townSigns.name", desc: "settings.item.townSigns.desc" },
      { id: "adaptiveTraffic", type: "bool", default: false, name: "settings.item.adaptiveTraffic.name", desc: "settings.item.adaptiveTraffic.desc" },
      { id: "autodriveObstacles", type: "bool", default: false, name: "settings.item.autodriveObstacles.name", desc: "settings.item.autodriveObstacles.desc" },
      { id: "autodriveOncomingPass", type: "bool", default: false, name: "settings.item.autodriveOncomingPass.name", desc: "settings.item.autodriveOncomingPass.desc" },
      { id: "rebaseDistance", type: "float", min: 2000, max: 16000, step: 100, default: 15000, name: "settings.item.rebaseDistance.name", desc: "settings.item.rebaseDistance.desc" },
    ],
  },
  {
    // the BNE_* build switches a player may turn on (server/experimental.py FEATURES: the launcher starts the server
    // with them; settings.lua SCHEMA); all off by default
    name: "Experimental", label: "settings.page.experimental",
    intro: "settings.intro.experimental",
    items: [
      { id: "roadSigns", type: "bool", default: false, name: "settings.item.roadSigns.name", desc: "settings.item.roadSigns.desc",
        note: RESTART_NOTE },
      { id: "expNlRoadSigns", type: "bool", default: false, name: "settings.item.expNlRoadSigns.name", desc: "settings.item.expNlRoadSigns.desc",
        note: RESTART_NOTE, showIf: c => c.roadSigns === true },
      { id: "expRoadRevamp", type: "bool", default: false, name: "settings.item.expRoadRevamp.name", desc: "settings.item.expRoadRevamp.desc",
        note: RESTART_NOTE },
      { id: "expEuScenery", type: "bool", default: false, name: "settings.item.expEuScenery.name", desc: "settings.item.expEuScenery.desc",
        note: RESTART_NOTE },
      { id: "expDropRails", type: "bool", default: false, name: "settings.item.expDropRails.name", desc: "settings.item.expDropRails.desc",
        note: RESTART_NOTE },
      { id: "expEntranceFlares", type: "bool", default: false, name: "settings.item.expEntranceFlares.name", desc: "settings.item.expEntranceFlares.desc",
        note: RESTART_NOTE },
      { id: "expPriorityStops", type: "bool", default: false, name: "settings.item.expPriorityStops.name", desc: "settings.item.expPriorityStops.desc",
        note: RESTART_NOTE },
      { id: "expRoadBumps", type: "bool", default: false, name: "settings.item.expRoadBumps.name", desc: "settings.item.expRoadBumps.desc",
        note: RESTART_NOTE },
      { id: "expRoughDirt", type: "bool", default: false, name: "settings.item.expRoughDirt.name", desc: "settings.item.expRoughDirt.desc",
        note: RESTART_NOTE },
      { id: "expWearStrips", type: "bool", default: false, name: "settings.item.expWearStrips.name", desc: "settings.item.expWearStrips.desc",
        note: RESTART_NOTE },
      { id: "expGradeGuard", type: "bool", default: false, name: "settings.item.expGradeGuard.name", desc: "settings.item.expGradeGuard.desc",
        note: RESTART_NOTE },
      { id: "expPhotoSurface", type: "bool", default: false, name: "settings.item.expPhotoSurface.name", desc: "settings.item.expPhotoSurface.desc",
        note: RESTART_NOTE },
      { id: "expTerrainBumps", type: "bool", default: false, name: "settings.item.expTerrainBumps.name", desc: "settings.item.expTerrainBumps.desc",
        note: RESTART_NOTE },
      { id: "expCliffWalls", type: "bool", default: false, name: "settings.item.expCliffWalls.name", desc: "settings.item.expCliffWalls.desc",
        note: RESTART_NOTE },
      { id: "expTunnelLids", type: "bool", default: false, name: "settings.item.expTunnelLids.name", desc: "settings.item.expTunnelLids.desc",
        note: RESTART_NOTE },
      { id: "expSubseaTunnels", type: "bool", default: false, name: "settings.item.expSubseaTunnels.name", desc: "settings.item.expSubseaTunnels.desc",
        note: RESTART_NOTE },
      { id: "expAiTunnels", type: "bool", default: false, name: "settings.item.expAiTunnels.name", desc: "settings.item.expAiTunnels.desc",
        note: RESTART_NOTE },
      { id: "expRailCollide", type: "bool", default: false, name: "settings.item.expRailCollide.name", desc: "settings.item.expRailCollide.desc",
        note: RESTART_NOTE },
      { id: "expCatenary", type: "bool", default: false, name: "settings.item.expCatenary.name", desc: "settings.item.expCatenary.desc",
        note: RESTART_NOTE },
      { id: "expRailCrossings", type: "bool", default: false, name: "settings.item.expRailCrossings.name", desc: "settings.item.expRailCrossings.desc",
        note: RESTART_NOTE },
      { id: "expRailTunnels", type: "bool", default: false, name: "settings.item.expRailTunnels.name", desc: "settings.item.expRailTunnels.desc",
        note: RESTART_NOTE },
      { id: "expRailCurves", type: "bool", default: false, name: "settings.item.expRailCurves.name", desc: "settings.item.expRailCurves.desc",
        note: RESTART_NOTE },
      { id: "expWatertight", type: "bool", default: false, name: "settings.item.expWatertight.name", desc: "settings.item.expWatertight.desc",
        note: RESTART_NOTE },
      { id: "expCanadaLidar", type: "bool", default: false, name: "settings.item.expCanadaLidar.name", desc: "settings.item.expCanadaLidar.desc",
        note: RESTART_NOTE },
      { id: "expGenGround", type: "bool", default: false, name: "settings.item.expGenGround.name", desc: "settings.item.expGenGround.desc",
        note: RESTART_NOTE },
      { id: "expStaleRevisit", type: "bool", default: false, name: "settings.item.expStaleRevisit.name", desc: "settings.item.expStaleRevisit.desc",
        note: RESTART_NOTE },
    ],
  },
  {
    name: "Interface", label: "settings.page.interface",
    items: [
      { id: "runtimeProfiler", type: "bool", default: false, name: "settings.item.runtimeProfiler.name", desc: "settings.item.runtimeProfiler.desc" },
      { id: "profilerMode", type: "select", default: "essential", name: "settings.item.profilerMode.name", desc: "settings.item.profilerMode.desc", options: [ { label: "settings.option.essential", value: "essential" }, { label: "settings.option.full", value: "full" } ] },
      { id: "statusBar", type: "bool", default: true, name: "settings.item.statusBar.name", desc: "settings.item.statusBar.desc" },
      { id: "placeNamesOnline", type: "bool", default: true, name: "settings.item.placeNamesOnline.name", desc: "settings.item.placeNamesOnline.desc" },
      { id: "telemetry", type: "bool", default: true, name: "settings.item.telemetry.name", desc: "settings.item.telemetry.desc" },
      { id: "telemetryPerf", type: "bool", default: true, name: "settings.item.telemetryPerf.name", desc: "settings.item.telemetryPerf.desc", showIf: c => c.telemetry !== false },
      { id: "telemetryNet", type: "bool", default: true, name: "settings.item.telemetryNet.name", desc: "settings.item.telemetryNet.desc", showIf: c => c.telemetry !== false },
      { id: "mapStyle", type: "select", default: "2d", name: "settings.item.mapStyle.name", desc: "settings.item.mapStyle.desc", options: [ { label: "settings.option.2d", value: "2d" }, { label: "settings.option.3dexperimental", value: "3d" } ] },
      { id: "routeAvoidHighways", type: "bool", default: false, name: "settings.item.routeAvoidHighways.name", desc: "settings.item.routeAvoidHighways.desc" },
      { id: "hudVisible", type: "bool", default: false, name: "settings.item.hudVisible.name", desc: "settings.item.hudVisible.desc" },
      { id: "dashboardSatellite", type: "bool", default: true, name: "settings.item.dashboardSatellite.name", desc: "settings.item.dashboardSatellite.desc" },
      { id: "creditsVisible", type: "bool", default: true, name: "settings.item.creditsVisible.name", desc: "settings.item.creditsVisible.desc" },
      { id: "creditsCorner", type: "select", default: "bottom-left", name: "settings.item.creditsCorner.name", desc: "settings.item.creditsCorner.desc", showIf: c => c.creditsVisible !== false,
        options: [ { label: "settings.option.bottomleft", value: "bottom-left" }, { label: "settings.option.bottomright", value: "bottom-right" }, { label: "settings.option.topleft", value: "top-left" }, { label: "settings.option.topright", value: "top-right" } ] },
      { id: "creditsSize", type: "select", default: "small", name: "settings.item.creditsSize.name", desc: "settings.item.creditsSize.desc", showIf: c => c.creditsVisible !== false,
        options: [ { label: "settings.option.small", value: "small" }, { label: "settings.option.normal", value: "normal" } ] },
      { id: "mainMenuTile", type: "bool", default: true, name: "settings.item.mainMenuTile.name", desc: "settings.item.mainMenuTile.desc" },
    ],
  },
  {
    name: "Multiplayer", label: "settings.page.multiplayer",
    items: [
      { id: "beammpOnLaunch", type: "bool", default: true, name: "settings.item.beammpOnLaunch.name", desc: "settings.item.beammpOnLaunch.desc" },
      { id: "beammpLauncher", type: "path", what: "beammp", name: "settings.item.beammpLauncher.name", placeholder: "settings.item.beammpLauncher.placeholder", desc: "settings.item.beammpLauncher.desc" },
    ],
  },
  {
    name: "Storage", label: "settings.page.storage",
    items: [
      { id: "cacheUsage", type: "info", name: "settings.item.cacheUsage.name", desc: "settings.item.cacheUsage.desc" },
      { id: "cacheCapGb", type: "number", default: 20, min: 0, unit: "GB", name: "settings.item.cacheCapGb.name", desc: "settings.item.cacheCapGb.desc" },
      { id: "routeDownload", type: "select", default: "ahead", name: "settings.item.routeDownload.name", desc: "settings.item.routeDownload.desc",
        options: [ { label: "settings.option.off", value: "off" }, { label: "settings.option.ahead12miles", value: "ahead" }, { label: "settings.option.wholeroute", value: "route" } ] },
      { id: "cacheDir", type: "path", what: "cache", name: "settings.item.cacheDir.name", placeholder: "settings.item.cacheDir.placeholder", desc: "settings.item.cacheDir.desc" },
      { id: "cacheClear", type: "action", name: "settings.item.cacheClear.name", desc: "settings.item.cacheClear.desc" },
    ],
  },
  {
    name: "Performance", label: "settings.page.performance",
    intro: "settings.intro.performance",
    items: [
      { id: "serverCores", type: "select", default: "auto", name: "settings.item.serverCores.name", desc: "settings.item.serverCores.desc",
        note: RESTART_NOTE,
        options: [ { label: "settings.option.auto", value: "auto" }, { label: "settings.option.1", value: "1" }, { label: "settings.option.2", value: "2" }, { label: "settings.option.3", value: "3" }, { label: "settings.option.4", value: "4" }, { label: "settings.option.6", value: "6" }, { label: "settings.option.8", value: "8" } ] },
      { id: "serverPriority", type: "select", default: "low", name: "settings.item.serverPriority.name", desc: "settings.item.serverPriority.desc",
        note: RESTART_NOTE,
        options: [ { label: "settings.option.low", value: "low" }, { label: "settings.option.normal", value: "normal" } ] },
      { id: "teleportPriority", type: "bool", default: false, name: "settings.item.teleportPriority.name", desc: "settings.item.teleportPriority.desc" },
      { id: "memoryUsage", type: "info", name: "settings.item.memoryUsage.name", desc: "settings.item.memoryUsage.desc" },
      { id: "maxRamGb", type: "select", default: "auto", name: "settings.item.maxRamGb.name", desc: "settings.item.maxRamGb.desc",
        note: "settings.item.maxRamGb.note",
        options: [ { label: "settings.option.auto", value: "auto" }, { label: "settings.option.3gb", value: "3" }, { label: "settings.option.4gb", value: "4" }, { label: "settings.option.6gb", value: "6" }, { label: "settings.option.8gb", value: "8" }, { label: "settings.option.12gb", value: "12" }, { label: "settings.option.16gb", value: "16" }, { label: "settings.option.24gb", value: "24" }, { label: "settings.option.32gb", value: "32" } ] },
    ],
  },
  {
    // third-party add-ons (EarthAddons.vue, earthAddonsStore.js): the page is the add-ons' own view, no settings rows
    name: "Add-ons", label: "settings.page.addons",
    addons: true,
    items: [],
  },
  {
    name: "Feedback", label: "settings.page.feedback",
    items: [
      { id: "reportOpen", type: "feedback", what: "report", label: "settings.item.reportOpen.label", name: "settings.item.reportOpen.name", desc: "settings.item.reportOpen.desc" },
      { id: "profilerOpen", type: "feedback", what: "profiler", label: "settings.item.profilerOpen.label", name: "settings.item.profilerOpen.name", desc: "settings.item.profilerOpen.desc" },
      { id: "reportPost", type: "links", name: "settings.item.reportPost.name", desc: "settings.item.reportPost.desc",
        links: [ { label: "settings.item.reportPost.link.githubissues", url: "https://github.com/TecnaGamer/BeamNG.Earth/issues" } ] },
    ],
  },
  {
    name: "About", label: "settings.page.about",
    items: [
      { id: "links", type: "links", name: "settings.item.links.name", desc: "settings.item.links.desc",
        links: [ { label: "settings.item.links.link.github", url: "https://github.com/TecnaGamer/BeamNG.Earth" }, { label: "settings.item.links.link.discord", url: "https://discord.gg/tgf" }, { label: "settings.item.links.link.kofi", url: "https://ko-fi.com/tecnagamer" } ] },
      { id: "connection", type: "info", name: "settings.item.connection.name", desc: "settings.item.connection.desc" },
      { id: "anchor", type: "info", name: "settings.item.anchor.name", desc: "settings.item.anchor.desc" },
      { id: "player", type: "info", name: "settings.item.player.name", desc: "settings.item.player.desc" },
      { id: "weather", type: "info", name: "settings.item.weather.name", desc: "settings.item.weather.desc" },
      { id: "gravity", type: "info", name: "settings.item.gravity.name", desc: "settings.item.gravity.desc" },
      { id: "contributors", type: "contributors", name: "settings.item.contributors.name", desc: "settings.item.contributors.desc" },
      { id: "credits", type: "text", name: "settings.item.credits.name", desc: "settings.item.credits.desc" },
      { id: "attribution", type: "text", name: "settings.item.attribution.name", desc: "settings.item.attribution.desc" },
    ],
  },
  {
    name: "Developer", label: "settings.page.developer",
    devPresets: true,
    items: [
      { id: "tint", type: "dev", name: "settings.item.tint.name", desc: "settings.item.tint.desc" },
      { id: "outlines", type: "dev", name: "settings.item.outlines.name", desc: "settings.item.outlines.desc" },
      { id: "loading", type: "dev", name: "settings.item.loading.name", desc: "settings.item.loading.desc" },
      { id: "physics", type: "dev", name: "settings.item.physics.name", desc: "settings.item.physics.desc" },
      { id: "physicsTerrain", type: "dev", name: "settings.item.physicsTerrain.name", desc: "settings.item.physicsTerrain.desc" },
      { id: "rings", type: "dev", name: "settings.item.rings.name", desc: "settings.item.rings.desc" },
      { id: "probe", type: "dev", name: "settings.item.probe.name", desc: "settings.item.probe.desc" },
      { id: "stats", type: "dev", name: "settings.item.stats.name", desc: "settings.item.stats.desc" },
      { id: "graphs", type: "dev", name: "settings.item.graphs.name", desc: "settings.item.graphs.desc" },
      { id: "map", type: "dev", name: "settings.item.map.name", desc: "settings.item.map.desc" },
      { id: "controls", type: "dev", name: "settings.item.controls.name", desc: "settings.item.controls.desc" },
      { id: "log", type: "dev", name: "settings.item.log.name", desc: "settings.item.log.desc" },
      { id: "devOutlineKm", type: "float", min: 0.5, max: 20, step: 0.5, default: 3, name: "settings.item.devOutlineKm.name", desc: "settings.item.devOutlineKm.desc" },
      { id: "devMapKm", type: "float", min: 1, max: 80, step: 1, default: 10, name: "settings.item.devMapKm.name", desc: "settings.item.devMapKm.desc" },
    ],
  },
]

// Each page's icon (the pause menu's side buttons take the icon's name, LayoutMenu's tabs the icon) and its button id
// (the first keeps the old Mods-tab button's id: settings.lua M.BUTTON_ID, pageExists)
export const PAGE_ICONS = { Graphics: "eyeOutlineOpened", World: "globe", Experimental: "test", Interface: "cogOutline",
  Multiplayer: "peopleOutline", Storage: "floppyDisk", "Add-ons": "puzzleModule", Feedback: "bug", About: "info",
  Developer: "code" }
export const pageId = (name, i) => i === 0 ? "beamngEarth-settings" : "beamngEarth-settings-" + name.toLowerCase()

// The quality presets' cards (settings.lua PRESETS: what each sets)
export const qualityPresets = [
  { id: "low", name: "settings.preset.low.name", short: "settings.preset.low.short", desc: "settings.preset.low.desc" },
  { id: "medium", name: "settings.preset.medium.name", short: "settings.preset.medium.short", desc: "settings.preset.medium.desc" },
  { id: "high", name: "settings.preset.high.name", short: "settings.preset.high.short", desc: "settings.preset.high.desc" },
  { id: "ultra", name: "settings.preset.ultra.name", short: "settings.preset.ultra.short", desc: "settings.preset.ultra.desc" },
  { id: "extreme", name: "settings.preset.extreme.name", short: "settings.preset.extreme.short", desc: "settings.preset.extreme.desc" },
]

export const devPresets = [
  { id: "off", label: "settings.devPreset.off.label" },
  { id: "lod", label: "settings.devPreset.lod.label" },
  { id: "loading", label: "settings.devPreset.loading.label" },
  { id: "physics", label: "settings.devPreset.physics.label" },
  { id: "all", label: "settings.devPreset.all.label" },
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
export const pageLabel = page => t(page.label)
export const optionConfig = { value: o => o.value, label: o => t(o.label) }    // (BngSelect: our options' fields)
export const presetLine = computed(() => {
  const p = qualityPresets.find(q => q.id === config.value.quality)
  return p ? `${t(p.name)}: ${t(p.desc)}` : ""
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
  if (!s.connection) return t('settings.modUnavailable')
  let label = s.connection
  if (s.session) label += " (" + t('settings.connectionSession', { id: s.session }) + ")"
  if (s.lastError) label += ` - ${s.lastError}`
  return label
})

const OFFLINE_STATUS = "Offline - using cached data"
export const statusText = computed(() => {
  const line = status.value.status || "-"
  return typeof line === "string" ? line.replaceAll(OFFLINE_STATUS, () => t("settings.status.offline")) : line
})
// Off the BeamNG.Earth level (a stock level, the main menu) the settings are saved but nothing applies them until the
// level loads (issue #1284: the pause menu's tab on West Coast USA looked as if it did nothing). Strictly false: the
// first poll's answer, not the empty status before it.
export const offLevel = computed(() => status.value.connection !== undefined && status.value.inOurLevel === false)

// "Restart world server": the box at the top of each page (earthRestart.js) while a setting waits for a restart or one
// is under way; the button asks the Lua side (settings.uiRestartServer), the poll shows the phases
const settingNames = {}
settingsSchema.forEach(cat => cat.items.forEach(item => { if (item.id && item.name) settingNames[item.id] = item.name }))
export const restartSent = ref("")     // the request's error, if it was not sent
export const restart = computed(() => {
  const p = status.value.paths      // (the cache folder chosen for the next start: a restart takes it too)
  const more = p && p.cacheNext && p.cacheDir && p.cacheNext !== p.cacheDir ? [t('settings.downloadCacheFolder')] : []
  const v = restartView(status.value.restart, id => settingNames[id] && t(settingNames[id]), more, t)
  return restartSent.value && !v.busy ? { ...v, show: true, level: "warn", note: restartSent.value } : v
})
export function restartServer() {
  restartSent.value = ""
  engineLua("beamngEarth_settings and beamngEarth_settings.uiRestartServer and beamngEarth_settings.uiRestartServer() or nil", res => {
    if (!res || !res.ok) restartSent.value = (res && res.error) || t('settings.modUnavailableLower')
    refresh()
  })
}

// the connection banner's state line (core.banner(): starting / not running / lost / offline / degraded
// / connected, plus the edge guard's line) and its level (info / ok / warn / error)
export const bannerText = computed(() => (status.value.banner && status.value.banner.text) || "")
export const bannerLevel = computed(() => (status.value.banner && status.value.banner.level) || "info")
// a newer version on GitHub: { current, latest, url } (core.updateInfo)
export const update = computed(() => status.value.update || null)
// a link from the About page, in the system's browser through the server (core.openPage: the game's openWebBrowser
// refuses GitHub, Discord and Ko-fi, "unable to open webpage due to domain filter"; the server opens only the
// page's own links and GitHub profiles, updates.openable)
export function openLink(url) {
  if (typeof url === "string" && url.startsWith("https://")) {
    engineLua(`if beamngEarth_core and beamngEarth_core.openPage then beamngEarth_core.openPage(${JSON.stringify(url)}) end`)
  }
}

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
  if (item.id === "anchor") return s.anchorId ? `#${s.anchorId}  ${fmt(s.anchorLat, 5)}, ${fmt(s.anchorLon, 5)}` : t('settings.noOrigin')
  if (item.id === "player") return (typeof s.playerLat === "number") ? t('settings.positionFromOrigin', { latitude: fmt(s.playerLat, 5), longitude: fmt(s.playerLon, 5), distance: number(s.dist / 1000, 1) }) : "-"
  if (item.id === "attribution") return s.attribution || "-"
  if (item.id === "credits") return s.credits || "-"
  if (item.id === "weather") return s.weather || "-"
  if (item.id === "gravity") return s.gravity || "-"
  if (item.id === "cacheUsage") return (s.cache && s.cache.line) || "-"
  if (item.id === "memoryUsage") return (s.memory && s.memory.line) || "-"
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
    cacheResult.value = t('settings.cacheFreed', { size: c.clearedText || "0 MB" })
  } else if (cacheBusy.value && Date.now() - cacheSentAt > 120000) { // a server without the cache, or gone
    cacheBusy.value = false
    cacheResult.value = t('settings.serverTimeout')
  }
  const p = status.value.paths
  if (pathBusy.value && p && p.answers > pathAnswers && p.result) { // the server's answer to our choice
    pathMsg.value = { ...pathMsg.value, [p.result.what]: p.result }
    if (p.result.ok) pathText.value = { ...pathText.value, [p.result.what]: "" }
    pathBusy.value = ""
  } else if (pathBusy.value && Date.now() - pathSentAt > (pathPick.value ? 960000 : 30000)) {
    pathMsg.value = { ...pathMsg.value, [pathBusy.value]: { ok: false, text: t('settings.serverTimeout') } }
    pathBusy.value = ""
  }
  if (state.dev) dev.value = state.dev
}

// The cache folder and the BeamMP launcher: what they are now (the server's app_paths) or the last answer
export function pathNote(item) {
  const m = pathMsg.value[item.what]
  if (m) return m.text
  if (pathBusy.value === item.what) return pathPick.value ? t('settings.pickPath') : "..."
  const p = status.value.paths
  if (!p) return "-"
  if (item.what === "cache") {
    return p.cacheNext && p.cacheNext !== p.cacheDir ? t("settings.pathRestart", { path: p.cacheDir, next: p.cacheNext }) : t("settings.pathInUse", { path: p.cacheDir || "-" })
  }
  return p.beammp ? t('settings.pathCurrent', { path: p.beammp }) : t('settings.pathNotFound')
}

export function sendPath(item, pick, text) {
  pathMsg.value = { ...pathMsg.value, [item.what]: null }
  pathAnswers = (status.value.paths && status.value.paths.answers) || 0
  const script = `beamngEarth_settings and beamngEarth_settings.uiPathSet(${JSON.stringify(item.what)}, ${pick ? "true" : "false"}, ${JSON.stringify(text || "")}) or nil`
  engineLua(script, res => {
    if (res && res.ok) { pathBusy.value = item.what; pathPick.value = pick; pathSentAt = Date.now() }
    else pathMsg.value = { ...pathMsg.value, [item.what]: { ok: false, text: (res && res.error) || t('settings.modUnavailableLower') } }
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
    else cacheResult.value = (res && res.error) || t('settings.modUnavailableLower')
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
  return p ? t(p.name) : id
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
