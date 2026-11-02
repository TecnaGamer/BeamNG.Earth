-- The AI road graph (server/navgraph.py, --traffic on) handed to the game's own navgraph with
-- map.reset(customNodes) (docs/research/2026-10-10-navgraph-traffic-signals.md §2.4-2.8, the plan in §8): BeamNG's
-- traffic and AI then drive our streamed roads. The server sends a graph around the player every ~400 m; this
-- module clips it to the terrain block (the only collidable ground, minus EDGE_M) and applies it whenever a new
-- graph arrives or the block moved, at most every MIN_GAP_S.
--
-- The custom map path's pitfalls (research §2.7), each handled here: vehicles are told to fetch the new graph
-- (mapmgr.requestMap); the node table object stays the same one across resets (gameplay_traffic caches it); every
-- edge carries its speed limit and lanes; the road rules come from a file of ours (the level's info.json has none);
-- a reset by someone else (map.reset() with no graph rebuilds an empty one from our level) is undone next frame.
local M = {}

M.EDGE_M = 60           -- graph nodes this far inside the block's ground box only
M.MIN_GAP_S = 1.0
-- An apply (or its signals' second step) waits for a light frame (governor roomFor: at most APPLY_ROOM_MS of streaming
-- and other work spent in it, not a terrain commit's), APPLY_WAIT_FRAMES at most
M.APPLY_ROOM_MS = 4.0
M.APPLY_WAIT_FRAMES = 30
-- The block moved this far from where the graph was last clipped: clipped again to the new block (the server's graph
-- reaches WINDOW_M = 1100 m, the block ~450 m; without it the graph ran out ahead of a car driving on before the next
-- graph came, every 400 m: the GPS arrows ended and the autodrive stopped, the user's Cannonball run 2026-10-14)
M.RECLIP_M = 150
M.UI_MAP = false        -- send our graph to the UI (the big map) on every apply
M.RULES_FILE = '/temp/beamngEarth/roadRules.json'
M.rules = { rightHandDrive = false, turnOnRed = true }    -- US; `rightHandDrive = true` means driving on the left
M.enabled = true
-- Traffic (gameplay_traffic, the player's traffic amount): started once the first graph is in the map; a car
-- within GUARD_M of the block's edge (or past it, where there is no ground) is respawned on the graph
-- (gameplay_traffic.forceTeleport), checked every GUARD_S; after a teleport every car is, once the new
-- place's graph is in.
M.GUARD_M = 40
M.GUARD_S = 0.5
-- A car standing still this long (longer than a signal cycle), this far from the player, is respawned too: stock
-- traffic respawns only cars far away and out of sight, so one wedged on a corner the map's road cuts through a
-- building, or queued behind the player's parked car in a one-lane street, stood for good (Frederick, Fells Point
-- 2026-10-14)
M.STUCK_S = 45
M.STUCK_MS = 0.5
M.STUCK_FAR_M = 40
M.RESPAWN_M = { 60, 300 }   -- a respawned car goes this far (min, max) behind the camera
-- A car heading for a graph node whose road the clip cut off (within 45 degrees, CUT_M or CUT_S of driving away) is
-- respawned too: to the AI the cut is a dead end, and a car spawned facing one made a three-point turn on the spot
-- (GitHub #72, "spawns the wrong way and tries to turn around immediately"; Bathurst 2026-10-21: 20 m from a cut)
-- A car that has just appeared (FRESH_S) turns round for a cut much farther ahead (Bathurst: 89 m): CUT_FRESH_M
-- (250 m sent cars respawned near a cut straight back again: traffic popping in and out, PR #493, OmdevShastri).
-- A car respawned for a cut or for standing still is not respawned for those again within COOLDOWN_S (one that
-- leaves the block's guard still is at once).
M.CUT_M = 60
M.CUT_S = 4
M.CUT_FRESH_M = 95
M.FRESH_S = 5
M.COOLDOWN_S = 8.0
M.JUMP_M = 30           -- a car this much farther than at the last check was respawned (stock or ours)
-- At speed a car respawned behind the camera is never seen again: on a motorway every car ahead reached the block's
-- edge within ~30 s and reappeared behind the player, who drove alone (GitHub #353, Albany to New York). Faster than
-- AHEAD_MS, a respawned car goes ahead along the player's way instead, AHEAD_M (min, max) on, short of the block's
-- edge guard (a car popping in 100+ m ahead at motorway speed is hardly seen; in town it still goes behind).
M.AHEAD_MS = 20         -- (45 mph)
M.AHEAD_M = { 100, 200 }
-- (gameplay_walk: 0.40's traffic.lua calls gameplay_walk.addVehicleToBlacklist when it activates its cars, and this
-- level had none loaded: the cars spawned and never drove, 2026-10-19)
M.EXTENSIONS = { 'core_multiSpawn', 'gameplay_traffic', 'gameplay_walk' }

local graph = nil       -- the server's last graph {anchor, nodes, edges}
local applied = nil     -- {anchor, serial, box} of the graph in the game's map now
local dirty = false
local lastApply = -math.huge
local persistent = nil  -- the node table handed out once (research §2.7.2)
local ours = false
local foreign = false
local urgent = false      -- a rebase moved the graph: applied again without waiting for MIN_GAP_S (M.transform)
local stats = { applies = 0, lastMs = 0, nodes = 0, edges = 0 }
local pendingSignals = nil -- the clipped nodes whose signals go in next frame
local started = false
local respawnAll = false
local lastGuard = -math.huge
local cuts = {}         -- { x, y } of the applied graph's nodes that lost a road to the clip

-- Pure: point inside box {x0, y0, x1, y1} shrunk by m (no box: anywhere).
function M.inside(b, x, y, m)
  return not b or (x >= b[1] + m and x <= b[3] - m and y >= b[2] + m and y <= b[4] - m)
end

-- Pure: the lanes string of an edge (one char per lane, '+' along in -> out, '-' against; research §2.2), the
-- lanes against the direction first for right-hand traffic.
function M.lanes(out, back, leftHand)
  local plus, minus = string.rep('+', out), string.rep('-', back)
  return leftHand and (plus .. minus) or (minus .. plus)
end

-- A link steeper than this (rise over run) is left out of the graph: no car drives it, and the AI given a path over
-- one circled under it for ever (GitHub #131: Baltimore's East Eager Street, a bridge whose first node stood 6.4 m
-- over its junction with Greenmount Avenue 8 m away; the steepest real streets are ~0.37, San Francisco's).
M.MAX_GRADE = 0.45

-- Pure: is the link between graph nodes p and q ({x, y, z}) too steep to drive (MAX_GRADE)?
function M.tooSteep(p, q)
  local run = math.sqrt((p[1] - q[1]) ^ 2 + (p[2] - q[2]) ^ 2)
  return math.abs(p[3] - q[3]) > M.MAX_GRADE * math.max(run, 1.0)
end

-- Pure-ish (needs vec3): the stock map's node table of graph g clipped to box b: {name = {pos, radius,
-- links = {other = edge}}}, every edge shared by both ends (double-sided) with in/out nodes, positions and radii.
-- Returns nodes, node count, edge count and the cuts: { x, y } of each kept node with a road to a node left out.
function M.toNodes(g, b, edgeM, leftHand, yieldEvery)
  local nodes, nn, ne = {}, 0, 0
  local keep = {}
  local steps = 0
  local function step()       -- (in a coroutine: a pause every yieldEvery items, buildNodes)
    if yieldEvery then
      steps = steps + 1
      if steps % yieldEvery == 0 then coroutine.yield() end
    end
  end
  for name, n in pairs(g.nodes) do
    if M.inside(b, n[1], n[2], edgeM) then keep[name] = n end
    step()
  end
  local function node(name)
    local t = nodes[name]
    if not t then
      local n = keep[name]
      t = { pos = vec3(n[1], n[2], n[3]), radius = n[4], links = {} }
      nodes[name] = t
      nn = nn + 1
    end
    return t
  end
  local cut, cutList = {}, {}
  for _, e in ipairs(g.edges) do
    step()
    local a, bb = e[1], e[2]
    for _, pair in ipairs({ { a, bb }, { bb, a } }) do
      local k = pair[1]
      if keep[k] and not keep[pair[2]] and g.nodes[pair[2]] and not cut[k] then
        cut[k] = true
        cutList[#cutList + 1] = { keep[k][1], keep[k][2] }
      end
    end
    if keep[a] and keep[bb] and a ~= bb and not M.tooSteep(keep[a], keep[bb]) then
      local A, B = node(a), node(bb)
      if not A.links[bb] then
        local edge = { drivability = e[7], oneWay = e[3] == 1, lanes = M.lanes(e[4], e[5], leftHand),
          speedLimit = e[6], inNode = a, outNode = bb, inPos = A.pos, outPos = B.pos,
          inRadius = e[8], outRadius = e[8] }
        A.links[bb] = edge
        B.links[a] = edge
        ne = ne + 1
      end
    end
  end
  return nodes, nn, ne, cutList
end

-- Pure: is a car at (px, py) moving along unit (dx, dy) heading for one of the cuts within reach m (45 degrees)?
function M.headingToCut(px, py, dx, dy, list, reach)
  for _, c in ipairs(list) do
    local vx, vy = c[1] - px, c[2] - py
    local d = math.sqrt(vx * vx + vy * vy)
    if d <= reach and (d < 1 or (vx * dx + vy * dy) >= 0.7071 * d) then return true end
  end
  return false
end

function M.onMessage(msg, currentAnchor)
  if not M.enabled or msg.anchorId ~= currentAnchor then return false end
  local ok, g = pcall(jsonDecode, readFile(msg.path) or '')
  if not ok or type(g) ~= 'table' or type(g.nodes) ~= 'table' or type(g.edges) ~= 'table' then return false end
  graph = { anchor = msg.anchorId, serial = msg.serial, nodes = g.nodes, edges = g.edges,
            signals = type(g.signals) == 'table' and g.signals or {}, rules = type(g.rules) == 'table' and g.rules or nil }
  dirty = true
  return true
end

-- Traffic signals and stop signs (core_trafficSignals, research §5): the server's controlled junctions whose
-- node and approach nodes are in the clipped graph. A signalled junction is one sequence of two phases (its
-- approaches split by axis, US lightsBasic timings from the approach speed), a stop junction shares one signStop
-- controller; an instance's pos is the stop point on its approach's right half, dir the travel direction.
M.SIGNAL_TYPE = 'lightsBasic'
-- the switchable head (the stock boom, instanced_trafficlight material): one TSStatic per approach where the
-- road tile's static boom stands (trees.lua's signal group is hidden while these exist)
M.SIGNAL_SHAPE = '/art/shapes/objects/s_trafficlight_boom_sn.dae'
local heads = {}        -- instance name -> {id, x, y}: the heads in the scene (kept across applies)
local seenHeads = {}

local function clearHeads(keep)
  for name, h in pairs(heads) do
    if not (keep and keep[name]) then
      local o = scenetree.findObjectById(h.id)
      if o then o:delete() end
      heads[name] = nil
    end
  end
end

-- A head for instance inst at a's prop transform: the one already standing there is kept (it is linked by
-- the instance's name, which stays the same: setupSignals finds it again), so a new graph costs only the new ones.
local function makeHead(inst, a)
  if not a[8] or not inst.createSignalObject then return end
  seenHeads[inst.name] = true
  local h = heads[inst.name]
  if h and math.abs(h.x - a[8]) < 0.05 and math.abs(h.y - a[9]) < 0.05 and scenetree.findObjectById(h.id) then return end
  if h then
    local old = scenetree.findObjectById(h.id)
    if old then old:delete() end
  end
  local o = inst:createSignalObject(M.SIGNAL_SHAPE, vec3(a[8], a[9], a[10]), quat(0, 0, 0, 1))
  if not o then return end
  local m = MatrixF(true)
  m:setFromEuler(vec3(0, 0, -math.rad(a[11] or 0)))     -- as trees.lua turns the static boom (negated: clockwise)
  m:setPosition(vec3(a[8], a[9], a[10]))
  o:setTransform(m)
  heads[inst.name] = { id = o:getID(), x = a[8], y = a[9] }
end

local function signalData(nodes)
  local ts = core_trafficSignals
  local inst, ctrls, seqs = {}, {}, {}
  local stop = nil
  local ns = 0
  for _, j in ipairs(graph.signals or {}) do
    local jn = nodes[j.j]
    if jn and type(j.app) == 'table' then
      local apps = {}
      for _, a in ipairs(j.app) do if nodes[a[1]] then apps[#apps + 1] = a end end
      if #apps > 0 then
        ns = ns + 1
        local cs = {}
        if j.kind == 'signal' then
          local speed = 13
          for _, l in pairs(jn.links) do speed = math.max(speed, l.speedLimit or 0) end
          for ph = 0, 1 do
            local c = ts.newController({ name = 'bne_' .. j.j .. '_' .. ph })
            c:applyDefinition(M.SIGNAL_TYPE)
            if c.autoSetTimings then c:autoSetTimings(speed, 2 * jn.radius + 6) end
            ctrls[#ctrls + 1] = c
            cs[ph] = c
          end
          local seq = ts.newSequence({ name = 'bne_' .. j.j, phases = { { controllerIds = { cs[0].id } },
            { controllerIds = { cs[1].id } } } })
          seqs[#seqs + 1] = seq
          for i, a in ipairs(apps) do
            local s = ts.newSignal({ name = 'bne_' .. j.j .. '_' .. i, pos = vec3(a[2], a[3], a[4]),
              dir = vec3(a[5], a[6], 0), controllerId = cs[a[7] == 1 and 1 or 0].id, sequenceId = seq.id })
            inst[#inst + 1] = s
            local okH, errH = pcall(makeHead, s, a)
            if not okH then log('W', 'beamngEarth', 'navgraph: signal head: ' .. tostring(errH)) end
          end
        else
          if not stop then
            stop = ts.newController({ name = 'bne_stop' })
            stop:applyDefinition('signStop')
            ctrls[#ctrls + 1] = stop
          end
          for i, a in ipairs(apps) do
            inst[#inst + 1] = ts.newSignal({ name = 'bne_' .. j.j .. '_' .. i, pos = vec3(a[2], a[3], a[4]),
              dir = vec3(a[5], a[6], 0), controllerId = stop.id })
          end
        end
      end
    end
  end
  return { instances = inst, controllers = ctrls, sequences = seqs }, ns
end

local function signalsSystem()
  if not core_trafficSignals and extensions and extensions.load then pcall(extensions.load, 'core_trafficSignals') end
  return core_trafficSignals
end

local function setRules()
  -- the country's rules (server navgraph.road_rules): driving side, turn on red, red+yellow before green
  local r = graph.rules
  if r then
    M.rules = { rightHandDrive = r.rightHandDrive and true or false, turnOnRed = r.turnOnRed and true or false }
    M.SIGNAL_TYPE = r.redYellow and 'lightsBasicEU' or 'lightsBasic'
  end
end

-- pre: toNodes' results built ahead (buildNodes), else made here
local function apply(b, pre, moved)
  setRules()
  local tN = os.clock()
  local nodes, nn, ne, cl
  if pre then nodes, nn, ne, cl = pre[1], pre[2], pre[3], pre[4]
  else nodes, nn, ne, cl = M.toNodes(graph, b, M.EDGE_M, M.rules.rightHandDrive) end
  stats.nodesMs = (os.clock() - tN) * 1000         -- (the parts of an apply's frame: the hitch hunt, 2026-10-23)
  local ts = signalsSystem()
  -- before the reset: its onNavgraphReloaded would place the old instances on the new graph (research §2.8)
  if ts then pcall(ts.resetSignals) end
  writeFile(M.RULES_FILE, jsonEncode({ roadRules = M.rules }))
  local orig = path.getPathLevelInfo
  path.getPathLevelInfo = function() return M.RULES_FILE end
  -- the reset sends the whole map to the UI (NavigationMapChanged, research §2.3): only the big map uses it,
  -- and it is a good part of the reset's frame; dropped during ours (M.UI_MAP = true keeps it)
  local trig = guihooks and guihooks.trigger
  if trig and not M.UI_MAP then
    guihooks.trigger = function(name, ...) if name ~= 'NavigationMapChanged' then return trig(name, ...) end end
  end
  local okG, gps = pcall(require, 'beamngEarth/gps')
  local mOld = map.getMap and map.getMap()
  local tG = os.clock()
  local keep = okG and gps.beforeGraph and gps.beforeGraph(mOld and mOld.nodes) or nil
  stats.gpsMs = (os.clock() - tG) * 1000
  local t0 = os.clock()
  ours = true
  local ok, err = pcall(map.reset, nodes)
  ours = false
  if trig then guihooks.trigger = trig end
  path.getPathLevelInfo = orig
  if not ok then
    log('E', 'beamngEarth', 'navgraph: map.reset: ' .. tostring(err))
    return false
  end
  stats.resetMs = (os.clock() - t0) * 1000
  local m = map.getMap()
  if persistent and persistent ~= m.nodes then
    table.clear(persistent)
    for k, v in pairs(m.nodes) do persistent[k] = v end
    m.nodes = persistent
  else
    persistent = m.nodes
  end
  local tA = os.clock()
  if keep then pcall(gps.afterGraph, keep, m.nodes, moved) end    -- before the vehicles' new map
  stats.gpsMs = stats.gpsMs + (os.clock() - tA) * 1000
  be:queueAllObjectLua('mapmgr.requestMap()')
  -- the minimap (the navigation app, in 0.40's freeroam layout and ours) indexes the map's roads once and keeps them:
  -- it showed the first graph's streets, or none, as the player drove on (the user, 2026-10-22)
  local mr = rawget(_G, 'ui_apps_minimap_roads')
  if mr and mr.reset then pcall(mr.reset) end
  pendingSignals = ts and nodes or nil     -- next frame: a second, smaller hitch instead of one big one
  cuts = cl
  stats.applies, stats.lastMs, stats.nodes, stats.edges = stats.applies + 1, (os.clock() - t0) * 1000, nn, ne
  stats.applyMs = (os.clock() - tN) * 1000
  stats.cuts = #cl
  return true
end

-- The vehicle Lua that hands a vehicle the signals (a serialized table). A traffic car keeps the junction it
-- is approaching across map changes (ai.lua's trafficStates.intersection; stock maps never change) and
-- reads signalsData[node][nextNode][idx] without a check: a new graph without that junction (a teleport,
-- or the junction left the window) disabled every car near one (in game 2026-10-14, "attempt to index a
-- nil value" at ai.lua trafficActions). So the new table keeps the old entries it lacks: their nodes are
-- not on the new graph's paths (ai.lua's other reads check first), only a remembered junction resolves.
function M.setSignalsLua(data)
  return 'do local old, new = mapmgr.signalsData, ' .. data .. '\n'
    .. 'if old and new then for a, t in pairs(old) do local n = new[a] if not n then new[a] = t else '
    .. 'for b, u in pairs(t) do if n[b] == nil then n[b] = u end end end end end\n'
    .. 'mapmgr.setSignals(new) end'
end

-- The signals of the graph applied last frame (core_trafficSignals resolves them on the new map).
local function applySignals(nodes)
  local ts = core_trafficSignals
  if not ts then return end
  local t1 = os.clock()
  local okS, err2 = pcall(function()
    seenHeads = {}
    local data, ns = signalData(nodes)
    clearHeads(seenHeads)                   -- the heads of junctions no longer in the graph
    ts.setupSignals(data, false, true)
    -- a vehicle holding signals reads the state-change mailbox every frame and dies on one never written
    -- ("lpack: unexpected end of buffer", in game 2026-10-14): write an empty update before handing them out
    be:sendToMailbox('trafficSignalUpdates', lpack.encodeBin({}))
    be:queueAllObjectLua(M.setSignalsLua(serialize(ts.getMapNodeSignals())))
    stats.junctions = ns
    -- the road tiles' static booms stand where the switchable heads are now
    local trees = require('beamngEarth/trees')
    local grid = {}
    for _, h in pairs(heads) do grid[math.floor(h.x / 4) .. ',' .. math.floor(h.y / 4)] = true end
    trees.suppress = function(g, x, y)
      if g ~= 'signal' then return false end
      local cx, cy = math.floor(x / 4), math.floor(y / 4)
      for dx = -1, 1 do for dy = -1, 1 do if grid[(cx + dx) .. ',' .. (cy + dy)] then return true end end end
      return false
    end
    trees.refresh()
  end)
  stats.signalsMs = (os.clock() - t1) * 1000
  if not okS then log('W', 'beamngEarth', 'navgraph: signals: ' .. tostring(err2)) end
end

local function trafficSystem()
  if not (gameplay_traffic and core_multiSpawn and gameplay_walk) and extensions and extensions.load then
    pcall(extensions.load, unpack(M.EXTENSIONS))
  end
  return gameplay_traffic
end

local function playerId() return be and be.getPlayerVehicleID and be:getPlayerVehicleID(0) or -1 end

-- Pure: where a respawned car goes, (x, y, dirX, dirY, min m, max m) for gameplay_traffic.forceTeleport, or nil for
-- behind the camera: ahead of a player at (px, py) moving (vx, vy) m/s faster than AHEAD_MS, AHEAD_M along the way but
-- no farther than the block b's edge guard (GUARD_M + a margin) in that direction; nil too when that leaves no room.
function M.aheadSpot(px, py, vx, vy, b)
  local sp = math.sqrt(vx * vx + vy * vy)
  if sp < M.AHEAD_MS then return nil end
  local dx, dy = vx / sp, vy / sp
  local reach = math.huge
  if b then
    if dx > 1e-6 then reach = math.min(reach, (b[3] - px) / dx) elseif dx < -1e-6 then reach = math.min(reach, (b[1] - px) / dx) end
    if dy > 1e-6 then reach = math.min(reach, (b[4] - py) / dy) elseif dy < -1e-6 then reach = math.min(reach, (b[2] - py) / dy) end
  end
  local hi = math.min(M.AHEAD_M[2], reach - M.GUARD_M - 20)
  if hi < M.AHEAD_M[1] then return nil end
  return px, py, dx, dy, M.AHEAD_M[1], hi
end

-- Pure: is a car that has stood still since `since` (nil: moving) at distance d from the player stuck at `now`?
function M.stuck(since, now, d)
  return since ~= nil and now - since >= M.STUCK_S and d >= M.STUCK_FAR_M
end

-- Traffic cars the block left behind (or all, after a teleport), and stuck ones, are respawned on the graph.
local stillSince = {}   -- car id -> os.clock() it stopped moving
local seenAt = {}       -- car id -> { x, y, os.clock() it appeared there }
local lastTeleport = {} -- car id -> os.clock() it was last teleported
local function guard(b, all)
  local gt = gameplay_traffic
  if not gt or gt.getState() ~= 'on' then return end
  local me = playerId()
  local pv = me >= 0 and getObjectByID(me) or nil
  local pp = pv and pv:getPosition() or nil
  local now = os.clock()
  -- paused: nothing moves, nobody is stuck (the clocks start again after it)
  local paused = simTimeAuthority and simTimeAuthority.getPause and simTimeAuthority.getPause()
  if paused then table.clear(stillSince) end
  for id in pairs(gt.getTrafficData() or {}) do
    if id ~= me then
      local o = getObjectByID(id)
      if o and o:getActive() then
        local p = o:getPosition()
        local u = o:getVelocity()
        local sp = math.sqrt(u.x * u.x + u.y * u.y)
        local dir = sp > 1 and { x = u.x / sp, y = u.y / sp } or o:getDirectionVector()
        local dl = math.sqrt(dir.x * dir.x + dir.y * dir.y)
        local s0 = seenAt[id]
        if not s0 or (p.x - s0[1]) ^ 2 + (p.y - s0[2]) ^ 2 > M.JUMP_M ^ 2 then s0 = { p.x, p.y, now } end
        seenAt[id] = { p.x, p.y, s0[3] }
        local reach = now - s0[3] < M.FRESH_S and M.CUT_FRESH_M or math.max(M.CUT_M, sp * M.CUT_S)
        local toCut = dl > 1e-6 and M.headingToCut(p.x, p.y, dir.x / dl, dir.y / dl, cuts, reach)
        if not paused and o:getVelocity():length() < M.STUCK_MS then
          stillSince[id] = stillSince[id] or now
        else
          stillSince[id] = nil
        end
        local d = pp and (p - pp):length() or math.huge
        local underCooldown = (now - (lastTeleport[id] or -math.huge)) < M.COOLDOWN_S
        local urgent = not M.inside(b, p.x, p.y, M.GUARD_M)
        if all or urgent or (not underCooldown and (toCut or M.stuck(stillSince[id], now, d))) then
          if toCut then stats.cutRespawns = (stats.cutRespawns or 0) + 1 end
          stillSince[id] = nil
          lastTeleport[id] = now
          -- behind the camera: the stock default is 100-500 m in front of it, where the player sees the car
          -- pop in and its tyres smoke as it lands (the photo comparisons, 2026-10-14)
          local cp, cf = core_camera and core_camera.getPosition(), core_camera and core_camera.getForward()
          local pu = pv and pv:getVelocity()
          local ax, ay, adx, ady, amin, amax = nil
          if pp and pu and not all then ax, ay, adx, ady, amin, amax = M.aheadSpot(pp.x, pp.y, pu.x, pu.y, b) end
          if ax then
            stats.aheadRespawns = (stats.aheadRespawns or 0) + 1
            pcall(gt.forceTeleport, id, vec3(ax, ay, pp.z), vec3(adx, ady, 0), amin, amax)
          elseif cp and cf then
            pcall(gt.forceTeleport, id, cp, -cf, M.RESPAWN_M[1], M.RESPAWN_M[2])
          else
            pcall(gt.forceTeleport, id)
          end
        end
      else
        stillSince[id] = nil
      end
    end
  end
end

-- The player's traffic choice (the user, 2026-10-14: "if a player has them on then yes, if not then no"): the
-- level-start configurator's (Disabled / Parked only / Normal / Police; freeroam's spawningOptionsHelper), taken at
-- mission start by M.takeChoice. Off by default; the radial menu's traffic entries work at any time as on stock maps.
M.choice = { traffic = false, parked = false, police = false, trafficAmount = -1, parkedAmount = -1 }

-- Pure on its argument: the choice in freeroam's spawning options `o`, which are then set to disabled: freeroam would
-- spawn at camera-ready, before our first road graph is in the map; we start them once it is.
local heldOpts = nil    -- { options table, its traffic values } until freeroam's spawn time has passed

-- The player's options back as they chose them (the level-start configurator shows them as the current choice next
-- time): once our cars started, and when the level ends.
function M.restoreChoice()
  if not heldOpts then return end
  local o, v = heldOpts[1], heldOpts[2]
  o.trafficMode, o.trafficParked, o.trafficPolice = v[1], v[2], v[3]
  heldOpts = nil
end

function M.takeChoice(o)
  -- level start runs twice (seen 2026-10-14): the second would read the options we just disabled
  if heldOpts and heldOpts[1] == o then return M.choice end
  heldOpts = nil
  if type(o) ~= 'table' then
    M.choice = { traffic = false, parked = false, police = false, trafficAmount = -1, parkedAmount = -1 }
    return M.choice
  end
  M.choice = { traffic = o.trafficMode == 'enabled', parked = o.trafficParked == 'enabled',
               police = o.trafficPolice == 'enabled', trafficAmount = tonumber(o.trafficAmount) or -1,
               -- (0.40 renamed trafficParkedAmount to trafficParkingAmount)
               parkedAmount = tonumber(o.trafficParkingAmount or o.trafficParkedAmount) or -1 }
  -- (as stock freeroam: with traffic and parked cars both chosen, Options > Gameplay's parked vehicles setting off
  -- means none; GitHub #320 / #145: parked cars came with the setting off)
  local st = rawget(_G, 'settings')
  if M.choice.traffic and M.choice.parked and st and st.getValue and st.getValue('trafficParkedVehicles') == false then
    M.choice.parked = false
  end
  if log then
    log('I', 'beamngEarth', string.format('traffic choice: traffic %s, parked %s, police %s (freeroam %s / %s)',
      tostring(M.choice.traffic), tostring(M.choice.parked), tostring(M.choice.police), tostring(o.trafficMode),
      tostring(o.trafficParked)))
  end
  heldOpts = { o, { o.trafficMode, o.trafficParked, o.trafficPolice } }
  o.trafficMode, o.trafficParked, o.trafficPolice = 'disabled', 'disabled', 'disabled'
  return M.choice
end

local function startTraffic()
  started = true
  M.restoreChoice()       -- freeroam's own spawn time has passed
  local c = M.choice
  local gt = c.traffic and trafficSystem() or nil
  if gt and gt.getState() ~= 'on' then
    local ok, err = pcall(gt.setupTraffic, c.trafficAmount ~= 0 and c.trafficAmount or -1, { police = c.police })
    if not ok then log('W', 'beamngEarth', 'navgraph: setupTraffic: ' .. tostring(err)) end
  end
end

-- The player teleported: the traffic cars go inactive (no physics: their old positions have no ground) and come
-- back on the new place's graph once it is in.
local heldActive = nil
-- A rebase (core finishRebase, xf: the old frame -> the new one): the server's graph moved into the new anchor and
-- applied again at once, as the roads and trees are moved. It was dropped instead (its anchor no longer the current
-- one) and the game's map stayed in the old frame until the server's graph for the new anchor came, seconds later:
-- the AI planned toward roads hundreds of metres off and swerved, into a street tree on North Front Street, Baltimore
-- (Steam Deck, 3 of 3 rebase drives, 2026-10-29). The server's own graph replaces this one when it comes.
function M.transform(xf, anchorId)
  if not graph or not xf then return false end
  local nodes = {}
  for name, n in pairs(graph.nodes) do
    local m = {}
    for i, v in ipairs(n) do m[i] = v end
    m[1], m[2], m[3] = xf:apply(n[1], n[2], n[3])
    nodes[name] = m
  end
  graph = { anchor = anchorId, serial = graph.serial, nodes = nodes, edges = graph.edges, signals = graph.signals,
            rules = graph.rules, moved = true }
  dirty, urgent = true, true
  if applied then applied.box = nil end
  return true
end

function M.currentGraph() return graph end      -- (tests)

function M.onTeleport()
  respawnAll = true
  local gt = gameplay_traffic
  if gt and gt.getState() == 'on' and not heldActive then
    local vars = gt.getTrafficVars and gt.getTrafficVars() or {}
    heldActive = vars.activeAmount or math.huge
    pcall(gt.setActiveAmount, 0)
  end
end

-- Every frame: a new graph, a moved block or a foreign reset applies the graph again; the traffic guard.
-- Pure: how far the centre of ground box b {x0, y0, x1, y1} is from that of box a (0 when either is missing).
function M.boxMoved(a, b)
  if not a or not b or not a[4] or not b[4] then return 0 end
  local dx = (b[1] + b[3]) / 2 - (a[1] + a[3]) / 2
  local dy = (b[2] + b[4]) / 2 - (a[2] + a[4]) / 2
  return math.sqrt(dx * dx + dy * dy)
end

local waited = 0
local stopwatch = require('beamngEarth/stopwatch')

-- The node tables an apply hands map.reset, built over several frames (toNodes in a coroutine, PREP_MS a frame; ~20 ms in
-- one frame on a Steam Deck), for this graph and block box; nil until done.
M.PREP_MS = 2.0
M.PREP_YIELD = 256
local prep = nil        -- { graph, box, co, result }

local function buildNodes(b)
  if not prep or prep.graph ~= graph or not (prep.box == b or (prep.box and b and prep.box[1] == b[1] and prep.box[2] == b[2]
      and prep.box[3] == b[3] and prep.box[4] == b[4])) then
    setRules()
    local g, box, rh = graph, b and { b[1], b[2], b[3], b[4] } or nil, M.rules.rightHandDrive
    prep = { graph = g, box = box, co = coroutine.create(function()
      return { M.toNodes(g, box, M.EDGE_M, rh, M.PREP_YIELD) }
    end) }
  end
  if prep.result then return prep.result end
  local sw = stopwatch.new()
  repeat
    local ok, res = coroutine.resume(prep.co)
    if not ok then
      log('W', 'beamngEarth', 'navgraph: nodes: ' .. tostring(res))
      prep = nil
      return nil
    end
    if coroutine.status(prep.co) == 'dead' then prep.result = res end
  until prep.result or sw() >= M.PREP_MS
  return prep.result
end

-- May the apply run in this frame (gov: the frame governor; nil: always)?
local function room(gov)
  if not (gov and gov.roomFor) or waited >= M.APPLY_WAIT_FRAMES or gov:roomFor(M.APPLY_ROOM_MS) then
    waited = 0
    return true
  end
  waited = waited + 1
  return false
end

function M.update(aid, b, gov)
  if not M.enabled or not graph then return end
  local t0 = os.clock()
  if applied and t0 - lastGuard >= M.GUARD_S then
    lastGuard = t0
    local ok, err = pcall(guard, b, false)
    if not ok then log('W', 'beamngEarth', 'navgraph guard: ' .. tostring(err)) end
    local g = (os.clock() - t0) * 1000
    stats.guardMs = g
    stats.guardMaxMs = math.max(stats.guardMaxMs or 0, g)
  end
  if graph.anchor ~= aid then
    graph = nil
    return
  end
  if pendingSignals then
    if not room(gov) then return end
    local nodes = pendingSignals
    pendingSignals = nil
    local sw = stopwatch.new()
    applySignals(nodes)
    if gov and gov.noteFrame then gov:noteFrame(sw()) end
    return
  end
  -- a small block move alone does not re-apply (each apply is a ~40 ms frame); one of RECLIP_M does
  if not (dirty or foreign) and applied and M.boxMoved(applied.box, b) >= M.RECLIP_M then dirty = true end
  if not (dirty or foreign) then return end
  local t = os.clock()
  if t - lastApply < M.MIN_GAP_S and not foreign and not urgent then return end
  -- (a moved graph, M.transform: built in one go and applied in this frame, whatever the frame budget: until the cars
  -- have it their AI drives on points of the old frame)
  local pre
  if urgent then
    setRules()
    pre = { M.toNodes(graph, b and { b[1], b[2], b[3], b[4] } or nil, M.EDGE_M, M.rules.rightHandDrive) }
  else
    pre = buildNodes(b)
    if not pre or not room(gov) then return end
  end
  prep = nil
  lastApply = t
  dirty, foreign = false, false
  local moved = urgent
  urgent = false
  local sw = stopwatch.new()
  local okA = apply(b, pre, moved)
  if gov and gov.noteFrame then gov:noteFrame(sw()) end
  if okA then
    applied = { anchor = aid, serial = graph.serial, box = b and { b[1], b[2], b[3], b[4] } or {} }
    if not started then
      startTraffic()
      respawnAll = false
    elseif respawnAll then
      respawnAll = false
      local gt = gameplay_traffic
      if gt and heldActive then
        pcall(gt.setActiveAmount, heldActive)
        heldActive = nil
        if gt.forceTeleportAll then pcall(gt.forceTeleportAll) end
      else
        pcall(guard, b, true)
      end
    end
  end
end

-- onNavgraphReloaded (core forwards the hook): someone else rebuilt the map; ours again next frame.
function M.onReloaded()
  if not ours and graph then foreign = true end
end

-- How many times a graph went into the game's map (gps.lua: the map's node table stays the same table across them).
function M.applies() return stats.applies end

function M.status()
  return { graph = graph and graph.serial or nil, applies = stats.applies, lastMs = stats.lastMs,
           nodes = stats.nodes, edges = stats.edges, junctions = stats.junctions, started = started,
           cuts = stats.cuts, cutRespawns = stats.cutRespawns, aheadRespawns = stats.aheadRespawns,
           resetMs = stats.resetMs, signalsMs = stats.signalsMs, nodesMs = stats.nodesMs, gpsMs = stats.gpsMs,
           applyMs = stats.applyMs, guardMs = stats.guardMs, guardMaxMs = stats.guardMaxMs,
           traffic = gameplay_traffic and gameplay_traffic.getState() or nil }
end

function M.reset()
  heads = {}            -- (the level took them with it)
  graph, applied, dirty, lastApply, persistent, ours, foreign = nil, nil, false, -math.huge, nil, false, false
  started, respawnAll, lastGuard, heldActive = false, false, -math.huge, nil
  pendingSignals = nil
  stillSince = {}
  seenAt = {}
  lastTeleport = {}
  cuts = {}
  M.restoreChoice()
  stats = { applies = 0, lastMs = 0, nodes = 0, edges = 0 }
  prep, waited = nil, 0
end

return M
