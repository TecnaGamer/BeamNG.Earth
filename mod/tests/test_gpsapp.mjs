// node mod/tests/test_gpsapp.mjs  (the GPS app's math: BeamNGEarthGps/app.js beamngEarthGpsMath)
import { readFileSync } from "node:fs"
import assert from "node:assert"
import vm from "node:vm"
const src = readFileSync(new URL("../beamng_earth/ui/modules/apps/BeamNGEarthGps/app.js", import.meta.url), "utf8")
let directive = null
const ctx = { angular: { module: () => ({ directive: (name, d) => { directive = name; return {} } }) }, console, Date, Math }
vm.runInNewContext(src + "\nthis.GM = beamngEarthGpsMath", ctx)
const GM = ctx.GM
assert.equal(directive, "beamngEarthGps", "the directive registers")
// zoom by speed, with hysteresis at a band's edge
assert.equal(GM.zoomFor(5, null), 17); assert.equal(GM.zoomFor(20, null), 16); assert.equal(GM.zoomFor(40, null), 15)
assert.equal(GM.zoomFor(14.5, 17), 17, "just over the edge: stays"); assert.equal(GM.zoomFor(17, 17), 16, "clearly over: moves")
// tiles around the player cover the circle, nearest first, relative to the player's pixel
const t = GM.tilesAround(40.44, -80.0, 17, 400)
assert.ok(t.length >= 9 && t.length < 40, "a few dozen tiles: " + t.length)
assert.ok(t[0].left <= 0 && t[0].left + 256 >= 0 && t[0].top <= 0 && t[0].top + 256 >= 0, "the first holds the player")
// the route: pixels relative to the player, runs broken where it leaves the circle
const pz = GM.project(40.44, -80.0, 17)
const pts = [[40.44, -80.0], [40.441, -80.0], [40.442, -80.0], [40.60, -80.0], [40.70, -80.0]]
const runs = GM.routeRuns(pts, 0, pz, 17, 400)
assert.equal(runs.length, 1, "one run near the car")
assert.ok(Math.abs(runs[0][0].x) < 1e-6 && Math.abs(runs[0][0].y) < 1e-6, "starts at the car")
assert.ok(runs[0][1].y < 0, "north is up (screen -y)")
assert.ok(GM.pathOf(runs).startsWith("M0.0 0.0 L"), GM.pathOf(runs).slice(0, 20))
const ch = GM.chevrons(runs, 70, 40)
assert.ok(ch.length > 0 && ch.every(c => Math.abs(c.deg + 90) < 1e-6), "chevrons point north (-90 deg)")
// distances and arrival as the card says them
assert.equal(GM.fmtDist(106.7, true), "350 ft"); assert.equal(GM.fmtDist(19956, true), "12.4 mi")
assert.equal(GM.fmtDist(40000, true), "24.9 mi"); assert.equal(GM.fmtDist(200000, true), "124 mi"); assert.equal(GM.fmtDist(83, false), "80 m")
assert.equal(GM.fmtDist(12400, false), "12.4 km"); assert.equal(GM.fmtDist(-5, true), "0 ft")
const at = new Date(2026, 9, 25, 1, 56, 0).getTime()
assert.equal(GM.fmtArrive(18 * 60, at, true), "Arrive 2:14 AM · 18 min")
assert.equal(GM.fmtArrive(18 * 60, at, false), "Arrive 02:14 · 18 min")
assert.equal(GM.fmtArrive(95 * 60, at, true), "Arrive 3:31 AM · 1 h 35 min")
// every arrow routing.py names has a drawing; an unknown one falls back to straight
for (const a of ["straight", "slight-left", "left", "sharp-left", "uturn", "slight-right", "right", "sharp-right", "roundabout", "arrive"])
  assert.ok(GM.arrowPath(a).startsWith("M"), a)
assert.equal(GM.arrowPath("bogus"), GM.arrowPath("straight"))
console.log("gpsapp ok:", t.length, "tiles,", ch.length, "chevrons")
