-- GPS to a place picked on the world map (the user, 2026-10-14: "get the game's GPS system to work with this", then
-- "have the server plan out the whole route so it can work with much longer distances"). The destination is kept
-- as latitude / longitude (it outlives anchors and rebases). The server plans the whole road route (routing.py:
-- OSRM on OpenStreetMap); the map draws it, and the game's own route arrows (core_groundMarkers.setPath: arrows on
-- the road, the navigation app, the minimap's line) follow it on our AI road graph (navgraph.lua), which only
-- covers ~1 km around the player: the arrows go to the farthest point of the planned route ahead (up to LOOKAHEAD_M)
-- that the local graph reaches, set again as the player advances or the graph moves on. Leaving the route by more
-- than OFF_ROUTE_M plans it again from where the player is. Without a planned route (offline): steered toward the
-- destination through the graph node nearest it. Arrived (M.arrived: at the planned route's end, else within ARRIVE_M):
-- cleared.
-- Waypoints (M.setRoute, GitHub #935 / #886: a charging stop on the way, a route with several stops): the places the
-- route passes through before the destination, planned in one request (the server's routing.py, OSRM's intermediate
-- coordinates; at most MAX_VIA). Each is reached within ARRIVE_M of it, in order: a message, the API's
-- waypointReached event, and the re-routes from then on leave it out. The destination is not arrived at while a
-- waypoint is still ahead (a round trip ends where it starts).
local M = {}

M.MAX_VIA = 10          -- (server routing.MAX_VIA)

M.CHECK_S = 1.0
M.ARRIVE_M = 40         -- this close (horizontally): arrived
M.FINAL_M = 60          -- a graph node this close to the destination: the arrows end at the destination itself
M.ON_GRAPH_M = 25       -- route points may project this far onto a nearby graph edge
M.LOOKAHEAD_M = 1500    -- how far along the route the arrows may reach
M.SAMPLE_M = 40         -- route points tried for the arrows' target at least this far apart
M.RETARGET_M = 150      -- the arrows' target is moved on in steps of this much route
M.VIA_M = 250           -- the arrows' path is pinned to the route by a waypoint this often (else the stock planner takes
                        -- its own shortest way to the target: Fells Point 2026-10-14, a loop round a block)
M.OFF_ROUTE_M = 120     -- farther from the route: planned again ...
M.REROUTE_S = 20        -- ... at most this often
-- Driving back along the route (turned round on the same road: never OFF_ROUTE_M off it, GitHub #623): the route's
-- remaining distance grown by BACKTRACK_M over the least it was since the plan, moving faster than BACKTRACK_MPS:
-- planned again from where the car is, the way it now drives.
M.BACKTRACK_M = 200
M.BACKTRACK_MPS = 5
-- Arrived, on a planned route: its end within ROUTE_ARRIVE_M along it (GitHub #623: 40 m from the destination in a
-- straight line ended the guidance with 82 m and two turns of route still to go round a corner).
M.ROUTE_ARRIVE_M = 30
-- A route request that failed (offline for a moment, the routing service busy or timing out) is asked again after
-- ROUTE_RETRY_S, twice as long after each failure in a row up to ROUTE_RETRY_MAX_S: one failure left the GPS steering
-- in a straight line for the rest of the drive, "no road route: heading straight there" (GitHub #103, #38).
M.ROUTE_RETRY_S = 15
M.ROUTE_RETRY_MAX_S = 240
-- Past the local graph's edge the arrows go on along the planned route itself (the user, 2026-10-14, a Cannonball run:
-- "the in game arrows would keep ending and I'd have to wait a while before they continue": the graph only covers the
-- terrain block, which lags a fast car): the stock planner's path gets the route's next TAIL_M appended, a point every
-- TAIL_STEP_M on the ground below it.
M.TAIL_M = 2000
M.TAIL_STEP_M = 25
-- Autodrive (the user, 2026-10-14: "get the car to auto drive along a gps route"; also a repeatable drive for the
-- performance recordings): the player's car's own AI (vehicle ai.driveUsingPath) drives the graph part of the arrows'
-- path, in its lane, minding traffic, at the legal speed. Compatible updates extend the vehicle's current plan;
-- graph replacement and recovery need a fresh lane-aware start.
M.AUTO_STALL_S = 6
M.AUTO_NOPROGRESS_S = 30  -- the route's remaining distance not down by AUTO_PROGRESS_M in this long: the path given again
M.AUTO_PROGRESS_M = 20    -- (a car circling at 4 mph to reach a waypoint it could not, never standing still: GitHub #180)
M.AUTO_RECOVER_M = 150    -- no progress after the path was given again either: the car put on its own carriageway this far
                          -- ahead along the route (wedged against a curb at a junction at 3 mph, the same drive; 40 m put it
                          -- back before the same junction six times)
M.GRAPH_WAIT_S = 4      -- after this wait, hold and request the vehicle graph again (never submit a stale path)
M.AUTO_AGGRESSION = 0.3
-- Safety: the AI brakes to a stop while the world server is away (no new ground or roads: an autodrive left on through
-- server restarts drove off the loaded world and wrecked the car, 2026-10-14) and goes on when it is back; a car
-- damaged this much ends the autodrive.
M.AUTO_DAMAGE = 20000

local dest = nil        -- { lat, lon, name }
local vias = {}         -- { { lat, lon, name }... } the waypoints still ahead, in order (M.setRoute)
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
local autoRoadPath = nil -- the same path including its rear endpoint, for underfoot edge continuity
local autoFinished = nil -- completed local endpoint: wait for a continuation instead of resubmitting it
local autoSerial = nil  -- the graph (navgraph applies) they were taken from
local autoForce = true -- restart after a stall/recovery, destination change or pause, even on the same path
local autoBlocked = false
local stillSince = nil
local progRemaining, progAt = nil, nil   -- the remaining route distance last seen go down, and when (AUTO_NOPROGRESS_S)
local leastRemaining = nil  -- the least remaining route distance since the route came (BACKTRACK_M)
local routeFails = 0        -- route requests failed in a row (ROUTE_RETRY_S)
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
        segs[#segs + 1] = { a.x, a.y, o.pos.x, o.pos.y, (a.z + o.pos.z) / 2, way, a = id, b = other }
      end
    end
  end
  return segs
end

-- Pure: the distance from (px, py) to the nearest segment of segs, that segment's z, and {x, y, dx, dy}: the point on it
-- and its direction (the way it may be driven for a one-way edge). With a heading (hx, hy): only segments that may be
-- driven that way (a one-way edge against it is left out: the other carriageway of a divided road). Two-way
-- edges face along that heading, independently of their stored endpoint order.
-- Optional accept(point, distanceSquared) filters projections without discarding a farther eligible edge.
function M.nearestEdge(segs, px, py, hx, hy, accept)
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
      local fx, fy = dx, dy
      if (s[6] or 0) < 0 or ((s[6] or 0) == 0 and hx and dx * hx + dy * hy < 0) then fx, fy = -dx, -dy end
      local point = { x = qx + px, y = qy + py, dx = fx, dy = fy, segment = s, t = t }
      if not accept or accept(point, d) then bd, bz, bp = d, s[5], point end
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

local lineKey = nil      -- (setLineArrows: the route-line arrows' key; nil while the planner's arrows are up)
local function clearArrows()
  local gm = rawget(_G, 'core_groundMarkers')
  if gm and gm.setPath then pcall(gm.setPath, nil) end
  routedTo, lineKey = nil, nil
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
  local via = nil
  if #vias > 0 then
    via = {}
    for k, w in ipairs(vias) do via[k] = { w.lat, w.lon } end
  end
  if c.requestRoute(nextId, player.lat, player.lon, dest.lat, dest.lon, b, via) then
    pendingId, lastRequest = nextId, os.clock()
  end
end

function M.setDestination(lat, lon, name)
  lat, lon = tonumber(lat), tonumber(lon)
  if not lat or not lon then return false end
  dest = { lat = lat, lon = lon, name = name }
  autoFinished = nil
  vias = {}
  route, routeError, pendingId, enu, progress, info, progressS = nil, nil, nil, nil, 1, nil, nil
  leastRemaining, routeFails = nil, 0
  routedTo, lastCheck, lastRequest = nil, -math.huge, -math.huge     -- plan and route at the next update
  downloadProgress = nil         -- the previous destination's bar, not this one's (its own push is coming)
  autoForce = true
  return true
end

-- Pure: a list of places ({ lat, lon, name? } or { lat, lon }) as waypoints and the destination: the valid ones, in
-- order, the last the destination; nil and why when none, or more than MAX_VIA + 1.
function M.routeStops(points)
  if type(points) ~= 'table' then return nil, 'no points' end
  local out = {}
  for _, p in ipairs(points) do
    local lat, lon = tonumber(type(p) == 'table' and (p.lat or p[1])), tonumber(type(p) == 'table' and (p.lon or p[2]))
    if not lat or not lon or lat < -90 or lat > 90 or lon < -180 or lon > 180 then return nil, 'bad coordinates' end
    out[#out + 1] = { lat = lat, lon = lon, name = type(p.name) == 'string' and p.name or (type(p[3]) == 'string' and p[3]) or nil }
  end
  if #out == 0 then return nil, 'no points' end
  if #out > M.MAX_VIA + 1 then return nil, 'at most ' .. (M.MAX_VIA + 1) .. ' points' end
  return out
end

-- The GPS through waypoints: points as M.routeStops takes them, the last the destination. True, or false and why.
function M.setRoute(points)
  local stops, why = M.routeStops(points)
  if not stops then return false, why end
  local last = table.remove(stops)
  if not M.setDestination(last.lat, last.lon, last.name) then return false, 'bad coordinates' end
  vias = stops
  return true
end

-- The waypoints still ahead: { { lat, lon, name }... } (a copy).
function M.waypoints()
  local out = {}
  for k, w in ipairs(vias) do out[k] = { lat = w.lat, lon = w.lon, name = w.name } end
  return out
end

function M.clear()
  if auto then M.setAutodrive(false) end
  vias = {}
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
           routeDownload = downloadProgress, replans = M.noProgressReplans, recoveries = M.autoRecoveries,
           waypoints = #vias > 0 and M.waypoints() or nil, legs = route and route.legs or nil }
end

-- The planned route for the map: { version, points = {{lat, lon}...}, dist, dur, elev = {{distM, elevM}...} } or nil.
function M.route()
  if not route then return nil end
  return { version = route.version, points = route.points, dist = route.dist, dur = route.dur, elev = route.elev,
           legs = route.legs, steps = route.steps }
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

-- The arrows' target follows connected projections of the planned route within LOOKAHEAD_M and ON_GRAPH_M.
-- Also reports when the route is already loaded on a different road, so an unreachable join can retain recovery.
-- the graph's applies count: the game's node table stays the same table when a new graph goes in
local function graphSerial()
  local ok, ng = pcall(require, 'beamngEarth/navgraph')
  return ok and ng.applies and ng.applies() or 0
end

local segCache = { nodes = nil, serial = nil, segs = {} }

-- Directed distances on the local graph, shared by all of this update's route samples.
function M.routeLinks(segs)
  local links = {}
  for _, s in ipairs(segs) do
    links[s.a], links[s.b] = links[s.a] or {}, links[s.b] or {}
    local length = math.sqrt((s[3] - s[1])^2 + (s[4] - s[2])^2)
    if s[6] ~= -1 then links[s.a][s.b] = length end
    if s[6] ~= 1 then links[s.b][s.a] = length end
  end
  return links
end

-- Nearby projected samples need a nearby road connection too. Otherwise the next sample can jump onto a
-- service road at the highway's loaded end, sending the planner back through a distant junction to reach it.
function M.routeConnected(links, from, to, limit)
  local a, b = from.segment, to.segment
  local la = math.sqrt((a[3] - a[1])^2 + (a[4] - a[2])^2)
  local lb = math.sqrt((b[3] - b[1])^2 + (b[4] - b[2])^2)
  local delta = (to.t - from.t) * la
  if a == b and (a[6] == 0 or delta * a[6] >= 0) and math.abs(delta) <= limit then return true end
  local pending, distance = {}, {}
  local function add(id, d)
    if d <= limit and (not distance[id] or d < distance[id]) then distance[id], pending[id] = d, d end
  end
  if a[6] ~= 1 then add(a.a, from.t * la) end
  if a[6] ~= -1 then add(a.b, (1 - from.t) * la) end
  while next(pending) do
    local id, d = nil, math.huge
    for candidate, cost in pairs(pending) do if cost < d then id, d = candidate, cost end end
    pending[id] = nil
    if (id == b.a and b[6] ~= -1 and d + to.t * lb <= limit)
      or (id == b.b and b[6] ~= 1 and d + (1 - to.t) * lb <= limit) then return true end
    for other, length in pairs(links[id] or {}) do add(other, d + length) end
  end
  return false
end

-- A loaded edge beyond a graph gap can continue this road even though no path joins them yet.
-- Check both road tangents in 3D, so a parallel service road or a road below a bridge is not a continuation.
function M.loadedContinuation(from, to, nodes)
  local function ends(point)
    local s = point.segment
    local a, b = nodes[s.a], nodes[s.b]
    if (s[3] - s[1]) * point.dx + (s[4] - s[2]) * point.dy < 0 then a, b = b, a end
    local dx, dy, dz = b.pos.x - a.pos.x, b.pos.y - a.pos.y, b.pos.z - a.pos.z
    local length = math.sqrt(dx * dx + dy * dy + dz * dz)
    if length < 0.1 then return end
    return a.pos, b.pos, dx / length, dy / length, dz / length, math.max(a.radius or 0, b.radius or 0)
  end
  local _, finish, ax, ay, az, ar = ends(from)
  local begin, _, bx, by, bz, br = ends(to)
  if not ax or not bx then return false end
  local dx, dy, dz = begin.x - finish.x, begin.y - finish.y, begin.z - finish.z
  local hx, hy, hz = ax + bx, ay + by, az + bz
  local norm = hx * hx + hy * hy + hz * hz
  local forward = dx * hx + dy * hy + dz * hz
  local width = math.min(ar, br)
  -- Road width allows lateral offset, not another road's elevation. Allow at most two metres
  -- of height error after extrapolating the grade through the gap, even on a wide carriageway.
  return forward > 0 and norm > 0 and math.abs(dz - forward * hz / norm) <= 2
    and dx * dx + dy * dy + dz * dz - forward * forward / norm <= width * width
end

local function routeTarget(e, nodes, i, t)
  local serial = graphSerial()
  if segCache.nodes ~= nodes or segCache.serial ~= serial then
    segCache = { nodes = nodes, serial = serial, segs = M.edgeSegments(nodes) }
    segCache.links = M.routeLinks(segCache.segs)
  end
  local segs = segCache.segs
  local n = #e.x
  local s0 = e.cum[i] + t * (e.cum[i + 1] - e.cum[i])
  local hx, hy = e.x[i + 1] - e.x[i], e.y[i + 1] - e.y[i]
  local rx, ry = e.x[i] + t * hx, e.y[i] + t * hy
  local d0, _, previous = M.nearestEdge(segs, rx, ry, hx, hy)
  if d0 > M.ON_GRAPH_M then previous = nil end
  local differentRoadLoaded = false
  local continuationLoaded = false
  if auto then
    -- On-road, start on the car's actual edge, including elevation: the route line may be nearer a parallel road.
    local veh = playerVeh()
    local p, h = veh and veh:getPosition(), veh and veh:getDirectionVector()
    local a, b, routeA, routeB
    if p and h then
      a, b = M.edgeUnder(nodes, p.x, p.y, h.x, h.y, p.z, nil, autoRoadPath)
      local length = math.sqrt(hx * hx + hy * hy)
      if length > 0.1 then routeA, routeB = M.edgeUnder(nodes, rx, ry, hx / length, hy / length, p.z, M.ON_GRAPH_M) end
    end
    -- Match the route's heading and elevation too: an offset line near a junction or over a lower road is ambiguous.
    differentRoadLoaded = b and routeB and not ((a == routeA and b == routeB) or (a == routeB and b == routeA))
    for _, seg in ipairs(segs) do
      if b and ((seg.a == a and seg.b == b) or (seg.a == b and seg.b == a)) then
        _, _, previous = M.nearestEdge({ seg }, p.x, p.y)
        break
      end
    end
  end
  -- The first join starts at the car, which can be behind the router's snapped start.
  -- Accepted samples below reset previousS, so this allowance is only used for the initial join.
  local previousS = s0 - (auto and previous and math.sqrt((previous.x - rx)^2 + (previous.y - ry)^2) or 0)
  local start, startS, previousDistanceSq = previous, previousS, d0 * d0
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
    local function connected(bp, distanceSq)
      if distanceSq > M.ON_GRAPH_M^2 then return false end
      if not previous or M.routeConnected(segCache.links, previous, bp, s - previousS + 2 * M.ON_GRAPH_M) then return true end
      if M.loadedContinuation(previous, bp, nodes) then continuationLoaded = true end
      -- A simplified route can lie nearer an exit for one sample. Do not let that tentative
      -- projection prevent a closer continuation still reachable from the car before the fork.
      return distanceSq < previousDistanceSq and start
        and M.routeConnected(segCache.links, start, bp, s - startS + 2 * M.ON_GRAPH_M)
    end
    local d, z, bp = M.nearestEdge(segs, x, y, e.x[k + 1] - e.x[k], e.y[k + 1] - e.y[k], connected)
    local final = s >= e.cum[n] - 1e-6
    if d <= M.ON_GRAPH_M then
      -- Drop only waypoints on the abandoned branch; earlier pins must still guide the planner.
      while #via > 0 and not M.routeConnected(segCache.links, via[#via].projection, bp,
        s - via[#via].s + 2 * M.ON_GRAPH_M) do table.remove(via) end
      lastVia = via[#via] and via[#via].s or s0
      previous, previousS = bp, s
      previousDistanceSq = d * d
      -- the target / waypoint itself on that edge (not the route's point, which can lie nearer the other carriageway:
      -- the game's planner went for that one, the wrong way up a one-way road, and asked for a U-turn; GitHub #180)
      if bp and not final then x, y = bp.x, bp.y end
      best = { s = s, x = x, y = y, z = z, final = final, projection = bp }
      misses = 0
      if s - lastVia >= M.VIA_M then via[#via + 1] = best; lastVia = s end
    elseif previous then
      misses = misses + 1                -- the route left the graph (a short gap, a junction's offset: not yet)
      if misses >= 3 then break end
    end
    if final then break end
  end
  if best then
    if via[#via] == best then via[#via] = nil end
    best.via = via
  end
  return best, differentRoadLoaded or continuationLoaded
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

-- Restore only nodes the marker planner could merge into one point (within 1 m in 3D).
-- This is not a general route repair: real graph gaps must still be cut by validFrom.
local function unmergedNodes(from, to, centre, nodes, links)
  if not centre or not nodes[from] or not nodes[to] then return end
  local queue, parent, i = { from }, { [from] = false }, 1
  while i <= #queue do
    local id = queue[i]; i = i + 1
    for nextId, edge in pairs(links[id] or {}) do
      if parent[nextId] == nil and ((edge.drivability or 1) > 0)
        and (not edge.oneWay or edge.inNode == nil or edge.inNode == id) then
        local p = nodes[nextId].pos
        local dx, dy, dz = p.x - centre.x, p.y - centre.y, (p.z or 0) - (centre.z or 0)
        if nextId == to or dx * dx + dy * dy + dz * dz <= 1 then
          parent[nextId] = id
          if nextId == to then
            local out = {}
            while id ~= from do table.insert(out, 1, id); id = parent[id] end
            return out
          end
          queue[#queue + 1] = nextId
        end
      end
    end
  end
end

-- Pure: graph nodes in planner order, with consecutive duplicates removed. The stock planner can strip the
-- final wp when projecting a destination onto an edge; retain that edge's forward endpoint for the AI.
function M.pathNodes(path, nodes, edgeFrom, edgeTo)
  local out, links, previous = {}, nil, nil
  for _, p in ipairs(path or {}) do
    if p.wp ~= nil and out[#out] ~= p.wp then
      local from = out[#out]
      if nodes and from and not M.edge(nodes, from, p.wp) then
        if not links then
          links = {}
          for id in pairs(nodes) do links[id] = {} end
          for id, n in pairs(nodes) do
            for other, edge in pairs(n.links or {}) do
              if nodes[other] then
                local data = type(edge) == 'table' and edge or {}
                links[id][other], links[other][id] = data, data
              end
            end
          end
        end
        local centre = previous.pos or (nodes[from] and nodes[from].pos)
        for _, id in ipairs(unmergedNodes(from, p.wp, centre, nodes, links) or {}) do out[#out + 1] = id end
      end
      out[#out + 1], previous = p.wp, p
    end
  end
  local from, last = out[#out], path and path[#path]
  -- fixStartEnd clears a projected road node's wp but keeps its linkCount. Prefer that road point
  -- to the requested destination, which may sit beside the road or below it on a slope.
  for i = #(path or {}), 1, -1 do
    if path[i].wp ~= nil then break end
    if path[i].linkCount ~= nil then last = path[i]; break end
  end
  -- Inside the final edge the planner can project away both endpoint ids. Its two road points
  -- may still identify the forward part of the directed edge already confirmed beneath the car.
  local first = path and path[1]
  local anchored = not from and edgeFrom and edgeTo and first and first.pos
    and first.linkCount ~= nil and last and last.linkCount ~= nil
  if anchored then from = edgeFrom end
  local start = nodes and nodes[from]
  if start and last and not last.wp and last.pos then
    local a, p = start.pos, last.pos
    local best, errorSq = nil, 1 -- same 1 m positional tolerance as the stock planner's node merging
    for id, n in pairs(nodes) do
      local edge = M.edge(nodes, from, id)
      if (not anchored or id == edgeTo) and edge and (type(edge) ~= 'table' or ((edge.drivability or 1) > 0
        and (not edge.oneWay or edge.inNode == nil or edge.inNode == from))) then
        local dx, dy, dz = n.pos.x - a.x, n.pos.y - a.y, (n.pos.z or 0) - (a.z or 0)
        local lengthSq = dx * dx + dy * dy + dz * dz
        if lengthSq > 0 then
          local px, py, pz = p.x - a.x, p.y - a.y, (p.z or 0) - (a.z or 0)
          local t = (px * dx + py * dy + pz * dz) / lengthSq
          local ex, ey, ez = px - t * dx, py - t * dy, pz - t * dz
          local distanceSq = ex * ex + ey * ey + ez * ez
          local forward = true
          if anchored then
            local q = first.pos
            local qx, qy, qz = q.x - a.x, q.y - a.y, (q.z or 0) - (a.z or 0)
            local t0 = (qx * dx + qy * dy + qz * dz) / lengthSq
            forward = t0 >= 0 and t0 < t and (qx - t0 * dx)^2 + (qy - t0 * dy)^2 + (qz - t0 * dz)^2 < 1
          end
          if forward and t > 0 and t <= 1 and distanceSq < errorSq then best, errorSq = id, distanceSq end
        end
      end
    end
    if best then
      if anchored then out[1] = from end
      out[#out + 1] = best
    end
  end
  return out
end

-- Tests only (tools/autodrive_perf.py --ai-mph): drive at this speed (m/s) instead of the legal one.
M.speedOverride = nil

-- Pure: the vehicle Lua that sets its AI on a path of node ids (nil: off).
function M.aiCommand(ids, aggression, brake)
  local load = 'if not beamngEarth_autodrive then extensions.load("beamngEarth_autodrive") end; '
  if not ids then return load .. (brake and 'beamngEarth_autodrive.arrive()' or 'beamngEarth_autodrive.stop("disabled")') end
  local q = {}
  for i, id in ipairs(ids) do q[i] = string.format('%q', tostring(id)) end
  local speed = tonumber(M.speedOverride)
  return load .. 'local result = beamngEarth_autodrive.drive({' .. table.concat(q, ',') .. '}, '
    .. tostring(routeVersion) .. ', ' .. tostring(autoForce) .. ', '
    .. (speed and string.format('%.2f', speed) or 'nil') .. ', '
    .. string.format('%.2f', aggression or M.AUTO_AGGRESSION) .. '); '
    .. [[if result == 'waiting' then obj:queueGameEngineLua("require('beamngEarth/gps').retryAutodrive()") end]]
end

function M.retryAutodrive()
  routedTo, autoForce = nil, true
end

function M.onAiRouteDone(id)
  local veh = auto and playerVeh()
  if not veh or veh:getId() ~= id then return end
  autoFinished = autoPath and autoPath[#autoPath]
  -- Completion leaves the stock AI in manual mode with no active route. Force a waypoint start,
  -- including when a compatible update was already queued before this notification arrived.
  M.retryAutodrive()
  lastCheck = -math.huge
end

local function holdAutodrive(veh)
  if not autoBlocked then
    veh:queueLuaCommand('if beamngEarth_autodrive then beamngEarth_autodrive.stop() else ai.setMode("stop") end')
  end
  autoBlocked, autoForce, routedTo = true, true, nil
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

-- Pure: the nearest graph edge (from id, to id) running the car's way, within its local half-width
-- or the existing 8 m horizontal tolerance. Rank by distance including the edge's interpolated height when
-- pz is supplied, so an overlapping road above/below cannot win just because its centreline is closer in XY.
-- A supplied tolerance instead uses the inclusive search radius for approximate planned-route projections.
-- Where fork lanes overlap, retain an edge in the active path while the car is still inside its road width.
-- Outside that segment (including past either endpoint), fall back to the nearest eligible edge as usual.
function M.edgeUnder(nodes, px, py, hx, hy, pz, tolerance, path)
  local preferred = {}
  for i = 2, #(path or {}) do
    local a, b = path[i - 1], path[i]
    preferred[a] = preferred[a] or {}; preferred[b] = preferred[b] or {}
    preferred[a][b], preferred[b][a] = true, true
  end
  local best, bd, kept, kd = nil, math.huge, nil, math.huge
  local function try(from, to, e)
    local a, b = nodes[from] and nodes[from].pos, nodes[to] and nodes[to].pos
    if not (a and b) or not drivable(e, from) then return end
    local dx, dy = b.x - a.x, b.y - a.y
    local L = math.sqrt(dx * dx + dy * dy)
    if L > 0.1 and (dx * hx + dy * hy) / L > 0.5 then
      local along = ((px - a.x) * dx + (py - a.y) * dy) / (L * L)
      local t = math.max(0, math.min(1, along))
      local qx, qy = a.x + t * dx - px, a.y + t * dy - py
      local d = math.sqrt(qx * qx + qy * qy)
      local ra, rb = nodes[from].radius or 0, nodes[to].radius or 0
      if type(e) == 'table' then
        -- Edge-specific radii avoid borrowing the width of a wider road at a shared junction.
        local rIn, rOut = e.inRadius, e.outRadius
        if e.inNode == to then rIn, rOut = rOut, rIn end
        ra, rb = rIn or ra, rOut or rb
      end
      local reach = tolerance or math.max(8, ra + t * (rb - ra))
      local dz = pz and a.z and b.z and (a.z + t * (b.z - a.z) - pz) or 0
      local distanceSq = d * d + dz * dz
      if (d < reach or (tolerance and d == reach)) and distanceSq < bd then best, bd = { from, to }, distanceSq end
      if preferred[from] and preferred[from][to] and along >= 0 and along <= 1 and d < ra + t * (rb - ra)
        and math.abs(dz) <= 2 and distanceSq < kd then kept, kd = { from, to }, distanceSq end
    end
  end
  for id, n in pairs(nodes or {}) do
    for other, e in pairs(n.links or {}) do
      try(id, other, e)
      try(other, id, e)
    end
  end
  best = kept or best
  if best then return best[1], best[2] end
end

-- Pure: join through the forward endpoint b of the edge a -> b beneath the car. BeamNG's manual-path AI
-- drives toward path[1] from the car's position; a is behind the car, not an initial segment hint.
-- If the planner starts beyond the junction, keep b so the AI does not cut across the corner.
function M.joinStart(ids, a, b, nodes)
  if not a or not b or #ids == 0 or ids[1] == b then return ids end
  local out, first = {}, 1
  if ids[1] == a then
    if ids[2] ~= b then return ids end -- the planner takes another direction: do not invent a detour
    first = 2
  else
    local e = nodes and M.edge(nodes, b, ids[1])
    if not e or not drivable(e, b) then return ids end
    for _, id in ipairs(ids) do
      if id == a or id == b then return ids end -- do not introduce a loop through a later route node
    end
    out[1] = b
  end
  for i = first, #ids do out[#out + 1] = ids[i] end
  return out
end

-- Only hand off paths connected to the directed segment under the car. A nearby parallel road is not a valid
-- starting point. No segment (off-road, wrong-way, or graph not loaded) means hold and try the planner again.
function M.connectedStart(ids, a, b, nodes)
  if not a or not b then return nil end
  local joined = M.joinStart(ids, a, b, nodes)
  if joined[1] ~= b then return nil end
  -- joinStart guards against added loops; revisiting these nodes in the planned route is valid.
  return joined
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
  local veh = auto and playerVeh()
  if veh and veh:getId() == id then
    -- The stock AI discards its route on a new mapData object. Even an unchanged set of node ids needs a fresh
    -- lane-aware start. Schedule it immediately; the vehicle helper also checks its actual map identity.
    routedTo, lastCheck = nil, -math.huge
  end
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
    if now - pathWait >= M.GRAPH_WAIT_S then
      M.graphWaitsTimedOut = (M.graphWaitsTimedOut or 0) + 1
      holdAutodrive(veh)
      pathWait = now
      veh:queueLuaCommand('mapmgr.requestMap()')
    end
    routedTo = nil
    return -- never give a new-graph route to an old vehicle graph, even after the timeout
  end
  pathWait = nil
  local p, d = veh:getPosition(), veh:getDirectionVector()
  local hl = math.sqrt(d.x * d.x + d.y * d.y)
  local a, b
  if hl > 0.1 then
    -- A call before another argument yields only its first result in Lua: keep both edge endpoints.
    a, b = M.edgeUnder(nodes, p.x, p.y, d.x / hl, d.y / hl, p.z, nil, autoRoadPath)
  end
  local ids = M.pathNodes(gm.routePlanner.path, nodes, a, b)
  -- the planner's path can be older than the graph (planned before the last apply): a step that is no edge now
  -- disabled the AI's Lua (ai.lua getEdgeLaneConfig, Lauterbrunnen 2026-10-15). The valid part is driven and the
  -- route planned again at the next check
  local valid = M.validFrom(ids, 1, { nodes })
  if #valid < #ids then
    ids, routedTo = valid, nil
    M.pathCuts = (M.pathCuts or 0) + 1
  end
  ids = M.connectedStart(ids, a, b, nodes)
  if not ids or #ids == 0 then holdAutodrive(veh); return end
  if #ids == 1 and ids[1] == autoFinished then
    local n = nodes[ids[1]]
    local dx, dy, dz = p.x - n.pos.x, p.y - n.pos.y, p.z - (n.pos.z or p.z)
    local reach = math.max(8, n.radius or 0)
    -- A late completion may follow an extension to a still-distant endpoint. Only hold at the reached one;
    -- a replacement map alone does not make that endpoint a usable continuation.
    if dx * dx + dy * dy + dz * dz <= reach * reach then holdAutodrive(veh); return end
  end
  veh:queueLuaCommand(M.aiCommand(ids))
  M.trace('path', ids, nodes, p)
  autoPath, autoSerial, stillSince = ids, graphSerial(), nil
  autoRoadPath = { a } -- the rear node is context only; the vehicle still drives toward b first
  for _, id in ipairs(ids) do autoRoadPath[#autoRoadPath + 1] = id end
  autoForce, autoBlocked = false, false
  -- A continuation gets its own progress window; waiting at the old endpoint was not a failed attempt.
  if autoFinished then progRemaining, progAt, stuckTries = nil, nil, 0 end
  autoFinished = nil
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
      local e = M.edge(nodes, ids[i], ids[i + 1])
      if not e or not drivable(e, ids[i]) then return out end
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

-- Do not reset the AI on its old map just before the new one arrives. An intact path can finish the handoff;
-- a cut path must stop until a connected replacement is available. The map acknowledgement schedules that start.
function M.afterGraph(keep, newNodes, moved)
  local veh = playerVeh()
  if not keep or not veh or not newNodes then return end
  if moved then M.graphMoves = (M.graphMoves or 0) + 1 end
  local ids = M.validFrom(keep.ids, 1, { newNodes })
  if #ids == keep.full and #ids == #keep.ids then
    M.graphUntouched = (M.graphUntouched or 0) + 1
  else
    holdAutodrive(veh)
    M.graphKeeps = (M.graphKeeps or 0) + 1
  end
  autoSerial, routedTo, lastCheck = nil, nil, -math.huge
end

-- At speed the arrows come straight from the planned route's line (LINE_*), not the game's planner on our AI graph:
-- the graph is clipped to the terrain block and lags behind a fast car, so the planner found no road ahead (the
-- arrows pointed at the destination across the land) or re-planned on a stale graph, and they stopped following
-- for a while (the user, 2026-11-09: "if your going fast enough the gps routing can go crazy or stop updating"). The
-- path is a dense line along the route (heights from the ground below), set again every LINE_REBUILD_M; the planner
-- only trims it as the car passes. Also used whenever none of the route ahead is on the local graph. Not with
-- autodrive (the AI drives the graph's path).
M.LINE_MPS = 25         -- (56 mph)
M.LINE_M = 1500         -- m of route ahead
M.LINE_STEP_M = 15
M.LINE_REBUILD_M = 120
local function setLineArrows(e, s0, z0)
  local gm = rawget(_G, 'core_groundMarkers')
  if not gm or not gm.setPath or not gm.routePlanner then return false end
  local key = 'L' .. tostring(route and route.version) .. ':' .. math.floor(s0 / M.LINE_REBUILD_M)
  if key == lineKey and gm.endWP then return true end
  local pts = M.routeSlice(e, s0, s0 + M.LINE_M, M.LINE_STEP_M)
  if #pts < 2 then return false end
  local ray = rawget(_G, 'castRayStatic')
  local z, path = z0 or 0, {}
  for j = 1, #pts do
    local x, y = pts[j][1], pts[j][2]
    if ray then
      local ok, d = pcall(ray, vec3(x, y, z + 60), vec3(0, 0, -1), 200)
      if ok and d and d < 200 then z = z + 60 - d end
    end
    path[j] = vec3(x, y, z)
  end
  if not gm.endWP or not lineKey then         -- (the markers' state set up once, to a near point: cheap to plan)
    if not pcall(gm.setPath, path[2], { clearPathOnReachingTarget = false }) then return false end
  end
  local rp = gm.routePlanner
  if rp.clear then pcall(rp.clear, rp) end
  rp.path = rp.path or {}
  for j = 1, #path do rp.path[j] = { pos = path[j] } end
  for j = #rp.path, #path + 1, -1 do rp.path[j] = nil end
  if rp.calcDistance then pcall(rp.calcDistance, rp) end
  local total = e.cum[#e.x]
  M.addRest(rp, total - math.min(s0 + M.LINE_M, total))
  gm.endWP = { path[#path] }
  if gm.sendToApp then pcall(gm.sendToApp) end        -- (the minimap's and the navigation app's route line)
  lineKey, routedTo = key, nil
  M.lineSets = (M.lineSets or 0) + 1
  return true
end

local arrowsSerial = nil
-- target: a vec3, or a list of them (waypoints along the route, then the target)
local function setArrows(key, target, tail, nodes)
  local serial = graphSerial()
  if key == routedTo and serial == arrowsSerial then return end
  local gm = rawget(_G, 'core_groundMarkers')
  if not gm or not gm.setPath then return end
  if auto and route and not paused then
    local veh = playerVeh()
    local p, d = veh and veh:getPosition(), veh and veh:getDirectionVector()
    local hl = d and math.sqrt(d.x * d.x + d.y * d.y) or 0
    local a, b
    if hl > 0.1 then a, b = M.edgeUnder(nodes, p.x, p.y, d.x / hl, d.y / hl, p.z, nil, autoRoadPath) end
    if not b then if veh then holdAutodrive(veh) end; return end
    -- Pin the planner to the forward junction before the distant route waypoints. Otherwise it can choose a
    -- parallel road as its start, even though that road cannot be reached from the vehicle's current segment.
    local anchored = { b }
    if type(target) == 'table' and target[1] then
      for _, wp in ipairs(target) do anchored[#anchored + 1] = wp end
    else anchored[#anchored + 1] = target end
    target = anchored
  end
  if pcall(gm.setPath, target, { clearPathOnReachingTarget = false }) then
    routedTo, arrowsSerial = key, serial
    -- (not before the route is in: the AI would set off on its own way toward the destination)
    if auto and route ~= nil and not paused then pcall(driveArrowsPath, gm, nodes) end    -- before the tail: the AI drives the graph part only
    if tail then pcall(appendTail, gm, tail.e, tail.s, tail.z) end
    return true
  end
end

-- brake is used only for arrival; an ordinary toggle hands control straight back to the player.
function M.setAutodrive(on, brake)
  on = on and true or false
  if on == auto then return auto end
  auto = on
  autoFinished = nil
  autoPath, autoRoadPath, autoSerial, stillSince, paused, pathWait = nil, nil, nil, nil, false, nil
  autoForce, autoBlocked = true, false
  progRemaining, progAt, stuckTries = nil, nil, 0
  if on then
    routedTo = nil                       -- the arrows (and the AI's path) set again at the next check
    lastCheck = -math.huge
  else
    local veh = playerVeh()
    if veh then veh:queueLuaCommand(M.aiCommand(nil, nil, brake)) end
  end
  return auto
end
function M.autodrive() return auto end

local function approach(distance)
  if not auto or paused then return end
  local veh = playerVeh()
  if veh then veh:queueLuaCommand(string.format(
    'if beamngEarth_autodrive then beamngEarth_autodrive.approach(%.3f) end', distance)) end
end

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
    autoForce = true
  end
  local v = veh.getVelocity and veh:getVelocity()
  local speed = v and math.sqrt(v.x * v.x + v.y * v.y) or 0
  if speed < 1 then stillSince = stillSince or now else stillSince = nil end
  if pathWait and M.vehicleHasGraph(veh) then routedTo = nil end     -- the path that waited: given now
  if (autoPath and autoSerial ~= graphSerial()) or (stillSince and now - stillSince > M.AUTO_STALL_S) then
    routedTo = nil
  end
  if autoBlocked then routedTo = nil end
  if stillSince and now - stillSince > M.AUTO_STALL_S then autoForce = true end
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

-- Ask for the route (again)? route: the planned one, false (the last request failed) or nil (none asked yet); since:
-- s since the last request; fails: requests failed in a row. Pure.
function M.retryRoute(route, since, fails)
  if route == nil then return true end
  if route ~= false then return false end
  return since >= math.min(M.ROUTE_RETRY_MAX_S, M.ROUTE_RETRY_S * 2 ^ math.max(0, (fails or 1) - 1))
end

-- Arrived? dist: straight to the destination; left: the planned route still ahead along it (nil: no route); off: the
-- car's distance from the route. On a route: its end reached (within ROUTE_ARRIVE_M along it, on it), or the
-- destination reached away from the route (driven to across a car park). Pure.
function M.arrived(dist, left, off)
  if left == nil then return dist < M.ARRIVE_M end
  if left < M.ROUTE_ARRIVE_M and off < M.ARRIVE_M then return true end
  return dist < M.ARRIVE_M and off > M.ARRIVE_M
end

-- Driving back along the route? least: the least remaining distance seen (nil: none yet), remaining: now, speed: the
-- car's (m/s). -> backtracking, the least remaining to keep. Pure.
function M.backtracking(least, remaining, speed)
  if not least or remaining < least then return false, remaining end
  return remaining - least > M.BACKTRACK_M and speed > M.BACKTRACK_MPS, least
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
        route = { points = r.points, dist = r.dist, dur = r.dur, version = routeVersion, elev = r.elev, legs = r.legs,
          steps = r.steps }
        routeError, enu, routedTo, leastRemaining, routeFails = nil, nil, nil, nil, 0
        autoForce = true
      else
        routeError = r.error or 'no route'
        routeFails = routeFails + 1
        if not route then route = false end
      end
    elseif now - lastRequest > 30 then
      pendingId = nil                      -- no answer: try again below
    end
  elseif M.retryRoute(route, now - lastRequest, routeFails) then
    requestRoute(player)
  end

  -- the next waypoint reached: passed on (the route through it stays; re-routes leave it out from now on)
  while vias[1] do
    local wx, wy = anchor:geodeticToEnu(vias[1].lat, vias[1].lon, 0)
    if math.sqrt((wx - player.x) ^ 2 + (wy - player.y) ^ 2) >= M.ARRIVE_M then break end
    local w = table.remove(vias, 1)
    if ui_message then pcall(ui_message, 'Waypoint: ' .. (w.name or 'reached'), 5, 'beamngEarthGps') end
    local api = rawget(_G, 'beamngEarth_api')
    if api and api._event then
      pcall(api._event, 'waypointReached', { lat = w.lat, lon = w.lon, name = w.name, left = #vias })
    end
  end
  local tx, ty = anchor:geodeticToEnu(dest.lat, dest.lon, 0)
  local dx, dy = tx - player.x, ty - player.y
  local dist = math.sqrt(dx * dx + dy * dy)
  info = { dist = dist, bearing = M.bearing(player.x, player.y, tx, ty), remaining = dist }
  local ahead = #vias > 0             -- (a waypoint still ahead: not arrived, steered toward it without a route)
  if ahead then tx, ty = anchor:geodeticToEnu(vias[1].lat, vias[1].lon, 0) end
  local function arrive()
    if ui_message then pcall(ui_message, 'Arrived: ' .. (dest.name or 'destination'), 5, 'beamngEarthGps') end
    local api = rawget(_G, 'beamngEarth_api')      -- (the modding API's event: other mods' jobs end here)
    if api and api._event then pcall(api._event, 'routeArrived', { lat = dest.lat, lon = dest.lon, name = dest.name }) end
    -- Finish with the stock brakes and parking brake rather than releasing the controls and coasting.
    -- Manual cancellation still releases control immediately; a local path ending is not arrival.
    if auto then M.setAutodrive(false, true) end
    M.clear()
  end
  if not route and not ahead and dist < M.ARRIVE_M then arrive() return end
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
    if not ahead and M.arrived(dist, remaining, off) then arrive() return end
    local speed = math.sqrt(vx * vx + vy * vy)
    local back
    back, leastRemaining = M.backtracking(leastRemaining, info.remaining, speed)
    if back and not pendingId and now - lastRequest > M.REROUTE_S then
      leastRemaining = nil
      requestRoute(player)
    end
    if route.dist and route.dist > 0 and route.dur then info.eta = route.dur * remaining / route.dist end
    if off > M.OFF_ROUTE_M and not pendingId and now - lastRequest > M.REROUTE_S then requestRoute(player) end
    local tg
    if auto then
      local differentRoadLoaded
      if nodes then tg, differentRoadLoaded = routeTarget(e, nodes, i, t) end
      if not tg then
        local veh = playerVeh()
        if veh then holdAutodrive(veh) end
        -- A graph cut needs time to load. On another road beside an already loaded route, retain recovery.
        if not differentRoadLoaded then
          progRemaining, progAt, stuckTries = nil, nil, 0
          return
        end
      end
    end
    if auto and not paused then
      if not progRemaining or info.remaining < progRemaining - M.AUTO_PROGRESS_M then
        if progRemaining then stuckTries = 0 end
        progRemaining, progAt = info.remaining, now
      -- A completed path waits for streaming, but an unreachable loaded continuation still needs recovery.
      elseif (not autoFinished or not tg) and now - progAt > M.AUTO_NOPROGRESS_S then
        stuckTries = stuckTries + 1
        if stuckTries >= 2 then         -- the path given again did not help: onto the route ahead
          stuckTries = 0
          if nodes and recoverAhead(e, nodes, i, t) then M.autoRecoveries = (M.autoRecoveries or 0) + 1 end
        end
        routedTo, progRemaining = nil, nil          -- planned and given again at this check
        autoForce = true
        M.noProgressReplans = (M.noProgressReplans or 0) + 1
      end
    end
    if auto and not tg then return end -- recovery may retry; never drive a destination-nearest fallback
    local hereS = e.cum[i] + t * (e.cum[i + 1] - e.cum[i])
    if not auto and speed >= M.LINE_MPS and setLineArrows(e, hereS, player.z) then return end
    tg = tg or (nodes and routeTarget(e, nodes, i, t))
    if not tg and not auto and setLineArrows(e, hereS, player.z) then return end
    lineKey = nil                       -- (back on the planner's arrows: set again by setArrows)
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
      if not ahead and tg.final then approach(remaining) end
      return
    end
  end
  -- no planned route (yet, or offline), or none of it on the local graph: toward the destination
  local id, pos, nd = M.pickNode(nodes, tx, ty)
  if not id then return end
  local final = nd <= M.FINAL_M
  setArrows(final and 'final' or id, final and vec3(tx, ty, pos.z) or vec3(pos.x, pos.y, pos.z), nil, nodes)
  if not ahead and final then approach(dist) end
end

-- The level ended: the arrows went with it (the destination and its route stay for the next level start).
function M.reset()
  routedTo, info, lastCheck, enu, progress, progressS = nil, nil, -math.huge, nil, 1, nil
  lineKey = nil
  autoPath, autoRoadPath, autoSerial, pathWait, vehGraph = nil, nil, nil, nil, {}
  autoForce, autoBlocked = true, false
  autoFinished = nil
end

return M
