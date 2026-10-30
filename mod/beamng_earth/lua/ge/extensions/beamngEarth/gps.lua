-- GPS to a place picked on the world map (the user, 2026-10-14: "get the game's GPS system to work with this", then
-- "have the server plan out the whole route so it can work with much longer distances"). The destination is kept
-- as latitude / longitude (it outlives anchors and rebases). The server plans the whole road route (routing.py:
-- OSRM on OpenStreetMap); the map draws it, and the game's own route arrows (core_groundMarkers.setPath: arrows on
-- the road, the navigation app, the minimap's line) follow it on our AI road graph (navgraph.lua), which only
-- covers ~1 km around the player: the arrows go to the farthest point of the planned route ahead (up to LOOKAHEAD_M)
-- that the local graph reaches, set again as the player advances or the graph moves on. Leaving the route by more
-- than OFF_ROUTE_M plans it again from where the player is. Without a planned route (offline): steered toward the
-- destination through the graph node nearest it. Within ARRIVE_M: arrived, cleared.
local M = {}

M.CHECK_S = 1.0
M.ARRIVE_M = 40         -- this close (horizontally): arrived
M.FINAL_M = 60          -- a graph node this close to the destination: the arrows end at the destination itself
M.ON_GRAPH_M = 25       -- a route point this close to a graph node is reachable on the local graph
M.LOOKAHEAD_M = 1500    -- how far along the route the arrows may reach
M.SAMPLE_M = 40         -- route points tried for the arrows' target at least this far apart
M.RETARGET_M = 150      -- the arrows' target is moved on in steps of this much route
M.VIA_M = 250           -- the arrows' path is pinned to the route by a waypoint this often (else the stock planner takes
                        -- its own shortest way to the target: Fells Point 2026-10-14, a loop round a block)
M.OFF_ROUTE_M = 120     -- farther from the route: planned again ...
M.REROUTE_S = 20        -- ... at most this often
-- Past the local graph's edge the arrows go on along the planned route itself (the user, 2026-10-14, a Cannonball run:
-- "the in game arrows would keep ending and I'd have to wait a while before they continue": the graph only covers the
-- terrain block, which lags a fast car): the stock planner's path gets the route's next TAIL_M appended, a point every
-- TAIL_STEP_M on the ground below it.
M.TAIL_M = 2000
M.TAIL_STEP_M = 25
-- Autodrive (the user, 2026-10-14: "get the car to auto drive along a gps route"; also a repeatable drive for the
-- performance recordings): the player's car's own AI (vehicle ai.driveUsingPath) drives the graph part of the arrows'
-- path, in its lane, minding traffic, at the legal speed; given again with every new arrows' path, when the graph was
-- applied again (its nodes are new tables) and when the car has stood still for AUTO_STALL_S.
M.AUTO_STALL_S = 6
M.AUTO_NOPROGRESS_S = 30  -- the route's remaining distance not down by AUTO_PROGRESS_M in this long: the path given again
M.AUTO_PROGRESS_M = 20    -- (a car circling at 4 mph to reach a waypoint it could not, never standing still: GitHub #180)
M.AUTO_RECOVER_M = 150    -- no progress after the path was given again either: the car put on its own carriageway this far
                          -- ahead along the route (wedged against a curb at a junction at 3 mph, the same drive; 40 m put it
                          -- back before the same junction six times)
M.GRAPH_WAIT_S = 4      -- the longest a new path waits for the car's copy of the graph (then given anyway)
M.AUTO_AGGRESSION = 0.3
-- Safety: the AI brakes to a stop while the world server is away (no new ground or roads: an autodrive left on through
-- server restarts drove off the loaded world and wrecked the car, 2026-10-14) and goes on when it is back; a car
-- damaged this much ends the autodrive.
M.AUTO_DAMAGE = 20000

local dest = nil        -- { lat, lon, name }
local route = nil       -- { points = {{lat, lon}...}, dist, dur, version } | false (no route: steering) | nil (none yet)
local routeVersion = 0
local routeError = nil
local pendingId, nextId = nil, 0
local lastRequest = -math.huge
local enu = nil         -- { anchor, x = {}, y = {}, cum = {} } of the route in the current anchor's frame
local progress = 1      -- index of the route segment the player is on
local progressS = nil   -- ... and the distance along the route there (routePosition: no jumps to another bend)
local routedTo = nil    -- the node id (or 'final') the current arrows go to
local lastCheck = -math.huge
local info = nil        -- { dist, bearing, remaining, eta } at the last check
local auto = false      -- autodrive on
local autoPath = nil    -- the node ids last given to the AI
local autoSerial = nil  -- the graph (navgraph applies) they were taken from
local stillSince = nil
local progRemaining, progAt = nil, nil   -- the remaining route distance last seen go down, and when (AUTO_NOPROGRESS_S)
local stuckTries = 0     -- AUTO_NOPROGRESS_S spells in a row without progress
local recoverAhead       -- (below)
local paused = false    -- the AI holds the car (world server away)
local vehGraph = {}     -- vehicle id -> the game's map buildSerial when its mapmgr last took the graph
local pathWait = nil    -- os.clock() since a path waits for the car to have the graph it was planned on
local targetS = nil     -- the route distance of the arrows' target
local downloadProgress = nil   -- { total, done, active } | nil: the route's corridor prefetch (server routeahead.py)

-- Pure: the id and position {x, y, z} of the node in `nodes` (map.getMap().nodes: id -> { pos = {x, y, z} })
-- nearest (tx, ty), and its distance; nil when there are none.
function M.pickNode(nodes, tx, ty)
  local best, bd, bp = nil, math.huge, nil
  for id, n in pairs(nodes or {}) do
    local p = n.pos
    if p then
      local dx, dy = p.x - tx, p.y - ty
      local d = dx * dx + dy * dy
      if d < bd then best, bd, bp = id, d, p end
    end
  end
  if not best then return nil end
  return best, bp, math.sqrt(bd)
end

-- Pure: the segments {ax, ay, bx, by, z} of the graph's edges (each once).
function M.edgeSegments(nodes)
  local segs, seen = {}, {}
  for id, n in pairs(nodes or {}) do
    local a = n.pos
    for other, _ in pairs(n.links or {}) do
      local key = tostring(id) < tostring(other) and (tostring(id) .. '|' .. tostring(other)) or (tostring(other) .. '|' .. tostring(id))
      local o = nodes[other]
      if a and o and o.pos and not seen[key] then
        seen[key] = true
        local e = n.links[other]
        -- the way it may be driven: 0 both, 1 from this node to the other, -1 back (a one-way edge runs from its inNode)
        local way = 0
        if type(e) == 'table' and e.oneWay and e.inNode ~= nil then way = (e.inNode == id) and 1 or -1 end
        segs[#segs + 1] = { a.x, a.y, o.pos.x, o.pos.y, (a.z + o.pos.z) / 2, way }
      end
    end
  end
  return segs
end

-- Pure: the distance from (px, py) to the nearest segment of segs, that segment's z, and {x, y, dx, dy}: the point on it
-- and its direction (the way it may be driven for a one-way edge). With a heading (hx, hy): only segments that may be
-- driven that way (a one-way edge against it is left out: the other carriageway of a divided road).
function M.nearestEdge(segs, px, py, hx, hy)
  local bd, bz, bp = math.huge, 0, nil
  for _, s in ipairs(segs) do
    local ax, ay, bx, by = s[1], s[2], s[3], s[4]
    local dx, dy = bx - ax, by - ay
    if hx and (s[6] or 0) ~= 0 and (dx * hx + dy * hy) * s[6] <= 0 then goto next end
    local L2 = dx * dx + dy * dy
    local t = L2 > 0 and math.max(0, math.min(1, ((px - ax) * dx + (py - ay) * dy) / L2)) or 0
    local qx, qy = ax + t * dx - px, ay + t * dy - py
    local d = qx * qx + qy * qy
    if d < bd then
      bd, bz = d, s[5]
      bp = { x = qx + px, y = qy + py, dx = (s[6] or 0) < 0 and -dx or dx, dy = (s[6] or 0) < 0 and -dy or dy }
    end
    ::next::
  end
  return math.sqrt(bd), bz, bp
end

-- Pure: the compass bearing (deg, 0 = north) from (x0, y0) to (x1, y1) in the east / north frame.
function M.bearing(x0, y0, x1, y1)
  return (math.deg(math.atan2(x1 - x0, y1 - y0)) + 360) % 360
end

-- Pure: the segment of polyline (xs, ys) nearest (px, py), searched over indices [i0, i1): its index i (segment
-- i -> i + 1), the fraction t along it and the distance.
function M.nearestSegment(xs, ys, px, py, i0, i1)
  local bi, bt, bd = 1, 0, math.huge
  for i = math.max(1, i0 or 1), math.min(#xs - 1, (i1 or #xs) - 1) do
    local ax, ay, bx, by = xs[i], ys[i], xs[i + 1], ys[i + 1]
    local dx, dy = bx - ax, by - ay
    local L2 = dx * dx + dy * dy
    local t = L2 > 0 and math.max(0, math.min(1, ((px - ax) * dx + (py - ay) * dy) / L2)) or 0
    local qx, qy = ax + t * dx - px, ay + t * dy - py
    local d = qx * qx + qy * qy
    if d < bd then bi, bt, bd = i, t, d end
  end
  return bi, bt, math.sqrt(bd)
end

-- Pure: where the car is on the route (polyline xs, ys with arc lengths cum), searched over segments [i0, i1): the
-- segment nearest (px, py), but one that runs against the car's way (vx, vy, moving) costs WRONG_WAY_M more and a jump
-- along the route from where the car last was (sPrev) costs JUMP_W per m beyond JUMP_FREE_M: on a winding road the
-- route's next or last bend can pass nearer the car than its own stretch, and the target was then set from there, at
-- times behind the car (the AI stopped and turned round: GitHub #269, Kern Canyon). Returns i, t, the distance.
M.WRONG_WAY_M = 40
M.JUMP_W = 0.1
M.JUMP_FREE_M = 60
function M.routePosition(xs, ys, cum, px, py, vx, vy, sPrev, i0, i1)
  local moving = (vx or 0) ^ 2 + (vy or 0) ^ 2 > 9
  local bi, bt, bd, bc = 1, 0, math.huge, math.huge
  for i = math.max(1, i0 or 1), math.min(#xs - 1, (i1 or #xs) - 1) do
    local ax, ay, bx, by = xs[i], ys[i], xs[i + 1], ys[i + 1]
    local dx, dy = bx - ax, by - ay
    local L2 = dx * dx + dy * dy
    local t = L2 > 0 and math.max(0, math.min(1, ((px - ax) * dx + (py - ay) * dy) / L2)) or 0
    local qx, qy = ax + t * dx - px, ay + t * dy - py
    local d = math.sqrt(qx * qx + qy * qy)
    local c = d
    if moving and dx * vx + dy * vy < 0 then c = c + M.WRONG_WAY_M end
    if sPrev then
      local jump = math.abs(cum[i] + t * (cum[i + 1] - cum[i]) - sPrev)
      if jump > M.JUMP_FREE_M then c = c + M.JUMP_W * (jump - M.JUMP_FREE_M) end
    end
    if c < bc then bi, bt, bd, bc = i, t, d, c end
  end
  return bi, bt, bd
end

local function clearArrows()
  local gm = rawget(_G, 'core_groundMarkers')
  if gm and gm.setPath then pcall(gm.setPath, nil) end
  routedTo = nil
end

local function core() return rawget(_G, 'beamngEarth_core') end

-- Pure: the compass bearing (degrees, 0 north, clockwise) of a direction (x east, y north), or nil when it is ~vertical.
function M.compass(dx, dy)
  if not dx or not dy or dx * dx + dy * dy < 0.01 then return nil end
  return math.floor((math.deg(math.atan2(dx, dy)) + 360) % 360 + 0.5) % 360
end

local function playerVeh()
  local get = rawget(_G, 'getPlayerVehicle')
  return get and get(0) or nil
end

local function requestRoute(player)
  local c = core()
  if not c or not c.requestRoute or not dest or not player or not player.lat then return end
  nextId = nextId + 1
  -- the car's heading: the route starts on the carriageway it drives on, the way it drives (without it OSRM began
  -- divided boulevards on the other carriageway, the car turned round to follow, left the route and was routed
  -- back again, over and over: Timisoara 2026-10-15)
  local veh = playerVeh()
  local d = veh and veh.getDirectionVector and veh:getDirectionVector()
  local b = d and M.compass(d.x, d.y)
  if c.requestRoute(nextId, player.lat, player.lon, dest.lat, dest.lon, b) then
    pendingId, lastRequest = nextId, os.clock()
  end
end

function M.setDestination(lat, lon, name)
  lat, lon = tonumber(lat), tonumber(lon)
  if not lat or not lon then return false end
  dest = { lat = lat, lon = lon, name = name }
  route, routeError, pendingId, enu, progress, info, progressS = nil, nil, nil, nil, 1, nil, nil
  routedTo, lastCheck, lastRequest = nil, -math.huge, -math.huge     -- plan and route at the next update
  downloadProgress = nil         -- the previous destination's bar, not this one's (its own push is coming)
  return true
end

function M.clear()
  if auto then M.setAutodrive(false) end
  dest, route, routeError, pendingId, enu, info = nil, nil, nil, nil, nil, nil
  downloadProgress = nil
  clearArrows()
end

-- The server's route_download push (routeahead.py): { total, done, active }.
function M.onRouteDownload(msg)
  downloadProgress = { total = tonumber(msg.total) or 0, done = tonumber(msg.done) or 0, active = msg.active == true }
end

-- Whether the game shows lengths in metric units (Options > Display, uiUnitLength; GitHub #327: the map said miles)
local function metric()
  local st = rawget(_G, 'settings')
  local ok, v = pcall(function() return st and st.getValue and st.getValue('uiUnitLength') end)
  return ok and v == 'metric'
end
M.metric = metric

function M.status()
  if not dest then return nil end
  return { lat = dest.lat, lon = dest.lon, name = dest.name, dist = info and info.dist, bearing = info and info.bearing,
           metric = metric(),
           remaining = info and info.remaining, eta = info and info.eta, routeVersion = route and route.version or 0,
           progress = progress, planning = pendingId ~= nil, routeError = routeError, pendingId = pendingId,
           autodrive = auto, autoNodes = autoPath and #autoPath or nil, off = info and info.off, arrows = routedTo,
           routeDownload = downloadProgress, replans = M.noProgressReplans, recoveries = M.autoRecoveries }
end

-- The planned route for the map: { version, points = {{lat, lon}...}, dist, dur, elev = {{distM, elevM}...} } or nil.
function M.route()
  if not route then return nil end
  return { version = route.version, points = route.points, dist = route.dist, dur = route.dur, elev = route.elev }
end

local function toEnu(anchor)
  if enu and enu.anchor == anchor then return enu end
  local xs, ys, cum = {}, {}, { 0 }
  for i, p in ipairs(route.points) do
    xs[i], ys[i] = anchor:geodeticToEnu(p[1], p[2], 0)
    if i > 1 then cum[i] = cum[i - 1] + math.sqrt((xs[i] - xs[i - 1]) ^ 2 + (ys[i] - ys[i - 1]) ^ 2) end
  end
  enu = { anchor = anchor, x = xs, y = ys, cum = cum }
  progress = 1
  return enu
end

-- The arrows' target on the planned route: the farthest sampled route point within LOOKAHEAD_M that lies on the local
-- graph (within ON_GRAPH_M of one of its edges); the stock route planner snaps it to the road.
-- the graph's applies count: the game's node table stays the same table when a new graph goes in
local function graphSerial()
  local ok, ng = pcall(require, 'beamngEarth/navgraph')
  return ok and ng.applies and ng.applies() or 0
end

local segCache = { nodes = nil, serial = nil, segs = {} }
local function routeTarget(e, nodes, i, t)
  local serial = graphSerial()
  if segCache.nodes ~= nodes or segCache.serial ~= serial then
    segCache = { nodes = nodes, serial = serial, segs = M.edgeSegments(nodes) }
  end
  local segs = segCache.segs
  local n = #e.x
  local s0 = e.cum[i] + t * (e.cum[i + 1] - e.cum[i])
  local best = nil
  local via, lastVia, misses = {}, s0, 0
  -- sample along the route every SAMPLE_M from the player's spot (the simplified route's vertices can be far apart)
  local s = s0
  local k = i
  while s - s0 <= M.LOOKAHEAD_M do
    s = math.min(s + M.SAMPLE_M, e.cum[n])
    while k < n - 1 and e.cum[k + 1] < s do k = k + 1 end
    local seg = e.cum[k + 1] - e.cum[k]
    local f = seg > 0 and (s - e.cum[k]) / seg or 0
    local x = e.x[k] + f * (e.x[k + 1] - e.x[k])
    local y = e.y[k] + f * (e.y[k + 1] - e.y[k])
    -- on the graph: near an edge drivable the route's way. A divided road's carriageways lie closer than ON_GRAPH_M:
    -- where the local graph ended a little sooner on the car's side, the target fell on the other one and the AI
    -- turned round across the highway to reach it, then circled at 4 mph (GitHub #180, reproduced on US-1, 2026-10-23)
    local d, z, bp = M.nearestEdge(segs, x, y, e.x[k + 1] - e.x[k], e.y[k + 1] - e.y[k])
    local final = s >= e.cum[n] - 1e-6
    if d <= M.ON_GRAPH_M then
      -- the target / waypoint itself on that edge (not the route's point, which can lie nearer the other carriageway:
      -- the game's planner went for that one, the wrong way up a one-way road, and asked for a U-turn; GitHub #180)
      if bp and not final then x, y = bp.x, bp.y end
      best = { s = s, x = x, y = y, z = z, final = final }
      misses = 0
      if s - lastVia >= M.VIA_M then via[#via + 1] = best; lastVia = s end
    elseif best then
      misses = misses + 1                -- the route left the graph (a short gap, a junction's offset: not yet)
      if misses >= 3 then break end
    end
    if final then break end
  end
  if best then
    if via[#via] == best then via[#via] = nil end
    best.via = via
  end
  return best
end

-- Pure: points {x, y} every step along polyline e (x, y, cum) from distance s0 to s1 (both ends included).
function M.routeSlice(e, s0, s1, step)
  local n = #e.x
  s1 = math.min(s1, e.cum[n])
  local out, k, s = {}, 1, s0
  while true do
    while k < n - 1 and e.cum[k + 1] < s do k = k + 1 end
    local seg = e.cum[k + 1] - e.cum[k]
    local f = seg > 0 and math.max(0, math.min(1, (s - e.cum[k]) / seg)) or 0
    out[#out + 1] = { e.x[k] + f * (e.x[k + 1] - e.x[k]), e.y[k] + f * (e.y[k + 1] - e.y[k]) }
    if s >= s1 then break end
    s = math.min(s + step, s1)
  end
  return out
end

-- Pure: the planner's path counted to the destination, not to its own end: every point's distToTarget (and the
-- planner's distance) plus the planned route's rest past the path (the minimap's and the navigation app's distance is
-- path[1].distToTarget: a 300-mile route showed "2.7 km", the arrows' reach, Discord 2026-10-24). The planner's own
-- updates keep it (they only drop passed points and move the first); a re-plan of its own starts from 0 again.
function M.addRest(rp, rest)
  if not rp or type(rp.path) ~= 'table' or not rest or rest <= 0 then return end
  for _, wp in ipairs(rp.path) do wp.distToTarget = (wp.distToTarget or 0) + rest end
  rp.distance = (rp.distance or 0) + rest
end

-- The planner's path (core_groundMarkers.routePlanner.path, {pos, distToTarget, wp, fixed}) goes on along the
-- route from s0: no node of it fixed (a fixed node behind the car would make the planner re-plan back to it).
local function appendTail(gm, e, s0, z0)
  local rp = gm.routePlanner
  if not rp or type(rp.path) ~= 'table' or #rp.path == 0 or not rp.calcDistance then return end
  local ray = rawget(_G, 'castRayStatic')
  local z = z0
  local pts = M.routeSlice(e, s0, s0 + M.TAIL_M, M.TAIL_STEP_M)
  for _, wp in ipairs(rp.path) do wp.fixed = nil end
  for j = 2, #pts do
    local x, y = pts[j][1], pts[j][2]
    if ray then
      local ok, d = pcall(ray, vec3(x, y, z + 60), vec3(0, 0, -1), 200)
      if ok and d and d < 200 then z = z + 60 - d end
    end
    rp.path[#rp.path + 1] = { pos = vec3(x, y, z) }
  end
  rp:calcDistance()
  local total = e.cum[#e.x]
  M.addRest(rp, total - math.min(s0 + M.TAIL_M, total))
end

-- Pure: the graph node ids of a planner path ({wp = id | nil}...): its graph stretch, ids in order, repeats dropped.
function M.pathNodes(path)
  local out = {}
  for _, p in ipairs(path or {}) do
    if p.wp ~= nil and out[#out] ~= p.wp then out[#out + 1] = p.wp end
  end
  return out
end

-- Tests only (tools/autodrive_perf.py --ai-mph): drive at this speed (m/s) instead of the legal one.
M.speedOverride = nil

-- Pure: the vehicle Lua that sets its AI on a path of node ids (nil: off).
function M.aiCommand(ids, aggression)
  if not ids then return 'ai.setMode("disabled")' end
  local q = {}
  for i, id in ipairs(ids) do q[i] = string.format('%q', tostring(id)) end
  local speed = tonumber(M.speedOverride)
  local mode = speed and string.format('routeSpeedMode = "set", routeSpeed = %.2f, ', speed) or 'routeSpeedMode = "legal", '
  return 'ai.driveUsingPath({path = {' .. table.concat(q, ',') .. '}, driveInLane = "on", ' .. mode
    .. 'avoidCars = "on", aggression = ' .. string.format('%.2f', aggression or M.AUTO_AGGRESSION) .. '})'
end

-- Pure: the edge data between nodes a and b, or nil. The game keeps each edge on one of its nodes only once a
-- graph is loaded (map.lua convertToSingleSided): a path's step read as no edge half the time, and every rebuild
-- of the graph cut the AI's path to a node behind the car (Lauterbrunnen 2026-10-15).
function M.edge(nodes, a, b)
  local na, nb = nodes[a], nodes[b]
  if not (na and nb) then return nil end
  return (na.links and na.links[b]) or (nb.links and nb.links[a]) or nil
end

-- Pure: may the edge between a and b be driven from a to b (a one-way edge runs from its inNode)?
local function drivable(e, a)
  return not (type(e) == 'table' and e.oneWay and e.inNode ~= nil and e.inNode ~= a)
end

-- Pure: the graph edge (from id, to id) the car at (px, py) heading (hx, hy) drives on: within 8 m, running its way.
function M.edgeUnder(nodes, px, py, hx, hy)
  local best, bd = nil, 8.0
  local function try(from, to, e)
    local a, b = nodes[from] and nodes[from].pos, nodes[to] and nodes[to].pos
    if not (a and b) or not drivable(e, from) then return end
    local dx, dy = b.x - a.x, b.y - a.y
    local L = math.sqrt(dx * dx + dy * dy)
    if L > 0.1 and (dx * hx + dy * hy) / L > 0.5 then
      local t = math.max(0, math.min(1, ((px - a.x) * dx + (py - a.y) * dy) / (L * L)))
      local qx, qy = a.x + t * dx - px, a.y + t * dy - py
      local d = math.sqrt(qx * qx + qy * qy)
      if d < bd then best, bd = { from, to }, d end
    end
  end
  for id, n in pairs(nodes or {}) do
    for other, e in pairs(n.links or {}) do
      try(id, other, e)
      try(other, id, e)
    end
  end
  if best then return best[1], best[2] end
end

-- Pure: the AI's path starting on the edge (a -> b) under the car: the planner's path starts at the node nearest
-- the car, which can be past a turn just ahead; the AI went straight for it, across the corner and into a pole
-- (Fells Point 2026-10-14).
function M.joinStart(ids, a, b, nodes)
  if not a or #ids == 0 then return ids end
  local out = {}
  if ids[1] == b then
    out[1] = a
  elseif ids[1] ~= a then
    if not (nodes and M.edge(nodes, b, ids[1])) then return ids end
    out[1], out[2] = a, b
  end
  for _, id in ipairs(ids) do out[#out + 1] = id end
  return out
end

-- (debug) the AI's new paths: M.TRACE = true logs each with the car's position and its first nodes'
function M.trace(kind, ids, nodes, p)
  if not M.TRACE then return end
  local parts = {}
  for i = 1, math.min(5, #ids) do
    local n = nodes and nodes[ids[i]]
    parts[i] = n and string.format('%s(%.0f,%.0f)', tostring(ids[i]), n.pos.x, n.pos.y) or tostring(ids[i]) .. '(?)'
  end
  log('I', 'beamngEarth', string.format('gps %s at (%.0f,%.0f) t=%.1f: %d nodes %s', kind, p.x, p.y, os.clock(), #ids,
    table.concat(parts, ' ')))
end

-- The vehicles' copies of the graph: the game sends a new graph to each vehicle a frame or more after it goes in
-- (map.request), and the stock AI given a path on the new graph before then indexed a step missing from its copy
-- ('edgeData' nil in graphpath.lua getEdgePositions): the vehicle's Lua stopped and the car ran straight off the
-- road into a pole (Lauterbrunnen 2026-10-15, twice).
function M.onVehicleMapmgrUpdate(id)
  local m = rawget(_G, 'map')
  local gm = m and m.getMap and m.getMap()
  if id then vehGraph[id] = gm and gm.buildSerial end
end

function M.vehicleHasGraph(veh)
  local m = rawget(_G, 'map')
  local gm = m and m.getMap and m.getMap()
  local serial = gm and gm.buildSerial
  if serial == nil or not (veh and veh.getId) then return true end
  return vehGraph[veh:getId()] == serial
end

local function driveArrowsPath(gm, nodes)
  local veh = playerVeh()
  if not veh or not gm.routePlanner then return end
  if not M.vehicleHasGraph(veh) then
    local now = os.clock()
    if not pathWait then
      pathWait = now
      veh:queueLuaCommand('mapmgr.requestMap()')     -- (answered only when its copy is out of date)
    end
    if now - pathWait < M.GRAPH_WAIT_S then return end
    M.graphWaitsTimedOut = (M.graphWaitsTimedOut or 0) + 1
  end
  pathWait = nil
  local ids = M.pathNodes(gm.routePlanner.path)
  -- the planner's path can be older than the graph (planned before the last apply): a step that is no edge now
  -- disabled the AI's Lua (ai.lua getEdgeLaneConfig, Lauterbrunnen 2026-10-15). The valid part is driven and the
  -- route planned again at the next check
  local valid = M.validFrom(ids, 1, { nodes })
  if #valid < #ids then
    ids, routedTo = valid, nil
    M.pathCuts = (M.pathCuts or 0) + 1
  end
  local p, d = veh:getPosition(), veh:getDirectionVector()
  local hl = math.sqrt(d.x * d.x + d.y * d.y)
  if hl > 0.1 then ids = M.joinStart(ids, M.edgeUnder(nodes, p.x, p.y, d.x / hl, d.y / hl), nodes) end
  if #ids < 2 then return end
  veh:queueLuaCommand(M.aiCommand(ids))
  M.trace('path', ids, nodes, p)
  autoPath, autoSerial, stillSince = ids, graphSerial(), nil
end

-- Pure: the index k of the path edge (ids[k] -> ids[k+1]) nearest (px, py) in nodes (positions), or nil.
function M.pathIndexAt(ids, nodes, px, py)
  local best, bd = nil, math.huge
  for k = 1, #ids - 1 do
    local a, b = nodes[ids[k]], nodes[ids[k + 1]]
    a, b = a and a.pos, b and b.pos
    if a and b then
      local dx, dy = b.x - a.x, b.y - a.y
      local L2 = dx * dx + dy * dy
      local t = L2 > 0 and math.max(0, math.min(1, ((px - a.x) * dx + (py - a.y) * dy) / L2)) or 0
      local qx, qy = a.x + t * dx - px, a.y + t * dy - py
      local d = qx * qx + qy * qy
      if d < bd then best, bd = k, d end
    end
  end
  return best
end

-- Pure: ids[k ..] as far as each step is an edge in every one of the graphs (lists of nodes tables).
function M.validFrom(ids, k, graphs)
  local out = { ids[k] }
  for i = k, #ids - 1 do
    for _, nodes in ipairs(graphs) do
      if not M.edge(nodes, ids[i], ids[i + 1]) then return out end
    end
    out[#out + 1] = ids[i + 1]
  end
  return out
end

-- The road graph is about to be applied anew (navgraph.lua, before map.reset): what the AI's path needs to stay valid.
-- The stock AI keeps its path's node ids across the map change and crashed on a step that was no longer an edge
-- ('edgeData' nil in ai.lua planAhead: the vehicle's Lua disabled, the car stopped for good; I-70 2026-10-15).
function M.beforeGraph(oldNodes)
  if not auto or paused or not autoPath or not oldNodes then return nil end
  local veh = playerVeh()
  if not veh then return nil end
  local p = veh:getPosition()
  local k = M.pathIndexAt(autoPath, oldNodes, p.x, p.y)
  if not k then return nil end
  return { ids = M.validFrom(autoPath, k, { oldNodes }), full = #autoPath - k + 1 }
end

-- ... and applied (newNodes: the game's map now): the AI gets the part of its path valid in both graphs at once,
-- before the new map reaches the vehicle; with less than two nodes left it drives the lanes on its own until the next
-- check gives it the route again.
function M.afterGraph(keep, newNodes, moved)
  local veh = playerVeh()
  if not keep or not veh or not newNodes then return end
  -- (a rebase moved the graph, navgraph.transform: the same ids, every position moved. The AI drops its plan by
  -- itself when its car gets the new map (ai.lua updateGFX: a new mapmgr.mapData -> currentRoute nil) and plans its
  -- path again on it; a path given here would only reset it)
  if moved then M.graphMoves = (M.graphMoves or 0) + 1 end
  local ids = M.validFrom(keep.ids, 1, { newNodes })
  -- the AI's whole path ahead is still a path of the new graph: left alone (each new path resets the AI: the
  -- gearbox's "arcade" note, and right after one it swerved onto a sidewalk into a pole, Lauterbrunnen 2026-10-15)
  if #ids == keep.full and #ids == #keep.ids then
    M.graphUntouched = (M.graphUntouched or 0) + 1
    autoSerial = graphSerial() + 1    -- (navgraph counts this apply right after: autoKeep must not re-path for it)
    return
  end
  -- broken: the AI keeps the part valid in both graphs (its copy is still the old one); the full route on the new
  -- graph follows once the car has it (driveArrowsPath waits). A path planned on the new graph here crashed the
  -- AI's Lua (a step missing from the vehicle's copy, 2026-10-15)
  local p = veh:getPosition()
  M.trace('keep', ids, newNodes, p)
  if #ids >= 2 then
    veh:queueLuaCommand(M.aiCommand(ids))
  else
    veh:queueLuaCommand('ai.setMode("traffic")')
  end
  autoPath, routedTo = ids, nil        -- the full route again at the next check
  M.graphKeeps = (M.graphKeeps or 0) + 1
end

local arrowsSerial = nil
-- target: a vec3, or a list of them (waypoints along the route, then the target)
local function setArrows(key, target, tail, nodes)
  local serial = graphSerial()
  if key == routedTo and serial == arrowsSerial then return end
  local gm = rawget(_G, 'core_groundMarkers')
  if not gm or not gm.setPath then return end
  if pcall(gm.setPath, target, { clearPathOnReachingTarget = false }) then
    routedTo, arrowsSerial = key, serial
    -- (not before the route is in: the AI would set off on its own way toward the destination)
    if auto and route ~= nil and not paused then pcall(driveArrowsPath, gm, nodes) end    -- before the tail: the AI drives the graph part only
    if tail then pcall(appendTail, gm, tail.e, tail.s, tail.z) end
    return true
  end
end

function M.setAutodrive(on)
  on = on and true or false
  if on == auto then return auto end
  auto = on
  autoPath, autoSerial, stillSince, paused, pathWait = nil, nil, nil, false, nil
  progRemaining, progAt, stuckTries = nil, nil, 0
  if on then
    routedTo = nil                       -- the arrows (and the AI's path) set again at the next check
    lastCheck = -math.huge
  else
    local veh = playerVeh()
    if veh then veh:queueLuaCommand(M.aiCommand(nil)) end
  end
  return auto
end
function M.autodrive() return auto end

local function damage(veh)
  local m = rawget(_G, 'map')
  local o = m and m.objects and m.objects[veh:getId()]
  return o and tonumber(o.damage) or 0
end

-- Autodrive kept going: the path given again when the graph was applied anew or the car stands still; paused while the
-- world server is away, ended when the car is wrecked.
local function autoKeep(nodes, now)
  if not auto then return end
  local veh = playerVeh()
  if not veh then return end
  if damage(veh) > M.AUTO_DAMAGE then
    M.setAutodrive(false)
    if ui_message then pcall(ui_message, 'Autodrive off: the car is damaged', 6, 'beamngEarthGps') end
    return
  end
  local c = core()
  local healthy = not (c and c.serverHealthy) or c.serverHealthy()
  if not healthy then
    if not paused then veh:queueLuaCommand('ai.setMode("stop")') end
    paused, stillSince = true, nil
    return
  elseif paused then
    paused, routedTo = false, nil         -- back: the path given again
  end
  local v = veh.getVelocity and veh:getVelocity()
  local speed = v and math.sqrt(v.x * v.x + v.y * v.y) or 0
  if speed < 1 then stillSince = stillSince or now else stillSince = nil end
  if pathWait and M.vehicleHasGraph(veh) then routedTo = nil end     -- the path that waited: given now
  if (autoPath and autoSerial ~= graphSerial()) or (stillSince and now - stillSince > M.AUTO_STALL_S) then
    routedTo = nil
  end
end

-- The car put back on the road AUTO_RECOVER_M ahead along route e (from segment i, fraction t): on the graph edge nearest
-- that point that may be driven the route's way, facing that way, on the ground. Returns true when it was placed.
recoverAhead = function(e, nodes, i, t)
  local veh = playerVeh()
  if not veh then return false end
  local n = #e.x
  local s = math.min(e.cum[i] + t * (e.cum[i + 1] - e.cum[i]) + M.AUTO_RECOVER_M, e.cum[n])
  local k = i
  while k < n - 1 and e.cum[k + 1] < s do k = k + 1 end
  local seg = e.cum[k + 1] - e.cum[k]
  local f = seg > 0 and (s - e.cum[k]) / seg or 0
  local x, y = e.x[k] + f * (e.x[k + 1] - e.x[k]), e.y[k] + f * (e.y[k + 1] - e.y[k])
  local d, z, bp = M.nearestEdge(M.edgeSegments(nodes), x, y, e.x[k + 1] - e.x[k], e.y[k + 1] - e.y[k])
  if not bp or d > M.ON_GRAPH_M then return false end
  local hl = math.sqrt(bp.dx * bp.dx + bp.dy * bp.dy)
  if hl < 1e-6 then return false end
  local pos = vec3(bp.x, bp.y, z + 2)
  local ray = rawget(_G, 'castRayStatic')
  local hit = ray and ray(pos + vec3(0, 0, 40), vec3(0, 0, -1), 100)
  if hit and hit < 100 then pos.z = pos.z + 40 - hit + 0.5 end
  local ok = pcall(spawn.safeTeleport, veh, pos, quatFromDir(vec3(bp.dx / hl, bp.dy / hl, 0), vec3(0, 0, 1)), nil, nil,
    false, nil, true)
  if ok then log('I', 'beamngEarth', string.format('autodrive: no progress, the car put on the route ahead at (%.0f, %.0f)',
    pos.x, pos.y)) end
  return ok
end

-- Every frame (anchor: the current geodesy anchor; player: {x, y, lat, lon}): the route kept to the destination.
function M.update(anchor, player)
  if not dest or not anchor or not player then return end
  local now = os.clock()
  if now - lastCheck < M.CHECK_S then return end
  lastCheck = now
  -- the planned route: asked once (and on re-routes), taken when it arrives
  if pendingId then
    local c = core()
    local r = c and c.takeRoute and c.takeRoute(pendingId)
    if r then
      pendingId = nil
      if r.points and #r.points >= 2 and (tonumber(r.dist) or 0) >= 1 then
        routeVersion = routeVersion + 1
        route = { points = r.points, dist = r.dist, dur = r.dur, version = routeVersion, elev = r.elev }
        routeError, enu, routedTo = nil, nil, nil
      else
        routeError = r.error or 'no route'
        if not route then route = false end
      end
    elseif now - lastRequest > 30 then
      pendingId = nil                      -- no answer: try again below
    end
  elseif route == nil then
    requestRoute(player)
  end

  local tx, ty = anchor:geodeticToEnu(dest.lat, dest.lon, 0)
  local dx, dy = tx - player.x, ty - player.y
  local dist = math.sqrt(dx * dx + dy * dy)
  info = { dist = dist, bearing = M.bearing(player.x, player.y, tx, ty), remaining = dist }
  if dist < M.ARRIVE_M then
    if ui_message then pcall(ui_message, 'Arrived: ' .. (dest.name or 'destination'), 5, 'beamngEarthGps') end
    M.clear()
    return
  end
  local m = rawget(_G, 'map') and map.getMap and map.getMap()
  local nodes = m and m.nodes
  autoKeep(nodes, now)

  if route then
    local e = toEnu(anchor)
    local n = #e.x
    local vx, vy = player.vx or 0, player.vy or 0
    local i, t, off = M.routePosition(e.x, e.y, e.cum, player.x, player.y, vx, vy, progressS, progress - 5,
      progress + 400)
    if off > M.OFF_ROUTE_M then
      i, t, off = M.routePosition(e.x, e.y, e.cum, player.x, player.y, vx, vy, progressS, 1, n)
    end
    progress = i
    progressS = e.cum[i] + t * (e.cum[i + 1] - e.cum[i])
    local remaining = e.cum[n] - (e.cum[i] + t * (e.cum[i + 1] - e.cum[i]))
    info.remaining = remaining + off
    info.off = off
    if auto and not paused then
      if not progRemaining or info.remaining < progRemaining - M.AUTO_PROGRESS_M then
        if progRemaining then stuckTries = 0 end
        progRemaining, progAt = info.remaining, now
      elseif now - progAt > M.AUTO_NOPROGRESS_S then
        stuckTries = stuckTries + 1
        if stuckTries >= 2 then         -- the path given again did not help: onto the route ahead
          stuckTries = 0
          if nodes and recoverAhead(e, nodes, i, t) then M.autoRecoveries = (M.autoRecoveries or 0) + 1 end
        end
        routedTo, progRemaining = nil, nil          -- planned and given again at this check
        M.noProgressReplans = (M.noProgressReplans or 0) + 1
      end
    end
    if route.dist and route.dist > 0 and route.dur then info.eta = route.dur * remaining / route.dist end
    if off > M.OFF_ROUTE_M and not pendingId and now - lastRequest > M.REROUTE_S then requestRoute(player) end
    local tg = nodes and routeTarget(e, nodes, i, t)
    if tg then
      -- set again only when the target moved on by a good stretch (each setPath re-plans the arrows)
      -- only forward (the farthest point on the graph flickered between two 150 m steps, every second a new path
      -- and the AI re-planning: Fells Point 2026-10-14), unless the old target is about reached
      local here = e.cum[i] + t * (e.cum[i + 1] - e.cum[i])
      local keep = targetS and type(routedTo) == 'string' and routedTo:sub(1, 1) == 'r'
        and tg.s < targetS + M.RETARGET_M and targetS > here + 2 * M.RETARGET_M
      local key = tg.final and 'final' or (keep and routedTo) or ('r' .. math.floor(tg.s / M.RETARGET_M))
      local wps = {}
      for _, w in ipairs(tg.via or {}) do wps[#wps + 1] = vec3(w.x, w.y, w.z) end
      wps[#wps + 1] = vec3(tg.x, tg.y, tg.z)
      if setArrows(key, wps, not tg.final and { e = e, s = tg.s, z = tg.z } or nil, nodes) then targetS = tg.s end
      return
    end
  end
  -- no planned route (yet, or offline), or none of it on the local graph: toward the destination
  local id, pos, nd = M.pickNode(nodes, tx, ty)
  if not id then return end
  local final = nd <= M.FINAL_M
  setArrows(final and 'final' or id, final and vec3(tx, ty, pos.z) or vec3(pos.x, pos.y, pos.z), nil, nodes)
end

-- The level ended: the arrows went with it (the destination and its route stay for the next level start).
function M.reset()
  routedTo, info, lastCheck, enu, progress, progressS = nil, nil, -math.huge, nil, 1, nil
end

return M
