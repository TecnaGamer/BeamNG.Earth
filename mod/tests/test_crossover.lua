-- luajit mod/tests/test_crossover.lua  (C2 vertical crossover math + off-screen test, V1_2_DESIGN 2.5)
local T = dofile((arg[0]:match('^(.*)[/\\]') or '.') .. '/testlib.lua')
local X = require('beamngEarth/crossover')

-- smootherstep
T.check(X.smootherstep(-1) == 0 and X.smootherstep(0) == 0 and X.smootherstep(1) == 1 and X.smootherstep(2) == 1, 'smootherstep ends')
T.near(X.smootherstep(0.5), 0.5, 1e-12, 'smootherstep midpoint')
local prev = 0
for i = 1, 100 do
  local v = X.smootherstep(i / 100)
  T.check(v >= prev, 'smootherstep monotone')
  prev = v
end
-- zero slope at both ends (no visible jerk when a tile starts / stops moving)
T.check(X.smootherstep(0.01) < 1e-4 and 1 - X.smootherstep(0.99) < 1e-4, 'smootherstep flat at the ends')

-- amplitude, durations
T.near(X.delta(1.0, 0.5), 1.2 * 1.5 + 0.2, 1e-12, 'Delta = 1.2 (err_new + err_old) + 0.2')
T.check(X.PHASE1_S == 0.25 and X.PHASE2_S == 0.25 and X.COVER_SINK_S == 0.3, 'durations 0.25 + 0.25 s, cover sink 0.3 s')
T.check(X.duration(true) == 0.5 and X.duration(false) == 0.25, 'crossover 0.5 s, rise-only 0.25 s')
T.check(X.MAX_GROUPS == 8 and X.INSTANT_M == 800 and X.MARGIN_DEG == 10 and X.NEAR_POP_M == 5000, 'cap 8, 800 m instant radius, 10 deg margin, near 5 km')
T.check(X.FAR_INSTANT_M == nil, 'no distance beyond which in-view swaps are instant (v1.3 D)')
T.near(X.defaultErr(0), 2, 1e-12, 'fallback err L0 = cell / 4')
-- M1 (docs/V1_4_SPEC.md 3.1): the server's cells per tile edge (hello.cells, core.lua) set it
T.check(X.cells() == 32, 'default: 32 cells (a FORMAT 3 server sends no hello.cells)')
T.check(X.setCells(64) == 64 and X.cells() == 64, 'setCells(64)')
T.near(X.defaultErr(0), 1, 1e-12, '64 cells: L0 fallback err = its 4 m cell / 4')
T.near(X.defaultErr(-1), 0.5, 1e-12, '64 cells: L-1 (2 m cells, the exact level) 0.5')
T.near(X.defaultErr(3), 8, 1e-12, '64 cells: L3 (32 m cells) 8')
T.check(X.setCells(nil) == 32 and X.setCells('x') == 32 and X.setCells(0) == 32 and X.setCells(2.5) == 32,
  'no / invalid cells: 32')
T.near(X.defaultErr(0), 2, 1e-12, 'back to the 32-cell error')

-- offsets over time
local D = 3
T.near(X.addOffset(D, 0), -D, 1e-12, 'add starts at -Delta (under the old surface)')
T.near(X.addOffset(D, 0.25), 0, 1e-12, 'add at 0 after phase 1')
T.near(X.addOffset(D, 0.4), 0, 1e-12, 'add stays at 0 in phase 2')
T.near(X.removeOffset(D, 0), 0, 1e-12, 'remove stays at 0 in phase 1')
T.near(X.removeOffset(D, 0.25), 0, 1e-12, 'remove still at 0 at the phase switch')
T.near(X.removeOffset(D, 0.5), -D, 1e-12, 'remove at -Delta at the end')
T.near(X.ramp(0, -2, 0.15, 0.3), -1, 1e-12, 'cover ramp midpoint')

-- every surface point moves monotonically and continuously from old to new (|new - old| <= Delta)
math.randomseed(3)
for _ = 1, 200 do
  local old = (math.random() - 0.5) * 20
  local new = old + (math.random() - 0.5) * 2 * D
  local function surf(t) return math.max(new + X.addOffset(D, t), old + X.removeOffset(D, t)) end
  T.near(surf(0), old, 1e-9, 'surface starts at old')
  T.near(surf(0.5), new, 1e-9, 'surface ends at new')
  local dir = new >= old and 1 or -1
  local s0, okMono, maxJump = surf(0), true, 0
  for k = 1, 500 do
    local s = surf(k * 0.001)
    if (s - s0) * dir < -1e-12 then okMono = false end
    maxJump = math.max(maxJump, math.abs(s - s0))
    s0 = s
  end
  T.check(okMono, 'monotone old -> new')
  T.check(maxJump < D * 0.02, 'continuous (no step at 1 ms resolution)')
end

-- off-screen test: a cone around the camera forward, wider than the frustum, + 10 deg
local cam = { x = 0, y = 0, z = 100, fx = 0, fy = 1, fz = 0, fovDeg = 60 }
local half = X.coneHalfDeg(60)
T.check(half > 30 and half < 70, 'cone half-angle covers the frustum corners (' .. half .. ' deg)')
local function tile(x, y, s) return X.tileSphere(x, y, x + s, y + s, 100, 2) end
local cx, cy, cz, r = tile(-128, 2000, 256)
T.check(X.sphereInView(cam, cx, cy, cz, r), 'tile ahead: in view')
cx, cy, cz, r = tile(-128, -2256, 256)
T.check(not X.sphereInView(cam, cx, cy, cz, r), 'tile behind: off-screen')
cx, cy, cz, r = tile(3000, -128, 256)
T.check(not X.sphereInView(cam, cx, cy, cz, r), 'tile 90 deg to the side: off-screen')
-- just outside the cone + margin vs just inside
local a = math.rad(half + X.MARGIN_DEG + 3)
T.check(not X.sphereInView(cam, 5000 * math.sin(a), 5000 * math.cos(a), 100, 10), 'outside cone + margin: off-screen')
a = math.rad(half + X.MARGIN_DEG - 3)
T.check(X.sphereInView(cam, 5000 * math.sin(a), 5000 * math.cos(a), 100, 10), 'inside the margin: in view')
-- camera inside the sphere, or a big near tile beside the camera: in view
cx, cy, cz, r = tile(-128, -128, 256)
local vis, d = X.sphereInView(cam, cx, cy, cz, r)
T.check(vis and d == 0, 'camera over the tile: in view, distance 0')
local _, dist = X.sphereInView(cam, 0, 40000, 100, 1000)
T.near(dist, 39000, 1e-6, 'distance to the sphere surface')

T.done('test_crossover')
