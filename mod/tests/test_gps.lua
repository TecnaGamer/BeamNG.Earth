-- luajit mod/tests/test_gps.lua  (the world map's GPS destination on the game's route)
local T = dofile((arg[0]:match('^(.*)[/\\]') or '.') .. '/testlib.lua')
local G = require('beamngEarth/gps')
local nodes = { a = { pos = { x = 0, y = 0, z = 1 } }, b = { pos = { x = 300, y = 0, z = 2 } }, c = { pos = { x = 0, y = 400, z = 3 } } }
local id, p, d = G.pickNode(nodes, 1000, 50)
T.check(id == 'b' and p.z == 2 and math.abs(d - math.sqrt(700 * 700 + 50 * 50)) < 1e-6, 'the node nearest a far destination')
T.check(G.pickNode({}, 0, 0) == nil, 'no graph: none')
T.check(math.abs(G.bearing(0, 0, 0, 10)) < 1e-9 and math.abs(G.bearing(0, 0, 10, 0) - 90) < 1e-9
  and math.abs(G.bearing(0, 0, -10, 0) - 270) < 1e-9, 'bearings: north 0, east 90, west 270')
T.check(G.setDestination(39.28, -76.59, 'x') and G.status().name == 'x', 'destination set')
T.check(not G.setDestination(nil, 1), 'a bad destination refused')
G.clear()
T.check(G.status() == nil, 'cleared')
T.check(G.setDestination(39.28, -76.59, 'x'), 'destination set again')
G.onRouteDownload({ total = 10, done = 3, active = true })
local rd = G.status().routeDownload
T.check(rd.total == 10 and rd.done == 3 and rd.active == true, 'the route_download push reaches status()')
G.clear()
T.check(G.status() == nil, 'cleared again (routeDownload gone with the rest)')
T.check(G.setDestination(39.28, -76.59, 'y') and G.status().routeDownload == nil,
  'a fresh destination starts without the previous one\'s download bar')
local xs, ys = { 0, 100, 100 }, { 0, 0, 100 }
local si, st, sd = G.nearestSegment(xs, ys, 50, 10)
T.check(si == 1 and math.abs(st - 0.5) < 1e-9 and math.abs(sd - 10) < 1e-9, 'on the first segment, halfway, 10 m off')
si, st, sd = G.nearestSegment(xs, ys, 120, 60)
T.check(si == 2 and math.abs(st - 0.6) < 1e-9 and math.abs(sd - 20) < 1e-9, 'on the second segment')
-- a hairpin (GitHub #269): the route goes east along y = 0, turns, and comes back west along y = 20; a car at (100, 12)
-- driving east, last seen 90 m along: on the way out (the way back passes nearer, 8 m, but runs against it)
do
  local hx, hy, hc = { 0, 200, 200, 0 }, { 0, 0, 20, 20 }, { 0, 200, 220, 420 }
  local ri, rt, rd = G.routePosition(hx, hy, hc, 100, 12, 15, 0, 90, 1, 4)
  T.check(ri == 1 and math.abs(rt - 0.5) < 1e-9 and math.abs(rd - 12) < 1e-9, 'hairpin: the car on its own stretch')
  ri = G.routePosition(hx, hy, hc, 100, 12, 0, 0, nil, 1, 4)
  T.check(ri == 3, 'standing, nothing known: the nearest stretch as before')
  ri = G.routePosition(hx, hy, hc, 100, 12, -15, 0, 300, 1, 4)
  T.check(ri == 3, 'on the way back (driving west, last seen 300 m along): that stretch')
end
local gnodes = { a = { pos = { x = 0, y = 0, z = 1 }, links = { b = {} } }, b = { pos = { x = 200, y = 0, z = 3 }, links = { a = {} } } }
local segs = G.edgeSegments(gnodes)
T.check(#segs == 1, 'one edge, once')
local ed, ez = G.nearestEdge(segs, 100, 10)
T.check(math.abs(ed - 10) < 1e-9 and ez == 2, 'a point beside the middle of an edge: on the graph')
-- a divided road: two one-way carriageways 20 m apart; with the route's heading only the one running its way counts
local div = { n1 = { pos = { x = 0, y = 0, z = 0 }, links = { n2 = { oneWay = true, inNode = 'n1' } } },
              n2 = { pos = { x = 0, y = 300, z = 0 }, links = {} },
              s1 = { pos = { x = 20, y = 400, z = 5 }, links = { s2 = { oneWay = true, inNode = 's1' } } },
              s2 = { pos = { x = 20, y = 0, z = 5 }, links = {} } }
local dsegs = G.edgeSegments(div)
local dn = G.nearestEdge(dsegs, 10, 350)
T.check(math.abs(dn - 10) < 1e-9, 'without a heading: past the northbound end, the southbound carriageway 10 m away')
dn = G.nearestEdge(dsegs, 10, 350, 0, 1)
T.check(math.abs(dn - math.sqrt(100 + 50 * 50)) < 1e-9, 'heading north: only the northbound carriageway (its end 51 m off)')
dn = G.nearestEdge(dsegs, 10, 100, 0, -1)
T.check(math.abs(dn - 10) < 1e-9, 'heading south: the southbound one')
local _, _, bp = G.nearestEdge(dsegs, 10, 100, 0, -1)
T.check(bp and math.abs(bp.x - 20) < 1e-9 and math.abs(bp.y - 100) < 1e-9 and bp.dy < 0 and math.abs(bp.dx) < 1e-9,
  'the point on the southbound carriageway, facing the way it is driven')
_, _, bp = G.nearestEdge(dsegs, 10, 100, 0, 1)
T.check(bp and math.abs(bp.x) < 1e-9 and bp.dy > 0, 'and on the northbound one, facing north')
local e = { x = { 0, 100, 100 }, y = { 0, 0, 100 }, cum = { 0, 100, 200 } }
local sl = G.routeSlice(e, 50, 160, 25)
T.check(#sl == 6 and sl[1][1] == 50 and sl[3][1] == 100 and sl[3][2] == 0 and sl[6][1] == 100 and sl[6][2] == 60,
  'the route sliced every 25 m round its corner, both ends included')
sl = G.routeSlice(e, 150, 5000, 25)
T.check(sl[#sl][2] == 100 and #sl == 3, 'a slice past the end stops at the destination')
local ids = G.pathNodes({ { pos = 1 }, { wp = 'a' }, { wp = 'a' }, { wp = 'b' }, { pos = 2, fixed = true }, { pos = 3 } })
T.check(#ids == 2 and ids[1] == 'a' and ids[2] == 'b', 'the AI path: the graph nodes of the planner path, in order, once')
local cmd = G.aiCommand({ 'a', 'b"c' })
T.check(cmd:find('driveUsingPath', 1, true) and cmd:find('"a","b\\"c"', 1, true) and cmd:find('driveInLane = "on"', 1, true),
  'the AI command lists the nodes quoted, in lane')
T.check(loadstring(cmd) ~= nil and loadstring(G.aiCommand(nil)) ~= nil, 'both AI commands are valid Lua')
T.check(G.aiCommand(nil) == 'ai.setMode("disabled")', 'autodrive off: the AI disabled')
-- the AI's path starts on the edge under the car (not at the first node past a turn ahead)
local g2 = { w = { pos = { x = 0, y = 0, z = 0 }, links = { j = {} } }, j = { pos = { x = 100, y = 0, z = 0 }, links = { w = {}, s = {} } },
             s = { pos = { x = 160, y = -60, z = 0 }, links = { j = {} } } }
local ea, eb = G.edgeUnder(g2, 40, 2, 1, 0)
T.check(ea == 'w' and eb == 'j', 'the car heading east on w-j')
T.check(G.edgeUnder(g2, 40, 20, 1, 0) == nil, 'nothing within 8 m: no edge')
local jn = G.joinStart({ 's' }, 'w', 'j', g2)
T.check(#jn == 3 and jn[1] == 'w' and jn[2] == 'j' and jn[3] == 's', 'the path goes through the junction, not across the corner')
jn = G.joinStart({ 'j', 's' }, 'w', 'j', g2)
T.check(#jn == 3 and jn[1] == 'w', 'the edge\'s start put in front')
jn = G.joinStart({ 'w', 'j', 's' }, 'w', 'j', g2)
T.check(#jn == 3, 'already starting there: unchanged')

-- the AI's path across a road-graph change: from the edge under the car, as far as every step is an edge in both graphs
do
  local function n(x, links) return { pos = { x = x, y = 0 }, links = links } end
  local old = { a = n(0, { b = 1 }), b = n(100, { a = 1, c = 1 }), c = n(200, { b = 1, d = 1 }), d = n(300, { c = 1 }) }
  local new = { a = n(0, { b = 1 }), b = n(100, { a = 1, c = 1 }), c = n(200, { b = 1 }), d = n(300, {}) }
  local ids = { 'a', 'b', 'c', 'd' }
  T.check(G.pathIndexAt(ids, old, 150, 3) == 2, 'the car on the second edge')
  local v = G.validFrom(ids, 2, { old, new })
  T.check(#v == 2 and v[1] == 'b' and v[2] == 'c', 'cut where the new graph has no edge')
  v = G.validFrom(ids, 1, { old })
  T.check(#v == 4, 'all valid in the old graph')
  T.check(#G.validFrom({ 'a', 'x' }, 1, { old }) == 1, 'an unknown node: cut')
end

-- a graph update that keeps the AI's path ahead valid leaves the AI alone
do
  local sent = {}
  local veh = { getPosition = function() return { x = 150, y = 0 } end,
                queueLuaCommand = function(_, c) sent[#sent + 1] = c end }
  local keep = { ids = { 'b', 'c' }, full = 2 }
  local function n(x, links) return { pos = { x = x, y = 0 }, links = links } end
  local same = { b = n(100, { c = 1 }), c = n(200, { b = 1 }) }
  local saved = getPlayerVehicle
  getPlayerVehicle = function() return veh end
  G.afterGraph(keep, same)
  T.check(#sent == 0, 'still valid: no new path for the AI')
  G.afterGraph(keep, { b = n(100, {}), c = n(200, {}) })
  T.check(#sent == 1, 'a broken step: the AI gets the part that is left')
  -- a rebase moved the graph (navgraph.transform): the same ids, but the AI's plan held the old frame's points
  local before = G.graphMoves or 0
  G.afterGraph(keep, same, true)
  T.check(#sent == 1 and (G.graphMoves or 0) == before + 1, 'a moved graph: no new path (the AI plans again by itself on the new map)')
  getPlayerVehicle = saved
end
do -- the game's loaded graph keeps each edge on one node only (map.lua convertToSingleSided)
  local P = function(x, y) return { x = x, y = y, z = 0 } end
  local one = {
    a = { pos = P(0, 0), links = { b = { oneWay = false } } },
    b = { pos = P(50, 0), links = {} },
    c = { pos = P(100, 0), links = { b = { oneWay = false } } },
    d = { pos = P(150, 0), links = { c = { oneWay = true, inNode = 'd' } } },
  }
  T.check(#G.validFrom({ 'a', 'b', 'c', 'd' }, 1, { one }) == 4, 'a step stored on either node is an edge')
  local f, t = G.edgeUnder(one, 70, 1, 1, 0)
  T.check(f == 'b' and t == 'c', 'the edge under the car, stored on its far node: ' .. tostring(f) .. tostring(t))
  f, t = G.edgeUnder(one, 70, 1, -1, 0)
  T.check(f == 'c' and t == 'b', 'driven the other way: ' .. tostring(f) .. tostring(t))
  T.check(G.edgeUnder(one, 120, 1, 1, 0) == nil, 'a one-way edge against its way: none')
  f, t = G.edgeUnder(one, 120, 1, -1, 0)
  T.check(f == 'd' and t == 'c', 'a one-way edge its way: ' .. tostring(f) .. tostring(t))
  local j = G.joinStart({ 'c', 'd' }, 'a', 'b', one)
  T.check(#j == 4 and j[1] == 'a' and j[2] == 'b', 'joined across an edge stored on the far node')
end
do -- a new path waits for the car's copy of the graph
  local savedMap, gm = map, { buildSerial = 3 }
  map = { getMap = function() return gm end }
  local veh = { getId = function() return 77 end }
  T.check(not G.vehicleHasGraph(veh), 'a vehicle that has not taken the graph: wait')
  G.onVehicleMapmgrUpdate(77)
  T.check(G.vehicleHasGraph(veh), 'taken: go')
  gm.buildSerial = 4
  T.check(not G.vehicleHasGraph(veh), 'a newer graph: wait again')
  map = savedMap
end
T.check(G.compass(0, 1) == 0 and G.compass(1, 0) == 90 and G.compass(-1, 0) == 270 and G.compass(0, 0) == nil,
  'the car heading as a compass bearing')
do -- the route request carries the car's heading (a nil global here once broke every request)
  local got
  local savedCore, savedGet = rawget(_G, 'beamngEarth_core'), rawget(_G, 'getPlayerVehicle')
  beamngEarth_core = { requestRoute = function(id, la0, lo0, la1, lo1, b) got = { b = b }; return true end }
  getPlayerVehicle = function() return { getDirectionVector = function() return { x = 1, y = 0, z = 0 } end } end
  G.clear()
  G.setDestination(39.3, -76.6, 'route test')
  local anchor = { geodeticToEnu = function() return 0, 0, 0 end }
  local ok, err = pcall(G.update, anchor, { x = 0, y = 0, lat = 39.28, lon = -76.59 })
  T.check(ok, 'update with a destination runs: ' .. tostring(err))
  T.check(got and got.b == 90, 'the route request has the heading east: ' .. tostring(got and got.b))
  G.clear()
  beamngEarth_core, getPlayerVehicle = savedCore, savedGet
end
do -- the minimap's distance counts the whole planned route, not only the arrows' reach
  local rp = { path = { { distToTarget = 2700 }, { distToTarget = 1000 }, { distToTarget = 0 } }, distance = 2700 }
  G.addRest(rp, 480000)
  T.check(rp.path[1].distToTarget == 482700 and rp.path[3].distToTarget == 480000 and rp.distance == 482700,
    'the rest of the route added to every point')
  G.addRest(rp, 0)
  T.check(rp.path[1].distToTarget == 482700, 'nothing left past the path: unchanged')
end
T.done('test_gps')
