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
do -- GPS guidance as the player and the routing server see it (GitHub #623)
  local savedClock = os.clock
  local savedCore, savedGet = rawget(_G, 'beamngEarth_core'), rawget(_G, 'getPlayerVehicle')
  local savedMap, savedMarkers, savedMessage = rawget(_G, 'map'), rawget(_G, 'core_groundMarkers'), rawget(_G, 'ui_message')
  local savedPause = rawget(_G, 'simTimeAuthority')
  local now, requests, responses, commands, player, healthy, online = 0, {}, {}, {}, nil, true, true
  local gamePaused = false
  simTimeAuthority = { getPause = function() return gamePaused end }
  os.clock = function() return now end
  map, core_groundMarkers, ui_message = nil, nil, nil
  local veh = {
    getDirectionVector = function() return { x = player.vx, y = player.vy, z = 0 } end,
    getVelocity = function() return { x = player.vx, y = player.vy, z = 0 } end,
    getId = function() return 1 end,
    queueLuaCommand = function(_, cmd) commands[#commands + 1] = cmd end,
  }
  getPlayerVehicle = function() return veh end
  beamngEarth_core = {
    requestRoute = function(id, lat0, lon0, lat1, lon1, bearing)
      if not online then return false end
      requests[#requests + 1] = { id = id, lat0 = lat0, lon0 = lon0, lat1 = lat1, lon1 = lon1, bearing = bearing }
      return true
    end,
    takeRoute = function(id)
      local r = responses[id]
      responses[id] = nil
      return r
    end,
    serverHealthy = function() return healthy end,
  }

  -- A planar ENU fixture centred on the reported Hunkubakkar destination; route points stay lat/lon.
  local lat0, lon0, mx, my = 63.768, -18.133, 49000, 111000
  local function point(x, y) return { lat0 + y / my, lon0 + x / mx } end
  local anchor = { sx = 0, sy = 0 }
  function anchor:geodeticToEnu(lat, lon) return (lon - lon0) * mx + self.sx, (lat - lat0) * my + self.sy, 0 end
  local rebased = { sx = 10000, sy = -5000, geodeticToEnu = anchor.geodeticToEnu }
  local function step(t, x, y, vx, vy, a)
    a = a or anchor
    now = t
    player = { x = x + a.sx, y = y + a.sy, lat = lat0 + y / my, lon = lon0 + x / mx, vx = vx or 0, vy = vy or 0 }
    G.update(a, player)
  end
  local function fresh()
    G.clear()
    now, requests, responses, commands, healthy, online, gamePaused = 0, {}, {}, {}, true, true, false
  end
  local function answer(points, dist)
    responses[requests[#requests].id] = { points = points, dist = dist, dur = 1000 }
  end
  local function trip(points, dist, x, y)
    fresh()
    G.setDestination(points[#points][1], points[#points][2], 'trip')
    step(0, x, y, 0, 0)
    answer(points, dist)
    step(1, x, y, 0, 0)
  end
  local line = { point(0, 0), point(10000, 0) }

  -- The destination is 20 m away across the block, but the route has 82 m and two turns left.
  fresh()
  local corner = { point(-100, 20), point(31, 20), point(31, 0), point(0, 0) }
  G.setDestination(lat0, lon0, 'corner')
  step(0, 0, 20, 10, 0)
  local s = G.status()
  T.check(s and s.planning and not G.route(), 'a nearby destination waits for its pending road route')
  answer(corner, 182)
  step(1, 0, 20, 10, 0)
  s = G.status()
  T.check(s and math.abs(s.dist - 20) < 1e-6 and math.abs(s.remaining - 82) < 1e-6 and not s.planning,
    'two turns and 82 m remain even though the destination is 20 m away')
  step(2, 31, 20, 31, 0)
  s = G.status()
  T.check(s and math.abs(s.remaining - 51) < 1e-6, 'guidance remains at the first turn')
  step(3, 31, 0, 0, -20)
  step(4, 30.5, 0, -1, 0)
  s = G.status()
  T.check(s and math.abs(s.remaining - 30.5) < 1e-6, 'guidance remains just above 30 m at the last turn')
  step(5, 29.5, 0, -1, 0)
  T.check(G.status() == nil and G.route() == nil, 'arrival below 30 m clears the destination and route')

  trip({ point(-100, 0), point(0, 0) }, 100, -100, 0)
  step(2, -20, 20, 10, 0)
  s = G.status()
  T.check(s and s.dist < 40 and math.abs(s.remaining - 40) < 1e-6,
    'near the destination, the remaining distance includes the approach back to the route')
  step(3, -15, 5, 5, -15)
  T.check(G.status() == nil, 'arrival clears once the approach and remaining route are both short')

  fresh()
  G.setDestination(lat0, lon0, 'snapped endpoint')
  step(0, 150, 0, -10, 0)
  answer({ point(150, 0), point(50, 0) }, 100)
  step(1, 150, 0, -10, 0)
  step(2, 50, 0, -10, 0)
  s = G.status()
  T.check(s and s.dist > 40 and s.remaining < 1e-6, 'the end of a snapped route alone is not arrival far from the destination')
  step(3, 39, 0, -11, 0)
  T.check(G.status() == nil, 'the destination proximity gate also becomes satisfied')

  fresh()
  online = false
  G.setDestination(lat0, lon0, 'offline')
  step(0, 100, 0, 0, 0)
  s = G.status()
  T.check(s and not s.planning, 'offline guidance stays active outside the proximity fallback')
  step(1, 20, 0, 0, 0)
  T.check(G.status() == nil, 'without a requestable route, proximity alone still arrives')
  fresh()
  G.setDestination(lat0, lon0, 'failed plan')
  step(0, 20, 0, 0, 0)
  responses[requests[1].id] = { error = 'no route' }
  step(1, 20, 0, 0, 0)
  T.check(G.status() == nil, 'a failed route uses the proximity fallback')
  fresh()
  G.setDestination(lat0, lon0, 'timed-out plan')
  step(0, 20, 0, 0, 0)
  for t = 1, 30 do step(t, 20, 0, 0, 0) end
  T.check(G.status() and G.status().planning, 'an unanswered initial plan is still awaited at the timeout boundary')
  step(31, 20, 0, 0, 0)
  T.check(G.status() == nil, 'an unanswered plan eventually permits the proximity fallback')

  -- On the same road, a sustained reversal must send a new plan from the car, with its actual heading.
  trip(line, 10000, 5000, 0)
  for k = 0, 7 do step(21 + k, 5000 - 25 * k, 0, -25, 0) end
  step(29, 4801, 0, -24, 0)
  T.check(#requests == 1 and G.status() and not G.status().planning, '199 m backwards does not re-route')
  step(30, 4775, 0, -26, 0)
  s = G.status()
  T.check(#requests == 2 and s and s.planning and math.abs(s.off) < 1e-6
      and math.abs(s.remaining - 5225) < 1e-6 and requests[2].bearing == 270
      and math.abs(requests[2].lon0 - (lon0 + 4775 / mx)) < 1e-9
      and math.abs(requests[2].lon1 - line[2][2]) < 1e-9,
    '225 m backwards on the route requests a westbound plan from the current position')
  for t = 31, 51 do step(t, 5000 - 25 * (t - 21), 0, -25, 0) end
  T.check(#requests == 2 and G.status() and G.status().planning, 'a pending plan suppresses further requests after cooldown')
  if requests[2] then responses[requests[2].id] = { error = 'no route' } end
  step(52, 4225, 0, -25, 0)
  T.check(#requests == 3 and G.status() and G.status().planning and G.route(),
    'after a failed plan, continued backwards travel can be re-requested without dropping the old route')

  trip(line, 10000, 5000, 0)
  for t = 2, 20 do step(t, 5000 - 25 * (t - 2), 0, -25, 0) end
  T.check(#requests == 1, 'a detected reversal waits through the existing 20-second request cooldown')
  step(21, 4525, 0, -25, 0)
  T.check(#requests == 2 and G.status() and G.status().planning, 'the sustained reversal is requested after cooldown')

  trip(line, 10000, 5000, 0)
  for k = 0, 25 do step(21 + k, 5000 + 20 * k, 0, 20, 0) end
  s = G.status()
  T.check(#requests == 1 and s and s.remaining < 5000, 'forward progress on the route never re-routes')
  trip(line, 10000, 5000, 0)
  for k = 0, 45 do step(21 + k, 5000, 0, 0, 0) end
  T.check(#requests == 1 and G.status() and not G.status().planning, 'a stopped car never re-routes')
  trip(line, 10000, 5000, 0)
  local oscillation, previous = { 0, -20, -40, -20, 0, 20, 40, 20 }, nil
  for k = 0, 47 do
    local x = 5000 + oscillation[k % #oscillation + 1]
    step(21 + k, x, 0, previous and x - previous or -20, 0)
    previous = x
  end
  T.check(#requests == 1 and G.status() and not G.status().planning, 'ordinary back-and-forth motion does not re-route')
  trip(line, 10000, 5000, 0)
  for k = 0, 10 do step(21 + k, 5000 - 10 * k, 11 * k, -10, 11) end
  s = G.status()
  T.check(#requests == 1 and s and math.abs(s.off - 110) < 1e-6 and math.abs(s.remaining - 5210) < 1e-6,
    'lateral deviation does not count as backwards travel along the route')

  -- Nearby parallel stretches can switch the projection by hundreds of route metres without the car moving that far.
  local hairpin = { point(0, 0), point(200, 0), point(200, 40), point(0, 40) }
  trip(hairpin, 440, 100, 40)
  step(21, 100, 40, -10, 0)
  step(22, 100, 0, 10, 0)
  for k = 1, 5 do step(22 + k, 100 - 6 * k, 0, -6, 0) end
  s = G.status()
  T.check(#requests == 1 and s and s.remaining > 350 and s.off < 1e-6,
    'a projection jump followed by small movements does not count as a sustained reversal')

  trip(line, 10000, 5000, 0)
  for k = 0, 35 do step(21 + k, 5000 - 6 * k, 0, -5.5, 0) end
  T.check(#requests == 1 and G.status(), 'even a large distance change below 20 km/h does not re-route')
  trip(line, 10000, 5000, 0)
  for k = 0, 34 do step(21 + k, 5000 - 6 * k, 0, -6, 0) end
  T.check(#requests == 2 and G.status() and G.status().planning,
    'a steady reversal just above 20 km/h is caught after more than 200 m')
  trip(line, 10000, 5000, 0)
  for k = 0, 4 do step(21 + k, 5000 - 60 * k, 0, -60, 0) end
  T.check(#requests == 1, 'even a large backwards change must persist for five seconds')
  step(26, 4700, 0, -60, 0)
  T.check(#requests == 2, 'a fast reversal persisting for five seconds re-routes')

  trip(line, 10000, 5000, 0)
  for k = 0, 6 do step(21 + k, 5000 - 30 * k, 0, -30, 0) end
  step(28, 4850, 0, 30, 0)
  for k = 1, 3 do step(28 + k, 4850 - 30 * k, 0, -30, 0) end
  T.check(#requests == 1, 'substantial forward progress resets a partial backwards run')
  for k = 4, 7 do step(28 + k, 4850 - 30 * k, 0, -30, 0) end
  T.check(#requests == 2, 'a fresh sustained reversal after the turnaround does re-route')

  -- A rolling window, not an old minimum retained indefinitely or a series of fixed windows.
  trip(line, 10000, 5000, 0)
  for k = 0, 10 do step(21 + k, 5000 - 10 * k, 0, -10, 0) end
  for k = 1, 44 do
    local odd = k % 2 == 1
    step(31 + k, odd and 4894 or 4900, 0, odd and -6 or 6, 0)
  end
  for k = 1, 11 do step(75 + k, 4900 - 10 * k, 0, -10, 0) end
  T.check(#requests == 1, 'a 100 m change older than the rolling window is not counted again')
  for k = 12, 21 do step(75 + k, 4900 - 10 * k, 0, -10, 0) end
  T.check(#requests == 2, 'a new 210 m change inside the rolling window re-routes')

  -- A paused simulation can retain a nonzero vehicle velocity; unchanged positions break the run.
  trip(line, 10000, 5000, 0)
  for k = 0, 5 do step(21 + k, 5000 - 30 * k, 0, -30, 0) end
  for t = 27, 31 do step(t, 4850, 0, -30, 0) end
  for k = 1, 2 do step(31 + k, 4850 - 30 * k, 0, -30, 0) end
  T.check(#requests == 1, 'a pause with stale velocity does not carry the old backwards distance across it')
  for k = 3, 7 do step(31 + k, 4850 - 30 * k, 0, -30, 0) end
  T.check(#requests == 2, 'a new sustained reversal after the pause re-routes')

  trip(line, 10000, 5000, 0)
  for k = 0, 5 do step(21 + k, 5000 - 30 * k, 0, -30, 0) end
  now, gamePaused = 26.25, true
  G.update(anchor, player)
  gamePaused = false
  for k = 1, 2 do step(26 + k, 4850 - 30 * k, 0, -30, 0) end
  T.check(#requests == 1, 'even a game pause between route checks resets the backwards window')
  for k = 3, 8 do step(26 + k, 4850 - 30 * k, 0, -30, 0) end
  T.check(#requests == 2, 'a sustained reversal after a brief game pause is still detected')

  trip(line, 10000, 5000, 0)
  for k = 0, 5 do step(21 + k, 5000 - 30 * k, 0, -30, 0) end
  step(31, 4820, 0, -30, 0)
  step(32, 4790, 0, -30, 0)
  T.check(#requests == 1, 'a gap in updates breaks the backwards window')
  for k = 2, 7 do step(31 + k, 4820 - 30 * k, 0, -30, 0) end
  T.check(#requests == 2, 'backwards travel after an update gap starts a new window')

  trip(line, 10000, 5000, 0)
  for k = 0, 5 do step(21 + k, 5000 - 30 * k, 0, -30, 0) end
  G.update(anchor, nil)
  for k = 1, 2 do step(26 + k, 4850 - 30 * k, 0, -30, 0) end
  T.check(#requests == 1, 'losing the player breaks the backwards window')
  for k = 3, 8 do step(26 + k, 4850 - 30 * k, 0, -30, 0) end
  T.check(#requests == 2, 'a new backwards run is detected after the player returns')

  trip(line, 10000, 5000, 0)
  for k = 0, 5 do step(21 + k, 5000 - 30 * k, 0, -30, 0) end
  for k = 0, 2 do step(27 + k, 4820 - 30 * k, 0, -30, 0, rebased) end
  T.check(#requests == 1 and G.status() and math.abs(G.status().off) < 1e-6,
    'a rebase does not carry backwards samples into the new frame')
  for k = 3, 7 do step(27 + k, 4820 - 30 * k, 0, -30, 0, rebased) end
  T.check(#requests == 2, 'a fresh backwards run on the rebased route re-routes')

  trip(line, 10000, 5000, 0)
  for k = 0, 5 do step(21 + k, 5000 - 30 * k, 0, -30, 0) end
  G.reset()
  for k = 1, 2 do step(26 + k, 4850 - 30 * k, 0, -30, 0) end
  T.check(#requests == 1 and G.route(), 'a level reset retains the route but discards the old backwards run')
  for k = 3, 8 do step(26 + k, 4850 - 30 * k, 0, -30, 0) end
  T.check(#requests == 2, 'the retained route can detect a new reversal after reset')

  trip(line, 10000, 5000, 0)
  for k = 0, 5 do step(21 + k, 5000 - 30 * k, 0, -30, 0) end
  G.setDestination(line[2][1], line[2][2], 'new destination')
  step(27, 4844, 0, -6, 0)
  answer(line, 10000)
  step(28, 4838, 0, -6, 0)
  for t = 29, 48 do step(t, 4838 - 6 * (t - 28), 0, -6, 0) end
  T.check(#requests == 2 and G.status() and not G.status().planning, 'a new destination starts a fresh backwards window')
  for t = 49, 62 do step(t, 4838 - 6 * (t - 28), 0, -6, 0) end
  T.check(#requests == 3, 'the replacement destination detects its own sustained reversal')

  -- A re-plan can spend several seconds pending while the old route is still displayed.
  trip(line, 10000, 5000, 0)
  local version = G.route() and G.route().version or 0
  step(21, 5000, 130, -30, 0)
  for k = 0, 5 do step(22 + k, 5000 - 30 * k, 0, -30, 0) end
  answer(line, 10000)
  step(28, 4844, 0, -6, 0)
  for t = 29, 42 do step(t, 4844 - 6 * (t - 28), 0, -6, 0) end
  local replanned = G.route()
  T.check(#requests == 2 and replanned and replanned.version > version and G.status() and not G.status().planning,
    'a newly accepted route discards samples gathered while its request was pending')
  for t = 43, 62 do step(t, 4844 - 6 * (t - 28), 0, -6, 0) end
  T.check(#requests == 3, 'backwards travel on the replacement route is detected independently')

  -- Manual re-routing must not change autodrive's existing no-progress window and recovery.
  trip(line, 10000, 5000, 0)
  local replans = G.noProgressReplans or 0
  G.setAutodrive(true)
  for k = 0, 30 do step(21 + k, 5000 - 10 * k, 0, -10, 0) end
  T.check(#requests == 1 and (G.noProgressReplans or 0) == replans, 'autodrive keeps its full no-progress window')
  step(52, 4690, 0, -10, 0)
  T.check(#requests == 1 and (G.noProgressReplans or 0) == replans + 1 and G.autodrive(),
    'autodrive still re-gives its path rather than using the manual re-route detector')
  G.setAutodrive(false)
  for k = 0, 19 do step(53 + k, 4680 - 10 * k, 0, -10, 0) end
  T.check(#requests == 1, 'switching to manual starts a fresh backwards window')
  for k = 20, 21 do step(53 + k, 4680 - 10 * k, 0, -10, 0) end
  T.check(#requests == 2, 'manual backwards travel after autodrive is re-routed')

  trip(line, 10000, 5000, 0)
  replans = G.noProgressReplans or 0
  G.setAutodrive(true)
  healthy = false
  for k = 0, 35 do step(21 + k, 5000, 0, 0, 0) end
  T.check(#requests == 1 and (G.noProgressReplans or 0) == replans and #commands == 1,
    'a server pause holds autodrive without running no-progress recovery')
  healthy = true
  for k = 0, 30 do step(57 + k, 5000 - 10 * k, 0, -10, 0) end
  T.check((G.noProgressReplans or 0) == replans, 'the resumed autodrive gets its no-progress window')
  step(88, 4690, 0, -10, 0)
  T.check((G.noProgressReplans or 0) == replans + 1 and #requests == 1,
    'autodrive recovery still fires after the resumed window')

  G.clear()
  os.clock = savedClock
  beamngEarth_core, getPlayerVehicle, map, core_groundMarkers, ui_message =
    savedCore, savedGet, savedMap, savedMarkers, savedMessage
  simTimeAuthority = savedPause
end
T.done('test_gps')
