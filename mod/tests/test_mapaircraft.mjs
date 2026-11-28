// node mod/tests/test_mapaircraft.mjs  (mapaircraft.js: the world map's live air traffic layer)
import * as ma from "../beamng_earth/ui/ui-vue/mods/beamngEarth/mapaircraft.js"
import assert from "node:assert"

const items = [
  { h: "a1b2c3", lat: 1, lon: 1, trk: 370, alt: 35000, gs: 450, f: "DAL1234", t: "A321", g: false, o: "ATL", d: "JFK" },
  { h: "b0b0b0", lat: 1.001, lon: 1, trk: -90, gs: 0, f: "N55", t: "C172", g: true },
  { h: "c0c0c0", lat: 50, lon: 1, trk: 0, f: "OFF" },
  { h: "bad", lat: "x", lon: 1 },
]
const project = (lat, lon) => ({ x: lon * 100, y: lat * 100 })
const onScreen = p => p.x >= 0 && p.x <= 1000 && p.y >= 0 && p.y <= 1000
assert.deepStrictEqual(ma.markers(items, ma.ICON_Z - 1, project, onScreen), [], "zoomed out too far: none")
const m = ma.markers(items, 8, project, onScreen)
assert.strictEqual(m.length, 2, "on screen and valid only")
assert.strictEqual(m[0].rot, 10, "track wrapped")
assert.strictEqual(m[1].rot, 270, "negative track wrapped")
assert.ok(m.every(x => x.showLabel), "few in view: labelled at any zoom")
assert.strictEqual(m[0].title, "DAL1234  A321  FL350  450 kt  ATL > JFK (click: GPS to here)")
assert.strictEqual(m[1].title, "N55  C172  on the ground (click: GPS to here)")
const many = Array.from({ length: 40 }, (_, i) => ({ h: "x" + i, lat: 1, lon: 1 + i / 100, trk: 0, f: "F" + i }))
assert.ok(ma.markers(many, 8, project, onScreen).every(x => !x.showLabel), "many in view, zoomed out: no labels")
assert.ok(ma.markers(many, ma.LABEL_Z, project, onScreen).every(x => x.showLabel), "zoomed in: labels")
assert.strictEqual(ma.label({ f: "<b>VERYLONGCALLSIGN" }).length, 12, "labels cut")
console.log("test_mapaircraft: ok")
