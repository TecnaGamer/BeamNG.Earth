-- luajit mod/tests/test_gps_autodrive.lua
-- Exercise the public GPS update lifecycle, including the production route-joining call.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local saved = {}
for _, name in ipairs({ 'getPlayerVehicle', 'beamngEarth_core', 'map', 'core_groundMarkers', 'vec3' }) do
  saved[name] = rawget(_G, name)
end
local savedClock, savedNav = os.clock, package.loaded['beamngEarth/navgraph']

local function fixture(path, y, radius, configure)
  local G = dofile(dir .. '/../beamng_earth/lua/ge/extensions/beamngEarth/gps.lua')
  local f = { gps = G, now = 0, serial = 1, drives = {}, extensions = {}, modes = {}, arrows = 0, requests = 0,
    healthy = true, speed = 20, x = 40, y = y or 2, z = 0, path = path or { 'j', 's', 't' } }
  local function node(x, y, links) return { pos = { x = x, y = y, z = 0 }, links = links, radius = radius } end
  f.nodes = { w = node(0, 0, { j = {} }), j = node(100, 0, { s = {} }),
    s = node(160, -60, { t = {} }), t = node(400, -60, {}) }
  f.map = { nodes = f.nodes, buildSerial = 1 }
  local env = setmetatable({}, { __index = _G })
  env.ai = { mode = 'disabled', driveUsingPath = function(args)
    T.check(args.path == nil and args.wpTargetList ~= nil, 'fresh start uses lane-aware waypoint input')
    f.drives[#f.drives + 1] = args.wpTargetList
    env.ai.mode = 'manual'
  end, setMode = function(mode) env.ai.mode = mode; f.modes[#f.modes + 1] = mode end,
    setPath = function(path) f.extensions[#f.extensions + 1] = path end }
  env.mapmgr = { requestMap = function() f.requests = f.requests + 1 end }
  env.obj = { queueGameEngineLua = function() G.retryAutodrive() end }
  env.extensions = { load = function()
    local loader = assert(loadfile(dir .. '/../beamng_earth/lua/vehicle/extensions/beamngEarth/autodrive.lua'))
    env.beamngEarth_autodrive = setfenv(loader, env)()
  end }
  function f:mapArrives()
    local graph = {}
    for id in pairs(self.nodes) do graph[id] = {} end
    for id, n in pairs(self.nodes) do
      for other, e in pairs(n.links) do graph[id][other] = e; graph[other][id] = e end
    end
    env.mapmgr.mapData = { graph = graph }
    G.onVehicleMapmgrUpdate(1)
  end
  f.route = { points = { { 0, 0 }, { 0, 0.01 }, { -0.006, 0.016 }, { -0.006, 0.04 } },
    dist = 425, dur = 30 }
  if configure then configure(f) end
  f.anchor = { geodeticToEnu = function(_, lat, lon) return lon * 10000, lat * 10000, 0 end }
  os.clock = function() return f.now end
  package.loaded['beamngEarth/navgraph'] = { applies = function() return f.serial end }
  vec3 = function(x, y, z) return { x = x, y = y, z = z } end
  map = { getMap = function() return f.map end }
  getPlayerVehicle = function() return {
    getId = function() return 1 end,
    getPosition = function() return { x = f.x, y = f.y, z = f.z } end,
    getDirectionVector = function() return { x = 1, y = 0, z = 0 } end,
    getVelocity = function() return { x = f.speed, y = 0, z = 0 } end,
    queueLuaCommand = function(_, command)
      local fn = assert(loadstring(command))
      setfenv(fn, env)()
    end,
  } end
  beamngEarth_core = {
    requestRoute = function() f.answer = f.route; return true end,
    takeRoute = function() local answer = f.answer; f.answer = nil; return answer end,
    serverHealthy = function() return f.healthy end,
  }
  core_groundMarkers = { routePlanner = {}, setPath = function(target)
    f.arrows = f.arrows + 1
    f.target = target
    local path = {}
    for _, id in ipairs(f.path) do path[#path + 1] = { wp = id } end
    core_groundMarkers.routePlanner.path = path
  end }
  function f:step(seconds)
    self.now = self.now + (seconds or 1)
    G.update(self.anchor, { x = self.x, y = self.y, z = self.z, lat = self.y / 10000, lon = self.x / 10000,
      vx = self.speed, vy = 0 })
  end
  local goal = f.route.points[#f.route.points]
  G.setDestination(goal[1], goal[2], 'test destination')
  G.setAutodrive(true)
  f:mapArrives()
  f:step() -- request the route
  f.requestArrows = f.arrows
  f:step() -- accept it and drive the planner's path
  return f
end

-- A nearby service road is not a continuation of a highway whose loaded graph ends first.
local function diversionRoads(f)
  local function n(x, y, links) return { pos = { x = x, y = y, z = 0 }, links = links, radius = 6 } end
  f.nodes = {
    w = n(0, 0, { j = {} }), j = n(100, 0, { k = {}, a = {} }), k = n(200, 0, {}),
    a = n(100, -100, { b = {} }), b = n(180, -100, { c = {} }),
    c = n(180, -20, { d = {} }), d = n(800, -20, {}),
  }
  f.map.nodes = f.nodes
  f.route = { points = { { 0, 0 }, { 0, 0.1 } }, dist = 1000, dur = 60 }
end

for _, offset in ipairs({ 0, -18 }) do
  local f = fixture({ 'j', 'k' }, 2, 6, function(f)
    diversionRoads(f)
    f.route.points = { { offset / 10000, 0 }, { offset / 10000, 0.1 } }
  end)
  local target = f.target[#f.target]
  T.check(target.x <= 200 and math.abs(target.y) < 1e-6,
    'highway target stays on the connected highway, even when the route line is closer to the service road')
end

do
  local f = fixture({ 'j', 'a', 'b', 'c', 'd' }, 2, 6, function(f)
    diversionRoads(f)
    f.route.points = { { 0, 0 }, { 0, 0.01 }, { -0.01, 0.01 }, { -0.01, 0.018 },
      { -0.002, 0.018 }, { -0.002, 0.08 } }
  end)
  local target = f.target[#f.target]
  T.check(target.x > 700 and target.y == -20 and #f.drives == 1,
    'a planned exit follows its connecting roads onto the same service road')
end

do
  local f = fixture({ 'j', 'k' }, 2, 6, function(f)
    diversionRoads(f)
    f.z = 12
    for _, id in ipairs({ 'w', 'j', 'k' }) do f.nodes[id].pos.z = 12 end
    f.nodes.lower = { pos = { x = 0, y = 2, z = 0 }, links = { lowerEnd = {} }, radius = 6 }
    f.nodes.lowerEnd = { pos = { x = 1000, y = 2, z = 0 }, links = {}, radius = 6 }
    f.route.points = { { 0.0002, 0 }, { 0.0002, 0.1 } }
  end)
  local target = f.target[#f.target]
  T.check(target.z == 12 and target.x <= 200,
    'target selection stays on the bridge when the road below is closer to the route line')
end

do
  local recoveries = 0
  local savedSpawn, savedQuat, savedLog = spawn, quatFromDir, log
  spawn = { safeTeleport = function() recoveries = recoveries + 1 end }
  quatFromDir = function(d) return d end
  log = function() end
  local f = fixture({ 'j', 'a', 'b', 'c', 'd' }, 2, 6, function(f)
    diversionRoads(f)
    f.x, f.speed = 95, 0
    f.nodes.j.links.k = nil
  end)
  T.check(f.arrows == f.requestArrows and f.modes[#f.modes] == 'stop',
    'no reachable route sample holds instead of using the destination-nearest side road fallback')
  f:step(35); f:step(35); f:step(35)
  T.check(recoveries == 0 and f.arrows == f.requestArrows,
    'waiting for the route graph does not recover onto another road')
  f.nodes.j.links.k = {}; f.path = { 'j', 'k' }
  f.serial = 2; f.map.buildSerial = 2; f:mapArrives(); f:step()
  local target = f.target[#f.target]
  T.check(f.arrows > f.requestArrows and target.x <= 200 and target.y == 0 and #f.drives > 0,
    'the normal polling lifecycle resumes along the highway once its graph connection loads')
  f = fixture({ 'j', 'a', 'b', 'c', 'd' }, 30, 6, function(f)
    diversionRoads(f)
    f.x, f.speed = 95, 0
    f.nodes.w.radius, f.nodes.j.radius = 50, 50
    f.nodes.j.links.k = nil
  end)
  for _ = 1, 350 do f:step() end
  T.check(recoveries == 0 and f.modes[#f.modes] == 'stop',
    'an outer lane on the intended wide edge still waits at a graph cut, even over 25 m from its centre')
  f = fixture(nil, 20, 6, function(f) f.speed = 0 end)
  f:step(35); f:step(35); f:step(35)
  T.check(recoveries == 1, 'an off-road car still recovers when its intended route has a usable graph')
  spawn, quatFromDir, log = savedSpawn, savedQuat, savedLog
end

-- A fully loaded route can be unreachable nearby from a different parallel road.
do
  local recoveries, moveOnRecovery, f = 0, false, nil
  local savedSpawn, savedQuat, savedLog = spawn, quatFromDir, log
  spawn = { safeTeleport = function(_, p)
    recoveries = recoveries + 1
    if moveOnRecovery then f.x, f.y, f.z = p.x, p.y, p.z end
  end }
  quatFromDir = function(d) return d end
  log = function() end
  local function parallel(loaded)
    return fixture({ 'a', 'b' }, 40, 6, function(f)
      local function n(x, y, links) return { pos = { x = x, y = y, z = 0 }, radius = 6, links = links } end
      f.speed = 0
      f.nodes = { w = n(0, 40, { j = {} }), j = n(500, 40, {}) }
      if loaded then
        f.nodes.a, f.nodes.b = n(0, 0, { b = {} }), n(1000, 0, {})
        f.nodes.j.links.b = {} -- the only junction is far beyond the local projection limit
      end
      f.map.nodes = f.nodes
      f.route = { points = { { 0, 0 }, { 0, 0.1 } }, dist = 1000, dur = 50 }
    end)
  end
  f = parallel(true)
  for _ = 1, 350 do f:step() end
  T.check(recoveries > 0 and f.gps.autodrive() and f.gps.status().off == 40,
    'an unreachable loaded route 40 m away retains recovery despite being inside the reroute threshold')
  T.check(#f.drives == 0, 'recovery does not hand the AI a disconnected path or a destination-nearest fallback')
  recoveries, moveOnRecovery = 0, true
  f = parallel(true)
  for _ = 1, 100 do f:step(); if recoveries > 0 then break end end
  f:step()
  T.check(recoveries == 1 and f.y == 0 and f.x > 40 and #f.drives > 0 and f.gps.status().off == 0,
    'after recovering to the intended road, normal GPS polling starts a connected forward path')
  recoveries, moveOnRecovery = 0, false
  f = parallel(false)
  for _ = 1, 350 do f:step() end
  T.check(recoveries == 0 and #f.drives == 0 and f.modes[#f.modes] == 'stop',
    'a parallel road alone is not evidence that the intended route graph has loaded')
  spawn, quatFromDir, log = savedSpawn, savedQuat, savedLog
end

do
  local f = fixture()
  T.check(#f.drives == 1 and table.concat(f.drives[1], ',') == 'j,s,t',
    'the first AI target stays ahead of the car, without the rear endpoint')
  local first = f.nodes[f.drives[1][1]].pos
  T.check(first.x > f.x, 'the commanded first target is ahead in the vehicle heading')
  T.check(f.target[1] == 'j', 'the ground-marker planner is anchored at the forward junction')
  f = fixture({ 's', 't' })
  T.check(#f.drives == 1 and table.concat(f.drives[1], ',') == 'j,s,t',
    'a planner path beyond the bend is joined through the forward junction only')
  f = fixture({ 'w', 'j', 's', 't' })
  T.check(#f.drives == 1 and table.concat(f.drives[1], ',') == 'j,s,t',
    'a path starting behind the car on its current segment drops the rear endpoint')
  f = fixture(nil, 20)
  T.check(#f.drives == 0 and f.modes[#f.modes] == 'stop',
    'without a nearby matching segment, hold rather than drive toward an unrelated road')
end

-- Existing route loops are valid, including after removing the rear endpoint.
for _, path in ipairs({ { 'j', 's', 't', 'w', 'j' }, { 'w', 'j', 's', 't', 'w', 'j' } }) do
  local f = fixture(path, 2, 6, function(f)
    f.nodes.t.links.w = {}
    f.route = { points = { { 0, 0 }, { 0, 0.01 }, { -0.006, 0.016 }, { -0.006, 0.04 },
      { 0, 0 }, { 0, 0.01 } }, dist = 900, dur = 90 }
  end)
  T.check(#f.drives == 1 and table.concat(f.drives[1], ',') == 'j,s,t,w,j' and #f.modes == 0,
    'a planned loop through the starting edge drives without stopping')
  f:mapArrives(); f:step()
  T.check(#f.drives == 2 and table.concat(f.drives[2], ',') == 'j,s,t,w,j' and #f.modes == 0,
    'a planned loop restarts intact after map replacement')
end

do
  local f = fixture({ 's', 'j' })
  T.check(#f.drives == 0 and f.modes[#f.modes] == 'stop',
    'joining a path must still reject a new loop through the forward junction')
end

do
  local f = fixture(nil, 10, 12)
  T.check(#f.drives == 1 and table.concat(f.drives[1], ',') == 'j,s,t',
    'autodrive starts in an outer lane 10 m from the centre of a 24 m carriageway')
  f:mapArrives(); f:step()
  T.check(#f.drives == 2 and #f.modes == 0, 'a graph handoff in the outer lane resumes without a stop')
  f = fixture(nil, 14, 12)
  T.check(#f.drives == 0 and f.modes[#f.modes] == 'stop', 'a position beyond the wide carriageway still holds')
end

do
  local f = fixture(nil, 2, 6, function(f)
    f.z = 12
    for _, n in pairs(f.nodes) do n.pos.z = 12 end
    f.nodes.lowerA = { pos = { x = 0, y = 2, z = 0 }, radius = 6, links = { lowerB = {} } }
    f.nodes.lowerB = { pos = { x = 100, y = 2, z = 0 }, radius = 6, links = {} }
  end)
  T.check(f.target[1] == 'j' and #f.drives == 1 and #f.modes == 0,
    'both planner anchoring and handoff select the bridge instead of the road directly below')
  f:mapArrives(); f:step()
  T.check(#f.drives == 2 and #f.modes == 0, 'bridge route resumes after map replacement without stopping')
end

do
  local f = fixture({ 'w', 'j' }, 2, 6, function(f)
    f.route = { points = { { 0, 0 }, { 0, 0.01 } }, dist = 100, dur = 10 }
  end)
  T.check(#f.drives == 1 and table.concat(f.drives[1], ',') == 'j' and #f.modes == 0,
    'a final-edge route drives to its single forward waypoint after removing the rear node')
  f.gps.onVehicleMapmgrUpdate(1); f:step()
  T.check(#f.drives == 1 and #f.modes == 0, 'an unchanged final waypoint keeps the active plan')
  f:mapArrives(); f:step()
  T.check(#f.drives == 2 and #f.modes == 0, 'a final waypoint restarts on a replacement vehicle map')
end

do
  local f = fixture({ 't', 's' })
  T.check(#f.drives == 0 and f.modes[#f.modes] == 'stop', 'a disconnected planner start is rejected')
  f.path = { 'j', 's', 't' }; f:step()
  T.check(#f.drives == 1, 'a blocked path retries and resumes once connected')
end

do
  local f = fixture()
  f.gps.onVehicleMapmgrUpdate(1); f:step()
  T.check(#f.drives == 1 and #f.extensions == 0, 'same-map identical paths do not reset the vehicle AI')
  f.gps.retryAutodrive(); f:step()
  T.check(#f.drives == 2, 'explicit recovery forces a fresh route even when the nodes match')
  local keep = f.gps.beforeGraph(f.nodes)
  f.map.buildSerial = 2
  f.gps.afterGraph(keep, f.nodes); f.serial = 2
  f:step(5)
  T.check(#f.drives == 2 and f.requests > 0, 'a new graph waits for the vehicle acknowledgement')
  f:step(5)
  T.check(#f.drives == 2 and f.modes[#f.modes] == 'stop', 'timeout holds rather than submitting to the old graph')
  f:mapArrives(); f:step()
  T.check(#f.drives == 3, 'an unchanged route restarts on the new graph instead of leaving the AI without a path')
  f.gps.setAutodrive(false)
  T.check(f.modes[#f.modes] == 'disabled', 'disabling clears the vehicle route state')
end

do
  local f = fixture()
  f.speed = 0; f:step(); f:step(7)
  T.check(#f.drives == 2, 'a stalled or ended route is restarted even if its node list did not change')
  f.healthy = false; f:step()
  T.check(f.modes[#f.modes] == 'stop', 'server outage holds the truck')
  f.healthy = true; f:step()
  T.check(#f.drives == 3, 'server recovery restarts the route')
end

-- The stock AI clears its route on completion but stays in manual mode. Its GE notification is asynchronous.
do
  local f = fixture()
  f.gps.onAiRouteDone(2); f:step()
  T.check(#f.drives == 1, 'another vehicle finishing its route does not restart the player')
  f.gps.onAiRouteDone(1); f:step(0.1)
  T.check(#f.drives == 2 and #f.extensions == 0,
    'completion restarts identical waypoints immediately, before the normal check and stall timers')

  f.nodes.u = { pos = { x = 500, y = -60, z = 0 }, links = {} }
  f.nodes.t.links.u = {}
  f:mapArrives(); f:step()
  local starts = #f.drives
  f.path = { 'j', 's', 't', 'u' }
  -- A route update can be sent before the queued completion notification is delivered.
  f.gps.onVehicleMapmgrUpdate(1); f:step()
  T.check(#f.extensions == 1, 'a compatible update can precede the completion notification')
  f.gps.onAiRouteDone(1); f:step(0.1)
  T.check(#f.drives == starts + 1 and #f.extensions == 1
    and table.concat(f.drives[#f.drives], ',') == 'j,s,t,u',
    'a delayed completion notification restarts the latest extended route through waypoints')

  f.gps.setAutodrive(false)
  starts = #f.drives
  f.gps.onAiRouteDone(1); f:step()
  T.check(#f.drives == starts and f.modes[#f.modes] == 'disabled',
    'a late completion notification cannot re-enable disabled Autodrive')
end

for _, name in ipairs({ 'getPlayerVehicle', 'beamngEarth_core', 'map', 'core_groundMarkers', 'vec3' }) do
  _G[name] = saved[name]
end
os.clock, package.loaded['beamngEarth/navgraph'] = savedClock, savedNav
T.done('test_gps_autodrive')
