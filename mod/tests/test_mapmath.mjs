// node mod/tests/test_mapmath.mjs  (mapmath.js: the world map's stand-in tiles while zooming)
import * as mm from "../beamng_earth/ui/ui-vue/mods/beamngEarth/mapmath.js"
import assert from "node:assert"
const tiles = mm.visibleTiles(39.28, -76.59, 10.5, 1280, 720)
const z = tiles[0].z
// nothing ready: no stand-ins
assert.deepEqual(mm.fallbackTiles(tiles, new Set()), [])
// every parent at z-1 ready: each missing tile covered by its parent, scaled 2x, placed over it
const ready = new Set(tiles.map(t => `${z - 1}/${Math.floor(t.x / 2)}/${Math.floor(t.y / 2)}`))
const fb = mm.fallbackTiles(tiles, ready)
assert.ok(fb.length > 0 && fb.every(f => f.z === z - 1 && Math.abs(f.size - tiles[0].size * 2) < 1e-9))
for (const t of tiles) {
  const p = fb.find(f => f.x === Math.floor(t.x / 2) && f.y === Math.floor(t.y / 2))
  assert.ok(p && p.left <= t.left + 1e-6 && p.top <= t.top + 1e-6 && p.left + p.size >= t.left + t.size - 1e-6, "covers it")
}
// a tile that is ready itself needs no stand-in
const one = new Set([...ready, `${z}/${tiles[0].x}/${tiles[0].y}`])
assert.ok(mm.fallbackTiles([tiles[0]], one).length === 0)
console.log("mapmath fallback ok:", fb.length, "stand-ins for", tiles.length, "tiles")

// overlay layers
{
  const lines = [[-10, 50, 10, 50], [100, 10, 110, 12]]
  const boxes = mm.lineBoxes(lines)
  assert.deepStrictEqual(boxes[0], [-10, 50, 10, 50])
  const vis = mm.visibleLines(lines, boxes, 50, 0, 5, 800, 600)
  assert.strictEqual(vis.length, 1, "only the line in view")
  assert.ok(/^[-\d.]+,[-\d.]+ [-\d.]+,[-\d.]+$/.test(vis[0]))
  const pts = [["Big", 0, 50, 2], ["Small", 0.4, 50, 9], ["Near", 0.02, 50.0, 2]]
  const lab = mm.visibleLabels(pts, 50, 0, 5, 800, 600)
  assert.deepStrictEqual(lab.map(l => l.name), ["Big"], "small ones wait for their zoom, overlapping ones are left out")
  assert.deepStrictEqual(mm.visibleLabels(pts, 50, 0, 10, 800, 600).map(l => l.name), ["Big", "Small"])
  assert.strictEqual(mm.visibleLabels(pts, 50, 0, 5, 800, 600, { maxZoom: 4 }).length, 0)
  const vb = mm.viewBox(0, 179.9, 6, 800, 600)
  assert.ok(vb[0] > vb[2], "a view over the antimeridian")
  // zoomed far out (the world 512 px wide in a 2000 px view): a line across the copies' seam (180 deg from the view's
  // centre) stays in one piece (no stroke across the map, the user 2026-11-08) and is drawn in every copy on screen
  const seam = [[25, 22, 40, 22]]
  const far = mm.visibleLines(seam, mm.lineBoxes(seam), 30, -148, 1, 2000, 1000)
  assert.ok(far.length >= 3, "the line in each copy of the world on screen: " + far.length)
  for (const f of far) {
    const xs = f.split(" ").map(q => Number(q.split(",")[0]))
    assert.ok(Math.abs(xs[1] - xs[0]) < 40, "a 15 degree line stays ~21 px long, not a stroke across the map: " + f)
  }
  console.log("mapmath layers ok")
}

// the map's credits by area
{
  const imagery = [["Swiss", 5.9, 45.8, 10.5, 47.8], ["Maryland", -79.5, 37.9, -75.0, 39.7], ["EOX"]]
  const paris = mm.viewBox(48.85, 2.35, 10, 800, 600, 0)
  assert.strictEqual(mm.creditsFor(imagery, "Elevation: X", paris), "Map imagery: EOX; Elevation: X")
  const bern = mm.viewBox(46.95, 7.45, 10, 800, 600, 0)
  assert.strictEqual(mm.creditsFor(imagery, "", bern), "Map imagery: Swiss; EOX")
  console.log("mapmath credits ok")
}

// the search box's places (GitHub #601: no saved places): an empty Lua list arrives as {}, and spreading it threw
{
  assert.deepEqual(mm.luaList({}), [])
  assert.deepEqual(mm.luaList(null), [])
  assert.deepEqual(mm.luaList({ 1: { name: "a" } }), [{ name: "a" }])
  assert.deepEqual(mm.searchList({}, {}), [])
  const got = mm.searchList({}, [{ text: "Fells Point", lat: 39.28, lon: -76.59 }])
  assert.equal(got.length, 1)
  assert.equal(got[0].name, "Fells Point")
  assert.deepEqual(mm.searchPlaces(got, "fells").map(p => p.name), ["Fells Point"])
  assert.deepEqual(mm.searchList([{ name: "Home" }], {}).map(p => p.name), ["Home"])
  console.log("mapmath search list ok")
}

