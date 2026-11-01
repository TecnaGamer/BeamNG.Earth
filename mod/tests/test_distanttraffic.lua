-- luajit mod/tests/test_distanttraffic.lua: path distance, lane side, continuous fades and deterministic density.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local dt = require('beamngEarth/distanttraffic')

local points = { { 0, 0, 10 }, { 100, 0, 20 }, { 100, 100, 30 } }
local p = dt.prepare('road', points, 20)
T.near(p.length, 200, 1e-9, 'horizontal arc length')
local out = {}
T.check(dt.sample(p, 50, out) == out, 'sample reuses its output')
T.near(out.x, 50, 1e-9, 'halfway along first segment')
T.near(out.y, 0, 1e-9, 'on the road centreline')
T.near(out.z, 15 + dt.LIGHT_HEIGHT_M, 1e-9, 'road height interpolated')
dt.sample(p, 150, out)
T.near(out.x, 100, 1e-9, 'on a northbound segment')
T.near(out.y, 50, 1e-9, 'distance follows the bend')
dt.sample(p, 250, out)
T.near(out.x, 50, 1e-9, 'wrap after path end')
T.near(dt.sample(p, 0).alpha, 0, 0, 'invisible at loop boundary')
T.check(dt.sample(p, 1).alpha < dt.sample(p, 10).alpha, 'smooth appearance at path start')
T.near(dt.sample(p, 199).alpha, dt.sample(p, 1).alpha, 1e-9, 'symmetric end fade')
points[1][1] = 2000
T.near(p.points[1][1], 0, 0, 'source points copied')
local lane = dt.prepare('lane', { { 0, 0, 0 }, { 100, 0, 0 }, { 100, 100, 0 } }, 20, 2)
T.near(dt.sample(lane, 50).y, -2, 1e-9, 'right of eastbound road')
T.near(dt.sample(lane, 150).x, 102, 1e-9, 'right of northbound road')
local a, b = dt.sample(lane, 101.99), dt.sample(lane, 102.01)
T.check(math.abs(a.x - b.x) + math.abs(a.y - b.y) < 0.03, 'lane offset meets continuously at a bend')
local dup = dt.prepare('dup', { { 0, 0, 0 }, { 0, 0, 1 }, { 10, 0, 1 } }, 5)
T.check(#dup.points == 2, 'duplicate horizontal position omitted')
T.check(dt.prepare('bad', { { 0, 0, 0 }, { 0, 0, 10 } }, 5) == nil, 'no vertical road')
T.check(dt.prepare('bad', { { 0, 0, 0 }, { 0 / 0, 0, 0 } }, 5) == nil, 'no nonfinite positions')
T.check(dt.prepare('bad', { { 0, 0, 0 }, { 10, 0, 0 } }, 0) == nil, 'positive speed required')
T.check(dt.prepare('collapsed', { { 0, 0, 0 }, { 10, 0, 0 }, { 10, 10, 0 } }, 5, -10) == nil,
  'an offset that collapses every segment is rejected')
T.near(dt.sample(p, -1).x, dt.sample(p, 199).x, 1e-9, 'negative phase wraps')
local short = dt.prepare('short', { { 0, 0, 0 }, { 10, 0, 0 } }, 5)
T.near(dt.sample(short, 5).alpha, 1, 1e-9, 'short path reaches full opacity at the middle')
T.near(dt.sample(short, 0).alpha, 0, 0, 'short path starts invisible')
T.check(dt.density(4) < dt.density(21), 'quiet overnight, busier evening')
T.near(dt.density(0), dt.density(24), 1e-12, 'midnight continuity')
T.near(dt.density(-3), dt.density(21), 1e-12, 'clock wraps')
T.near(dt.rank('a:42'), dt.rank('a:42'), 0, 'stable seed')
T.check(dt.rank('a:42') ~= dt.rank('a:43'), 'different identities')
T.near(dt.outside({ -100, -100, 100, 100 }, 0, 0), 0, 0, 'inside physical region')
T.near(dt.outside({ -100, -100, 100, 100 }, 130, 140), 50, 1e-9, 'distance outside corner')
T.near(dt.distanceFade(300, 400, 200, 2000, 400), 0, 0, 'near region excluded')
T.near(dt.distanceFade(2100, 400, 200, 2000, 400), 0, 0, 'far region excluded')
T.near(dt.distanceFade(1000, 400, 200, 2000, 400), 1, 0, 'middle range visible')
local head, tail = dt.facing(1, 0, 100, 0)
T.check(head == 1 and tail == 0, 'headlights when approaching')
head, tail = dt.facing(1, 0, -100, 0)
T.check(head == 0 and tail == 1, 'taillights when leaving')
local ha = dt.facing(1, 0, -0.001, 1)
local hb = dt.facing(1, 0, 0.001, 1)
T.check(math.abs(ha - hb) < 0.01, 'no headlight pop when seen side-on')
local view = { cam = { x = 0, y = 0, z = 0 }, forward = { x = 1, y = 0, z = 0 },
  right = { x = 0, y = -1, z = 0 }, up = { x = 0, y = 0, z = 1 }, tanH = 1, tanV = 0.5 }
T.near(dt.viewEdge(100, 0, 0, view), 0, 0, 'centre of view')
T.near(dt.viewEdge(100, 100, 0, view), 1, 0, 'horizontal edge')
T.near(dt.viewEdge(100, 0, 50, view), 1, 0, 'vertical edge')
T.check(dt.viewEdge(-100, 0, 0, view) == math.huge, 'behind the camera is outside')
T.near(dt.viewEdge(100, 0, 60, view), 1.2, 1e-9, 'vertical padding')
view.tanH = 2
T.near(dt.viewEdge(100, 150, 0, view), 0.75, 0, 'wide windows include more horizontal roads')
local busy = dt.prepare('busy-road', { { 0, 0, 0 }, { 4000, 0, 0 } }, 20)
busy.spacing = 50
T.check(dt.slots(busy) == 80, 'peak demand follows road length and spacing')
busy.spacing = 500
T.check(dt.slots(busy) == 8, 'a quiet road has less demand than a highway')
busy.spacing = 3000
T.check(dt.slots(busy) <= 2, 'dirt-road demand stays sparse')
busy.spacing = 50
local previous = dt.phase(busy, 1, 80)
for i = 2, 80 do
  local phase = dt.phase(busy, i, 80)
  T.check(phase - previous >= 30 and phase - previous <= 70, 'stable headways with bounded jitter')
  previous = phase
end
local q = dt.bandQuotas({ 1000, 1000, 100 }, 256)
T.check(q[1] + q[2] + q[3] == 256 and q[3] >= 51, 'distant demand retains a share of a saturated pool')
q = dt.bandQuotas({ 0, 1, 0 }, 256)
T.check(q[1] == 0 and q[2] == 1 and q[3] == 0, 'a lone road does not fill the city budget')
q = dt.bandQuotas({ 0, 0, 0 }, 256)
T.check(q[1] + q[2] + q[3] == 0, 'no compulsory cars without road demand')
T.near(dt.glowRadius(3500 - 1e-4), dt.glowRadius(3500 + 1e-4), 1e-5, 'continuous near/far footprint')
T.check(dt.glowRadius(9000) > dt.glowRadius(4000) and dt.glowRadius(100000) <= 16, 'bounded distant footprint')
local function tile(key, paths)
  return { v = dt.PROTO, key = key, frame = { 40, -76 }, pivot = 10, paths = paths or {
    { id = 'whole-road', points = points, speed = 20, offset = 1 } } }
end
local s = dt.newRoutes()
T.check(s:tile(tile('a')) and s.count == 0, 'enqueue without synchronous route preparation')
while s:step() do end
local route = s.routes['whole-road']
T.check(s.count == 1 and s.points == 3, 'one prepared route')
T.check(s:tile(tile('b')), 'a second tile owns the same whole road')
while s:step() do end
T.check(s.count == 1 and s.routes['whole-road'] == route, 'neighbour preserves the same route and phase')
s:drop('a')
T.check(s.routes['whole-road'] == route, 'one owner remains')
s:drop('b')
T.check(s.count == 0 and s.points == 0 and not next(s.routes), 'last owner releases points')
T.check(s:tile(tile('pending')), 'pending tile accepted')
s:drop('pending')
while s:step() do end
T.check(s.count == 0, 'dropped pending tile cannot resurrect a road')
T.check(not s:tile(tile('bad', { { points = {} } , { points = string.rep('x', 1000) } })), 'invalid payload rejected')
local many = {}
for i = 1, 129 do many[i] = { points = {} } end
T.check(not s:tile(tile('many', many)), 'tile path bound')
local oldCap = dt.MAX_ROUTES
dt.MAX_ROUTES = 2
local admission = dt.newRoutes()
admission:tile(tile('old', { { id = 'a', points = points, speed = 10 }, { id = 'b', points = points, speed = 10 } }))
while admission:step() do end
admission:tile(tile('next', { { id = 'c', points = points, speed = 10 } }))
while admission:step() do end
T.check(not admission:hasWork() and admission.pendingPoints == #points, 'full route capacity sleeps with bounded pending data')
for i = 1, 10000 do
  local key = 'churn' .. i
  admission:tile(tile(key, { { id = key, points = points, speed = 10 } }))
  admission:drop(key)
  admission:step()
end
T.check(#admission.queue <= dt.MAX_TILES and admission.tileCount == 2 and admission.pendingPoints == #points,
  'route saturation bounds stale queue records during tile/drop churn')
admission:drop('old')
while admission:step() do end
T.check(admission.routes.c ~= nil, 'a route waiting at capacity is admitted after old tiles drop')
dt.MAX_ROUTES = oldCap
local oldPointCap = dt.MAX_ROUTE_POINTS
dt.MAX_ROUTE_POINTS = 1024
local bounded = dt.newRoutes()
local dense = {}
for i = 1, 512 do dense[i] = { i, 0, 10 } end
for i = 1, dt.MAX_ROUTE_POINTS / 512 + 10 do bounded:tile(tile('dense' .. i, { { id = 'dense' .. i, points = dense, speed = 10 } })) end
T.check(bounded.points + bounded.pendingPoints <= dt.MAX_ROUTE_POINTS, 'pending preparation shares the point budget')
bounded:drop('dense1')
T.check(bounded.pendingPoints == dt.MAX_ROUTE_POINTS - 512, 'dropping pending data releases its reserved memory')
while bounded:step() do end
T.check(bounded.pendingPoints == 0 and bounded.points <= dt.MAX_ROUTE_POINTS, 'preparation releases raw point reservations')
dt.MAX_ROUTE_POINTS = oldPointCap
dt.MAX_ROUTE_POINTS = 6
local pressure = dt.newRoutes()
local a = tile('pressure-a', { { id = 'pressure-a', points = points, speed = 10 } })
local b = tile('pressure-b', { { id = 'pressure-b1', points = points, speed = 10 },
  { id = 'pressure-b2', points = points, speed = 10 } })
pressure:tile(a); while pressure:step() do end
local accepted, reason = pressure:tile(b)
T.check(not accepted and reason == 'capacity' and pressure.tiles['pressure-b'] == nil,
  'capacity admission is atomic, not a successful tile with permanently missing routes')
pressure:drop('pressure-a')
T.check(pressure:tile(b), 'a rejected tile can be retried after capacity is freed')
while pressure:step() do end
T.check(pressure.routes['pressure-b1'] and pressure.routes['pressure-b2'], 'retry restores every omitted route')
dt.MAX_ROUTE_POINTS = oldPointCap
for i = 1, dt.MAX_TILES + 1 do s:tile(tile('empty' .. i, {})) end
T.check(s.tileCount == dt.MAX_TILES, 'tile count bound')
T.done('test_distanttraffic')
