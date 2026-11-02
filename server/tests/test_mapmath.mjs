// The world map's tile math (mod ui/ui-vue/mods/beamngEarth/mapmath.js). Run: node server/tests/test_mapmath.mjs
// (server/tests/test_maptiles.py runs it too when node is installed).
import assert from "node:assert/strict"
import * as m from "../../mod/beamng_earth/ui/ui-vue/mods/beamngEarth/mapmath.js"

const near = (a, b, eps, what) => assert.ok(Math.abs(a - b) <= eps, `${what}: ${a} vs ${b}`)

// known tile: Fells Point, Baltimore (39.2826, -76.5916) at z15 is tile 9412/12490 (the OSM slippy-map formula, asinh(tan))
{
  const p = m.project(39.2826, -76.5916, 15)
  assert.equal(Math.floor(p.x / 256), 9412)
  assert.equal(Math.floor(p.y / 256), 12490)
}
// the world corners and the equator
{
  const a = m.project(0, 0, 0)
  near(a.x, 128, 1e-9, "x of 0,0"); near(a.y, 128, 1e-9, "y of 0,0")
  const b = m.project(m.MAX_LAT, -180, 1)
  near(b.x, 0, 1e-9, "west edge"); near(b.y, 0, 1e-3, "north edge")
}
// round trips
for (const [lat, lon] of [[39.2826, -76.5916], [-33.86, 151.21], [64.1, -21.9], [0, 179.99], [-85, -179.99]]) {
  for (const z of [0, 3.5, 12, 18]) {
    const p = m.project(lat, lon, z)
    const r = m.unproject(p.x, p.y, z)
    near(r.lat, lat, 1e-7, `lat round trip ${lat},${lon} z${z}`)
    near(r.lon, lon, 1e-7, `lon round trip ${lat},${lon} z${z}`)
  }
}
// screen <-> lat/lon in a view; the centre maps to the screen centre
{
  const v = [39.28, -76.59, 13.3, 1600, 900]
  const c = m.toScreen(39.28, -76.59, ...v)
  near(c.x, 800, 1e-6, "centre x"); near(c.y, 450, 1e-6, "centre y")
  const p = m.fromScreen(123, 456, ...v)
  const s = m.toScreen(p.lat, p.lon, ...v)
  near(s.x, 123, 1e-6, "screen round trip x"); near(s.y, 456, 1e-6, "screen round trip y")
  // north is up, east is right
  assert.ok(m.toScreen(39.3, -76.59, ...v).y < 450)
  assert.ok(m.toScreen(39.28, -76.5, ...v).x > 800)
}
// the antimeridian: a marker just east of it shows right of a centre just west of it
{
  const s = m.toScreen(0, -179.9, 0, 179.9, 10, 1000, 1000)
  assert.ok(s.x > 500 && s.x < 1000, `antimeridian marker at ${s.x}`)
  assert.equal(m.wrapLon(190), -170); assert.equal(m.wrapLon(-190), 170); assert.equal(m.wrapLon(180), 180)
}
// pan: dragging right moves the centre west; zoomAt keeps the point under the cursor
{
  const p = m.pan(10, 10, 8, 100, 0)
  assert.ok(p.lon < 10)
  near(p.lat, 10, 1e-9, "pan keeps lat")
  const before = m.fromScreen(300, 200, 40, -75, 9, 1200, 800)
  const z = m.zoomAt(40, -75, 9, 11.5, 300, 200, 1200, 800)
  const after = m.fromScreen(300, 200, z.lat, z.lon, z.z, 1200, 800)
  near(after.lat, before.lat, 1e-7, "zoomAt lat"); near(after.lon, before.lon, 1e-7, "zoomAt lon")
  assert.equal(m.zoomAt(0, 0, 5, 99, 0, 0, 10, 10).z, m.MAX_Z)
  assert.equal(m.zoomAt(0, 0, 5, -3, 0, 0, 10, 10).z, m.MIN_Z)
}
// visible tiles cover the view exactly once, wrap x, and drop rows outside the world
{
  const w = 1000, h = 700
  const tiles = m.visibleTiles(39.28, -76.59, 12.4, w, h)
  assert.ok(tiles.length > 0)
  for (const t of tiles) {
    assert.equal(t.z, 12)
    assert.ok(t.x >= 0 && t.x < 4096 && t.y >= 0 && t.y < 4096)
    assert.ok(t.left < w && t.left + t.size > 0 && t.top < h && t.top + t.size > 0, `tile ${t.key} off screen`)
  }
  // every screen pixel sampled is in exactly one tile
  for (const [sx, sy] of [[0, 0], [w - 1, h - 1], [500, 350], [3, 690]]) {
    const n = tiles.filter(t => sx >= t.left && sx < t.left + t.size && sy >= t.top && sy < t.top + t.size).length
    assert.equal(n, 1, `pixel ${sx},${sy}`)
  }
  const world = m.visibleTiles(0, 179, 2, 2000, 3000)
  assert.ok(world.every(t => t.y >= 0 && t.y < 4 && t.x >= 0 && t.x < 4))
  assert.ok(new Set(world.map(t => t.key)).size === world.length)
}
// search: every word, case-insensitive, prefix matches first
{
  const list = [{ name: "Fells Point" }, { name: "Point Reyes" }, { name: "Mount Whitney" }, { name: "Baltimore Point" }]
  assert.deepEqual(m.searchPlaces(list, "point").map(p => p.name), ["Point Reyes", "Fells Point", "Baltimore Point"])
  assert.deepEqual(m.searchPlaces(list, "fells POINT").map(p => p.name), ["Fells Point"])
  assert.deepEqual(m.searchPlaces(list, "  "), [])
  assert.deepEqual(m.searchPlaces(null, "x"), [])
}
assert.equal(m.fmtLatLon(39.28, -76.59), "39.28000°N, 76.59000°W")
console.log("mapmath ok")

// Natural Earth layers from normalised Mercator (normLines / normPoints): the very same strings and labels as
// projecting every vertex, for views at every zoom, over the antimeridian and near the pole; and faster
{
  const data = (await import("../../mod/beamng_earth/ui/ui-vue/mods/beamngEarth/maplayers.js")).default
  const boxes = m.lineBoxes(data.borders), norm = m.normLines(data.borders)
  const sboxes = m.lineBoxes(data.states), snorm = m.normLines(data.states)
  const places = data.places.map(p => [p[0], p[1], p[2], m.placeMinZoom(p[3], p[4]), p[4]])
  const pnorm = m.normPoints(places)
  const views = [[20, 0, 2.3], [39.28, -76.59, 5.5], [0, 179.9, 4], [-10, -179.5, 6.2], [60, 10, 8.7], [84, 0, 3],
                 [46.9, 7.4, 12.4], [-33.9, 151.2, 10]]
  for (const [lat, lon, z] of views) {
    const a = m.visibleLines(data.borders, boxes, lat, lon, z, 2560, 1440)
    const b = m.visibleLines(data.borders, boxes, lat, lon, z, 2560, 1440, 3000, norm)
    assert.deepEqual(b, a, `borders at ${lat},${lon} z${z}`)
    assert.deepEqual(m.visibleLines(data.states, sboxes, lat, lon, z, 1280, 720, 3000, snorm),
                     m.visibleLines(data.states, sboxes, lat, lon, z, 1280, 720), `states at ${lat},${lon} z${z}`)
    assert.deepEqual(m.visibleLabels(places, lat, lon, z, 2560, 1440, { max: 80, norm: pnorm }),
                     m.visibleLabels(places, lat, lon, z, 2560, 1440, { max: 80 }), `places at ${lat},${lon} z${z}`)
  }
  const time = f => { f(); const t0 = performance.now(); for (let i = 0; i < 20; i++) f(); return (performance.now() - t0) / 20 }
  const slow = time(() => m.visibleLines(data.borders, boxes, 20, 0, 2.3, 2560, 1440))
  const fast = time(() => m.visibleLines(data.borders, boxes, 20, 0, 2.3, 2560, 1440, 3000, norm))
  console.log(`mapmath layers from normalised Mercator: identical; world borders ${slow.toFixed(1)} -> ${fast.toFixed(1)} ms a frame`)
}

// tile layers: file paths (server maptiles.tile_path), centre-first order, the EOX credit
{
  assert.equal(m.tilePath("/t/", "sat", 3, 2, 1), "/t/3/2/1.jpg")
  assert.equal(m.tilePath("/t/", "map", 3, 2, 1), "/t/map/3/2/1.jpg")
  assert.equal(m.tilePath("/t/", "labels", 3, 2, 1), "/t/labels/3/2/1.png")
  const tiles = m.visibleTiles(39.28, -76.59, 12.4, 1600, 900)
  const cf = m.centreFirst(tiles, 1600, 900)
  assert.equal(cf.length, tiles.length)
  const dist = t => Math.hypot(t.left + t.size / 2 - 800, t.top + t.size / 2 - 450)
  for (let i = 1; i < cf.length; i++) assert.ok(dist(cf[i - 1]) <= dist(cf[i]), "nearest the centre first")
  assert.ok(cf[0].left <= 800 && cf[0].left + cf[0].size >= 800 && cf[0].top <= 450 && cf[0].top + cf[0].size >= 450,
            "the first tile is under the centre")
  assert.equal(m.layerCredits("sat", false), "")
  assert.ok(m.layerCredits("map", false).startsWith("Map: Terrain Light by EOX IT Services GmbH"))
  assert.ok(m.layerCredits("map", true).includes("Terrain Light and Overlay"))
  assert.ok(m.layerCredits("sat", true).startsWith("Labels: Overlay by EOX") && m.layerCredits("sat", true).includes("OpenStreetMap"))
  console.log("mapmath tile layers ok")
}

// the map's weather badge: a WMO code into one emoji (settings.uiMapState's weather.code)
{
  assert.equal(m.weatherIcon(0), "☀️")
  assert.equal(m.weatherIcon(63), m.weatherIcon(55))             // rain and drizzle: the same bucket
  assert.equal(m.weatherIcon(95), "⛈️")
  assert.equal(m.weatherIcon(73), "❄️")
  assert.equal(m.weatherIcon(undefined), "")
  console.log("mapmath weather icon ok")
}

// the cache view's grid (covermap.py cache_map): zoom, box, decoding and classes
{
  assert.equal(m.cacheGridZoom(2), 6)
  assert.equal(m.cacheGridZoom(12.4), 15)
  assert.equal(m.cacheGridZoom(13.6), 16)
  assert.equal(m.cacheGridZoom(18), 16)
  const b = m.cacheGridBox(39.2826, -76.5916, 13, 1600, 900, 16)
  assert.equal(b.z, 16)
  // the centre's cell is inside, about 8 cells per 256 px tile
  const c = m.project(39.2826, -76.5916, 16)
  assert.ok(b.x0 <= c.x / 256 && c.x / 256 <= b.x1 + 1 && b.y0 <= c.y / 256 && c.y / 256 <= b.y1 + 1)
  near(b.x1 - b.x0 + 1, 1600 / 256 * 8, 1.01, "cells across")
  // a huge window: clipped to the cell limit, inside the world
  const big = m.cacheGridBox(0, 0, 3, 10000, 10000, 6)
  assert.ok((big.x1 - big.x0 + 1) * (big.y1 - big.y0 + 1) <= m.CACHE_MAX_CELLS)
  assert.ok(big.x0 >= 0 && big.y0 >= 0 && big.x1 <= 63 && big.y1 <= 63)
  assert.deepEqual(Array.from(m.b64Bytes("AAEC/w==")), [0, 1, 2, 255])
  // detail classes by ground resolution: z17 imagery at 39 N (~0.9 m) is sharp, z12 (~29 m) coarse, partial flagged
  assert.equal(m.cacheClass("imagery", 17, 39).name, "Sharp")
  assert.equal(m.cacheClass("imagery", 12, 39).name, "Coarse")
  assert.equal(m.cacheClass("imagery", 14 | m.CACHE_PARTIAL, 39).partial, true)
  assert.equal(m.cacheClass("elevation", 16, 39).name, "Sharp")      // 512 px tiles: ~1.2 m
  assert.equal(m.cacheClass("summary", 3, 39).name, "Ready to drive")
  assert.equal(m.cacheClass("mapdata", 0, 39), null)
  assert.equal(m.fmtRes(1.2, true), "1.2 m")
  assert.equal(m.fmtRes(30, false), "98 ft")
  console.log("mapmath cache view ok")
}

// the Players list and its Show all: great-circle distances, and a view fitting everyone (the antimeridian, one player)
{
  near(m.distanceM(0, 0, 0, 1), 111195, 1, "a degree of the equator")
  near(m.distanceM(51.5074, -0.1278, 40.7128, -74.006), 5570e3, 5e3, "London - New York")
  near(m.distanceM(0, 179.5, 0, -179.5), m.distanceM(0, 0, 0, 1), 1e-6, "across the antimeridian: the short way")
  assert.equal(m.distanceM(36.6, -118, 36.6, -118), 0)
  const one = m.fitView([{ lat: 36.6, lon: -118 }], 1280, 720, 60, 12)
  assert.deepEqual([Math.round(one.lat * 1e6) / 1e6, one.lon, one.z], [36.6, -118, 12], "one player: on them, at maxZ")
  // Fiji and Samoa (either side of 180): side by side round 180, zoomed in, not the whole world between them
  const pac = m.fitView([{ lat: -17.7, lon: 178 }, { lat: -13.8, lon: -172 }], 1280, 720, 60, 12)
  near(Math.abs(pac.lon), 177, 1e-9, "centred on the antimeridian side")
  assert.ok(pac.z > 4, `zoomed in on the Pacific: z ${pac.z}`)
  for (const p of [{ lat: -17.7, lon: 178 }, { lat: -13.8, lon: -172 }]) {
    const s = m.toScreen(p.lat, p.lon, pac.lat, pac.lon, pac.z, 1280, 720)
    assert.ok(s.x >= 59.99 && s.x <= 1220.01 && s.y >= 59.99 && s.y <= 660.01, `inside the padded view: ${s.x}, ${s.y}`)
  }
  // players round the world: zoomed out, all of them still on screen
  const spread = [{ lat: 50, lon: 0 }, { lat: -30, lon: 120 }, { lat: 10, lon: -120 }]
  const world = m.fitView(spread, 1280, 720, 60, 12)
  assert.ok(world.z < 3, `zoomed out: z ${world.z}`)
  for (const p of spread) {
    const s = m.toScreen(p.lat, p.lon, world.lat, world.lon, world.z, 1280, 720)
    assert.ok(s.x >= 59.99 && s.x <= 1220.01 && s.y >= 59.99 && s.y <= 660.01, `round the world, on screen: ${s.x}, ${s.y}`)
  }
  // more than the map's widest view holds: at MIN_Z, not past it
  assert.equal(m.fitView([{ lat: 80, lon: 0 }, { lat: -80, lon: 0 }], 1280, 720, 60, 12).z, m.MIN_Z)
  console.log("mapmath players ok")
}
