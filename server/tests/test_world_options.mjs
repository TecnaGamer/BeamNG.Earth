import assert from "node:assert/strict"
import { readFileSync } from "node:fs"
import { fileURLToPath } from "node:url"

const root = fileURLToPath(new URL("../../", import.meta.url))
const read = path => readFileSync(new URL(path, `file://${root.replaceAll("\\", "/")}/`), "utf8")
const settings = read("mod/beamng_earth/lua/ge/extensions/beamngEarth/settings.lua")
const ui = read("mod/beamng_earth/ui/ui-vue/mods/beamngEarth/EarthSettings.vue")
const roads = read("mod/beamng_earth/lua/ge/extensions/beamngEarth/roads.lua")
const trees = read("mod/beamng_earth/lua/ge/extensions/beamngEarth/trees.lua")

const worldItems = ui.split('name: "World"')[1].split('name: "Interface"')[0]
for (const id of ["resumePosition", "roads", "bridges", "sidewalks", "osmStreetLights", "osmSpeedSigns", "osmExitSigns"]) {
  assert.ok(settings.includes(`${id} = { kind = 'bool'`), `Lua schema should define ${id}`)
  assert.ok(settings.match(new RegExp(`'${id}'`)), `Lua settings order should include ${id}`)
  assert.ok(worldItems.includes(`id: "${id}"`), `World options should expose ${id}`)
}

for (const id of ["setEnabled", "setBridgesEnabled", "setSidewalksEnabled", "setExitSignsEnabled"]) {
  assert.ok(roads.includes(`function M.${id}`), `Road module should implement ${id}`)
}
assert.ok(roads.includes("if what == 'deck' then enabled = M.bridgesEnabled"), "Bridge decks should have an independent switch")
assert.ok(roads.includes("enabled = M.enabled and M.sidewalksEnabled"), "Sidewalk meshes should have an independent switch")
assert.ok(trees.includes("function M.setRoadside(kind, enabled)"), "Mapped roadside objects should be switchable")
console.log("World settings and road feature switches are connected")
