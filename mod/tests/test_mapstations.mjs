// node mod/tests/test_mapstations.mjs  (mapstations.js: the world map's gas station and EV charger layers; the user,
// 2026-11-09: "a new layer on the map that you can enable, one for gas stations and one for ev's")
import * as ms from "../beamng_earth/ui/ui-vue/mods/beamngEarth/mapstations.js"
import assert from "node:assert"

const items = [
  { lat: 1, lon: 1, name: "Sheetz", kind: "fuel", km: 1 },
  { lat: 1, lon: 1.001, name: "Tesla Supercharger", kind: "ev", km: 2 },
]
// by zoom and by switch
assert.deepEqual(ms.visible(items, { fuel: true, ev: true, z: 11.9 }), [], "none below zoom 12")
assert.equal(ms.visible(items, { fuel: true, ev: true, z: 12 }).length, 2)
assert.deepEqual(ms.visible(items, { fuel: true, ev: false, z: 13 }).map(i => i.kind), ["fuel"], "EV switched off")
assert.deepEqual(ms.visible(items, { fuel: false, ev: true, z: 13 }).map(i => i.kind), ["ev"], "gas switched off")

// names from zoom 15, never two overlapping (the one nearer the centre kept)
const W = 800, H = 600
const a = { sx: 400, sy: 300, name: "Sheetz" }, b = { sx: 398, sy: 317, name: "Speedway" }, c = { sx: 100, sy: 100, name: "BP" }
assert.ok(ms.labels([a, b, c], 14.9, W, H).every(m => !m.showLabel), "no names below zoom 15")
const l = ms.labels([b, a, c], 15, W, H)
assert.deepEqual(l.filter(m => m.showLabel).map(m => m.name).sort(), ["BP", "Sheetz"], "overlap: the nearer to the centre")

// nor over another station's icon (a 76's pump over "Tesla Supercharger", in game 2026-11-09)
const t = { sx: 400, sy: 300, name: "Tesla Supercharger" }, pump = { sx: 410, sy: 314, name: "" }
const lt = ms.labels([t, pump], 15, W, H)
assert.equal(lt[0].showLabel, false, "a name over another icon is left out")
const far = ms.labels([t, { sx: 400, sy: 400, name: "" }], 15, W, H)
assert.equal(far[0].showLabel, true, "an icon elsewhere does not hide it")

// the radius asked: half the view's diagonal, at most 25 km
const r = ms.radiusKm({ lat: 0, lon: 0, z: 15 }, 1280, 720)
assert.ok(r > 3 && r < 4, `zoom 15 at the equator: ${r}`)
assert.equal(ms.radiusKm({ lat: 0, lon: 0, z: 12 }, 1280, 720), 25, "capped at 25 km")

assert.equal(ms.ICON.fuel, "⛽")
assert.equal(ms.ICON.ev, "⚡")
assert.equal(ms.NAMES.fuel, "Gas station")
assert.equal(ms.NAMES.ev, "EV charger")
// the right-click search: a newer one cancels an older one's pending retry (Codex on #1056: A's retry fired after B
// started, cancelled B's polling and routed to A's station)
{
  const timers = []
  const opts = { now: () => 0, later: (f) => { timers.push(f); return timers.length }, cancel: (id) => { if (id) timers[id - 1] = null } }
  const state = { gen: 0, retry: null }
  const asked = [], done = []
  let answerA = null
  ms.nearSearch(state, cb => { asked.push("A"); answerA = cb }, r => done.push(["A", r]), opts)
  answerA({ items: [], partial: true })              // A partial: a retry waits
  assert.equal(timers.filter(Boolean).length, 1)
  let answerB = null
  ms.nearSearch(state, cb => { asked.push("B"); answerB = cb }, r => done.push(["B", r]), opts)
  for (const f of timers.splice(0)) if (f) f()       // any retry still pending fires now
  answerB({ items: [{ name: "B" }], partial: false })
  assert.deepEqual(asked, ["A", "B"], "A's retry never asked again")
  assert.deepEqual(done.map(d => d[0]), ["B"], "only B's answer routes")
  // a partial answer is asked again until done
  const t2 = [], st = { gen: 0, retry: null }, seen = []
  let n = 0
  ms.nearSearch(st, cb => { n++; cb({ items: [], partial: n < 3 }) }, r => seen.push(r), { ...opts, later: f => { t2.push(f); return 0 } })
  while (t2.length) t2.shift()()
  assert.equal(n, 3)
  assert.equal(seen.length, 1)
}
console.log("mapstations ok")
