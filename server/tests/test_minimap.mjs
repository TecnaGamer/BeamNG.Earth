// Driving HUD mini-map math and BeamNG app metadata. Run: node server/tests/test_minimap.mjs
import assert from "node:assert/strict"
import { readFile } from "node:fs/promises"
import vm from "node:vm"
import * as map from "../../mod/beamng_earth/ui/ui-vue/mods/beamngEarth/mapmath.js"

const appPath = new URL("../../mod/beamng_earth/ui/modules/apps/BeamNGEarthMiniMap/app.js", import.meta.url)
const source = await readFile(appPath, "utf8")
const directives = []
const context = {
  angular: { module: () => ({ directive: name => directives.push(name) }) },
  Math, Number, Object,
}
vm.runInNewContext(source, context, { filename: "BeamNGEarthMiniMap/app.js" })
const mini = context.beamngEarthMiniMapMath
assert.ok(mini, "mini-map exposes its pure map math")

for (const [lat, lon, w, h] of [[42.1, -85.7, 228, 252], [39.2826, -76.5916, 220, 220], [0, 179.99, 220, 220], [-70, -179.9, 237, 206]]) {
  const z = 16
  const actual = mini.visibleTiles(lat, lon, z, w, h)
  const expected = map.visibleTiles(lat, lon, z, w, h)
  assert.deepEqual(JSON.parse(JSON.stringify(actual)), JSON.parse(JSON.stringify(expected)), `tiles at ${lat},${lon}`)
  assert.ok(actual.length > 0)
  for (const t of actual) {
    assert.ok(t.x >= 0 && t.x < 2 ** z && t.y >= 0 && t.y < 2 ** z)
    assert.ok(t.left < w && t.left + t.size > 0 && t.top < h && t.top + t.size > 0)
  }
}

for (const [lat, lon] of [[42.1, -85.7], [0, 179.99], [-33.86, 151.21]]) {
  const actual = mini.toScreen(lat, lon, 42.1, -85.7, 16, 228, 252)
  const expected = map.toScreen(lat, lon, 42.1, -85.7, 16, 228, 252)
  assert.deepEqual(JSON.parse(JSON.stringify(actual)), JSON.parse(JSON.stringify(expected)))
}
const route = mini.routePath([[42.1, -85.703], [42.1, -85.7], [42.1, -85.697]], 0, 2, 42.1, -85.7, 16, 220, 220)
assert.ok(route.startsWith("M"), "GPS route should produce an SVG path")
assert.ok(route.includes(" L"), "GPS route should join visible route segments")
assert.equal(mini.routePath([], 0, 0, 42.1, -85.7, 16, 220, 220), "")
assert.equal(mini.wrapLon(190), -170)
assert.equal(mini.wrapLon(-190), 170)
assert.equal(mini.wrapLon(180), 180)

const appJson = JSON.parse(await readFile(new URL("../../mod/beamng_earth/ui/modules/apps/BeamNGEarthMiniMap/app.json", import.meta.url), "utf8"))
const layout = JSON.parse(await readFile(new URL("../../mod/beamng_earth/settings/ui_apps/originalLayouts/default/beamngEarth.uilayout.json", import.meta.url), "utf8"))
assert.equal(appJson.appName, "beamngEarthMiniMap")
assert.equal(appJson.directive, "beamngEarthMiniMap")
assert.equal(directives[0], "beamngEarthMiniMap")
assert.match(appJson.domElement, /<beamng-earth-mini-map>/)
assert.ok(layout.apps.some(app => app.appName === "beamngEarthMiniMap"))
const luaSettings = await readFile(new URL("../../mod/beamng_earth/lua/ge/extensions/beamngEarth/settings.lua", import.meta.url), "utf8")
const uiSettings = await readFile(new URL("../../mod/beamng_earth/ui/ui-vue/mods/beamngEarth/EarthSettings.vue", import.meta.url), "utf8")
for (const id of ["miniMapVisible", "miniMapPosition", "miniMapZoom", "miniMapLayer"]) {
  assert.ok(luaSettings.includes(id), `Lua settings should define ${id}`)
  assert.ok(uiSettings.includes(id), `User Interface should expose ${id}`)
}
console.log("mini-map math and app metadata ok")
