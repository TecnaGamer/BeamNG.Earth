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
    setPath = function(path) f.extensions[#f.extensions + 1] = path end,
    setSpeedMode = function(mode) f.speedMode = mode end,
    setStopPoint = function(_, distance) f.stopDistance = distance end }
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
    for _, id in ipairs(f.path) do path[#path + 1] = type(id) == 'table' and id or { wp = id } end
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

-- One projection near a fork can be closer to an exit than to the continuing main road.
do
  local f = fixture({ 'j', 'main', 'end' }, -2, 6, function(f)
    local function n(x, y, links) return { pos = { x = x, y = y, z = 0 }, radius = 6, links = links } end
    f.nodes = { w = n(0, -4, { j = { oneWay = true, inNode = 'w' } }),
      j = n(300, -4, { main = { oneWay = true, inNode = 'j' }, ramp = { oneWay = true, inNode = 'j' } }),
      main = n(420, 0, { ['end'] = { oneWay = true, inNode = 'main' } }), ['end'] = n(1000, 0, {}),
      ramp = n(360, 2, { exit = { oneWay = true, inNode = 'ramp' } }), exit = n(500, 30, {}) }
    f.map.nodes = f.nodes
    f.route = { points = { { 0, 0 }, { 0, 0.1 } }, dist = 1000, dur = 60 }
  end)
  T.check(f.target[#f.target].x > 900, 'a tentative exit projection does not hide the reachable main-road continuation')
  for _, wp in ipairs(f.target) do
    if type(wp) == 'table' then
      T.check(wp.y <= 0, 'the planner receives no mandatory waypoint on the abandoned exit')
    end
  end
  f:mapArrives(); f:step()
  T.check(f.target[#f.target].x > 900, 'graph replacement preserves the main-road choice at the fork')
end

-- At a shallow fork the truck remains inside its current highway lane even when the ramp centre is nearer.
do
  local f = fixture({ 'j', 'main', 'end' }, -2, 4, function(f)
    local function n(x, y, links) return { pos = { x = x, y = y, z = 0 }, radius = 4, links = links } end
    f.nodes = { w = n(0, -4, { j = {} }), j = n(300, -4, { main = {}, ramp = {} }),
      main = n(420, -4, { ['end'] = {} }), ['end'] = n(1000, -4, {}),
      ramp = n(360, -2, { exit = {} }), exit = n(500, 30, {}) }
    f.map.nodes = f.nodes
    f.route = { points = { { -0.0004, 0 }, { -0.0004, 0.1 } }, dist = 1000, dur = 60 }
  end)
  f.x = 340
  f:mapArrives(); f:step()
  T.check(f.target[1] == 'main', 'a nearer exit centre does not replace the current underfoot highway edge')
  T.check(f.target[#f.target].x > 900, 'the overlap at the fork retains the highway continuation')
  f.x = 350; f:mapArrives(); f:step()
  T.check(f.target[1] == 'main' and f.target[#f.target].x > 900,
    'continuity keeps the current edge after the rear node is removed from the submitted waypoint list')
end

-- Past the end of a wide segment, its rounded endpoint must not pin the planner behind the car.
do
  local f = fixture({ 'j', 's', 't' }, 3, 10, function(f)
    local function n(x, radius, links) return { pos = { x = x, y = 0, z = 0 }, radius = radius, links = links } end
    f.nodes = { w = n(0, 10, { j = { inNode = 'w', inRadius = 10, outRadius = 10 } }),
      j = n(100, 10, { s = { inNode = 'j', inRadius = 2, outRadius = 2 } }),
      s = n(200, 2, { t = {} }), t = n(400, 2, {}) }
    f.map.nodes = f.nodes
    f.route = { points = { { 0, 0 }, { 0, 0.1 } }, dist = 1000, dur = 60 }
  end)
  f.x = 105; f:mapArrives(); f:step()
  T.check(f.target[1] == 's' and f.drives[#f.drives][1] == 's',
    'at a narrowing both the planner anchor and vehicle handoff advance beyond the passed wide endpoint')
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
  f = fixture({ 'j', 'a', 'b', 'c', 'd' }, 2, 6, function(f)
    diversionRoads(f)
    f.x, f.speed = 95, 0
    f.nodes.j.links.k = nil
    f.route.points = { { -0.0018, 0 }, { -0.0018, 0.1 } }
  end)
  for _ = 1, 350 do f:step() end
  T.check(recoveries == 0, 'a route-line offset beside a junction does not turn a graph cut into recovery')
  f = fixture({ 'j', 'k' }, 2, 6, function(f)
    diversionRoads(f)
    f.x, f.z, f.speed = 195, 12, 0
    for _, id in ipairs({ 'w', 'j', 'k' }) do f.nodes[id].pos.z = 12 end
    f.nodes.lower = { pos = { x = 0, y = 2, z = 0 }, links = { lowerEnd = {} }, radius = 6 }
    f.nodes.lowerEnd = { pos = { x = 1000, y = 2, z = 0 }, links = {}, radius = 6 }
    f.route.points = { { 0.0002, 0 }, { 0.0002, 0.1 } }
  end)
  for _ = 1, 350 do f:step() end
  T.check(recoveries == 0, 'a bridge graph cut does not recover onto the loaded road below')
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
  local function parallel(loaded, offset, deadEnd, roadOffset)
    offset = offset or 40
    return fixture({ 'a', 'b' }, offset, 6, function(f)
      local function n(x, y, links) return { pos = { x = x, y = y, z = 0 }, radius = 6, links = links } end
      f.speed = 0
      if deadEnd then f.x = 95 end
      f.nodes = { w = n(0, offset, { j = {} }), j = n(deadEnd and 100 or 500, offset, {}) }
      if loaded then
        f.nodes.a, f.nodes.b = n(0, roadOffset or 0, { b = {} }), n(1000, roadOffset or 0, {})
        -- The only connection is behind the dead end, or far ahead on the longer parallel road.
        if deadEnd then f.nodes.w.links.a = {} else f.nodes.j.links.b = {} end
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
  recoveries = 0
  f = parallel(true, 20, true)
  for _ = 1, 350 do f:step() end
  T.check(recoveries > 0 and f.gps.status().off == 20 and #f.drives == 0,
    'a dead end only 20 m from a loaded route retains recovery without an unsafe fallback')
  recoveries, moveOnRecovery = 0, true
  f = parallel(true, 20, true)
  for _ = 1, 100 do f:step(); if recoveries > 0 then break end end
  f:step()
  T.check(recoveries == 1 and f.y == 0 and f.x > 40 and #f.drives > 0 and f.gps.status().off == 0,
    'after recovering to the intended road, normal GPS polling starts a connected forward path')
  recoveries, moveOnRecovery = 0, false
  f = parallel(false)
  for _ = 1, 350 do f:step() end
  T.check(recoveries == 0 and #f.drives == 0 and f.modes[#f.modes] == 'stop',
    'a parallel road alone is not evidence that the intended route graph has loaded')
  f = parallel(false, 20, true)
  for _ = 1, 350 do f:step() end
  T.check(recoveries == 0 and #f.drives == 0,
    'a nearby dead end still waits when the intended route itself has not loaded')
  for _, roadOffset in ipairs({ 10, 25 }) do
    recoveries = 0
    f = parallel(true, 60, true, roadOffset)
    for _ = 1, 350 do f:step() end
    T.check(recoveries > 0 and #f.drives == 0,
      'loaded road ' .. roadOffset .. ' m from the route line retains recovery within GPS projection tolerance')
  end
  recoveries, moveOnRecovery = 0, true
  f = parallel(true, 40, true, 10)
  for _ = 1, 100 do f:step(); if recoveries > 0 then break end end
  f:step()
  T.check(recoveries == 1 and f.y == 10 and #f.drives > 0 and f.gps.status().off == 10,
    'polling resumes a connected route after recovering onto a road offset from the planned line')
  recoveries, moveOnRecovery = 0, false
  f = parallel(true, 60, true, 26)
  for _ = 1, 350 do f:step() end
  T.check(recoveries == 0, 'a loaded road outside GPS projection tolerance still waits for a usable route graph')
  spawn, quatFromDir, log = savedSpawn, savedQuat, savedLog
end

-- The router can snap its start ahead of a car already on the same loaded road.
do
  local recoveries = 0
  local savedSpawn, savedQuat, savedLog = spawn, quatFromDir, log
  spawn = { safeTeleport = function() recoveries = recoveries + 1 end }
  quatFromDir = function(d) return d end
  log = function() end
  local function gap(loaded, height, startX)
    return fixture({ 'j' }, 2, 4, function(f)
      f.x, f.speed = startX or 95, 0
      f.nodes = { w = { pos = { x = 0, y = 0, z = 0 }, radius = 4, links = { j = {} } },
        j = { pos = { x = 100, y = 0, z = 0 }, radius = 4, links = {} } }
      if loaded then
        f.nodes.next = { pos = { x = 220, y = 0, z = height or 0 }, radius = 4, links = { far = {} } }
        f.nodes.far = { pos = { x = 1000, y = 0, z = height or 0 }, radius = 4, links = {} }
      end
      f.map.nodes = f.nodes
      f.route = { points = { { 0, 0 }, { 0, 0.1 } }, dist = 1000, dur = 60 }
    end)
  end
  local f = gap(true)
  T.check(f.modes[#f.modes] == 'stop' and #f.drives == 0, 'a graph gap holds rather than sending a disconnected path')
  for _ = 1, 70 do f:step() end
  T.check(recoveries == 1, 'a loaded continuation across a 120 m graph gap retains normal no-progress recovery')
  recoveries = 0; f = gap(true, nil, 40)
  T.check(#f.drives == 1, 'the vehicle first drives the valid segment before the gap')
  f.x = 95; f.gps.onAiRouteDone(1)
  for _ = 1, 70 do f:step() end
  T.check(recoveries == 1, 'a completed local path still recovers when the continuation is loaded but disconnected')
  recoveries = 0; f = gap(false)
  for _ = 1, 350 do f:step() end
  T.check(recoveries == 0, 'an unloaded continuation still waits for the graph')
  for _, height in ipairs({ -4, -12 }) do
    recoveries = 0; f = gap(true, height)
    for _ = 1, 350 do f:step() end
    T.check(recoveries == 0, 'a road ' .. -height .. ' m below a bridge cut is not a loaded continuation')
  end
  recoveries = 0; f = gap(true)
  f.nodes.w.pos.z, f.nodes.j.pos.z, f.nodes.next.pos.z, f.nodes.far.pos.z = -6, 0, 7.2, 54
  f.serial = f.serial + 1
  for _ = 1, 70 do f:step() end
  T.check(recoveries == 1, 'a loaded continuation follows the road grade across an uphill gap')
  spawn, quatFromDir, log = savedSpawn, savedQuat, savedLog
end

for _, gap in ipairs({ 0, 60, 100 }) do
  local f = fixture({ 'n1', 'n2', 'n3', 'n4' }, 0, 6, function(f)
    f.x, f.y = 10, 0
    f.nodes = {}
    for i = 0, 13 do f.nodes['n' .. i] = { pos = { x = 80 * i, y = 0, z = 0 }, radius = 6, links = {} } end
    for i = 0, 12 do f.nodes['n' .. i].links['n' .. (i + 1)] = {} end
    f.map.nodes = f.nodes
    f.route = { points = { { 0, (10 + gap) / 10000 }, { 0, 0.1 } }, dist = 990 - gap, dur = 50 }
  end)
  T.check(#f.drives == 1 and f.drives[1][1] == 'n1',
    'a car ' .. gap .. ' m behind the route start drives forward on 80 m graph edges')
  f:mapArrives(); f:step()
  T.check(#f.drives == 2 and f.drives[2][1] == 'n1', 'the initial route gap also works after graph replacement')
end

do
  local f = fixture({ 'n1', 'n2', 'j', 'k' }, 0, 6, function(f)
    local function n(x, y, links) return { pos = { x = x, y = y, z = 0 }, radius = 6, links = links } end
    f.x, f.y = 10, 0
    f.nodes = { n0 = n(0, 0, { n1 = {} }), n1 = n(80, 0, { n2 = {} }),
      n2 = n(160, 0, { j = {} }), j = n(200, 0, { k = {}, a = {} }), k = n(240, 0, {}),
      a = n(200, -20, { b = {} }), b = n(480, -20, {}) }
    f.map.nodes = f.nodes
    f.route = { points = { { 0, 0.007 }, { 0, 0.1 } }, dist = 930, dur = 50 }
  end)
  local target = f.target[#f.target]
  T.check(#f.drives == 1 and target.x <= 240 and target.y == 0,
    'the initial gap allowance is consumed before later samples reach a service-road detour')
end

do
  local f = fixture()
  T.check(f.stopDistance and f.stopDistance > 0, 'a loaded final approach tells the stock planner where to brake')
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
  local f = fixture({ 'w', 'j' })
  f.x, f.speed = 98, 0 -- local graph/path ends here, but the GPS destination is still farther ahead
  f.gps.onAiRouteDone(1); f:step(0.1)
  T.check(#f.drives == 1 and f.modes[#f.modes] == 'stop',
    'a completed local endpoint holds instead of immediately restarting the same waypoint')
  for _ = 1, 35 do f:step(2) end
  f:mapArrives(); f:step()
  T.check(#f.drives == 1 and f.gps.autodrive(),
    'stall checks and graph replacement cannot restart an already completed endpoint')
  T.check(not f.gps.noProgressReplans and not f.gps.autoRecoveries,
    'waiting for a continuation does not trigger stuck-vehicle recovery')
  f.path = { 'j', 's', 't' }; f:step()
  T.check(#f.drives == 2 and #f.extensions == 0,
    'a usable continuation resumes through a fresh lane-aware start')
  f.speed = 20 -- moving, but not yet past the progress-distance threshold
  f:step()
  T.check(#f.drives == 2 and not f.gps.noProgressReplans and not f.gps.autoRecoveries,
    'the first update after a long wait does not immediately restart the continuation')
  f:step(f.gps.AUTO_NOPROGRESS_S - 1)
  T.check(#f.drives == 2 and not f.gps.noProgressReplans,
    'a continuation gets a full progress timeout after waiting')
  f:step(2)
  T.check(#f.drives == 3 and f.gps.noProgressReplans == 1 and not f.gps.autoRecoveries,
    'progress recovery still works if the resumed route makes no progress')
end

do
  local f = fixture({ 'w', 'j' }, nil, nil, function(f) f.x = 98 end)
  f:step(f.gps.AUTO_NOPROGRESS_S + 1); f:step()
  T.check(f.gps.noProgressReplans == 1, 'the old path can have a failed progress attempt before completing')
  f.speed = 0
  f.gps.onAiRouteDone(1); f:step()
  f:step(70)
  f.path = { 'j', 's', 't' }; f:step()
  f.speed = 20; f:step()
  f:step(f.gps.AUTO_NOPROGRESS_S + 1)
  T.check(f.gps.noProgressReplans == 2 and not f.gps.autoRecoveries,
    'the continuation does not inherit the old path recovery-attempt count')
end

do
  local f = fixture({ 'w', 'j' })
  f.path = { 'j', 's' }; f.gps.onVehicleMapmgrUpdate(1); f:step()
  T.check(#f.extensions == 1, 'the local path can extend before its completion notification arrives')
  f.x, f.y, f.path = 110, -10, { 's' }
  f.gps.onAiRouteDone(1); f:step(0.1)
  T.check(#f.drives == 2 and f.drives[2][1] == 's',
    'a delayed completion cannot block a single extended waypoint still ahead of the car')
end

do
  local f = fixture({ 'w', 'j' })
  f.x, f.speed = 98, 0
  f.gps.onAiRouteDone(1); f:step(0.1)
  f.gps.setAutodrive(false)
  f.x = 40
  f.gps.setAutodrive(true); f:step()
  T.check(#f.drives == 2, 'explicitly enabling Autodrive clears the completed endpoint')
end

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

-- Projected road endpoints still count when the final target is below the road on a slope.
do
  local f = fixture({ 'w', 'j', { pos = { x = 199.8, y = 0, z = 2.994 }, linkCount = 2 },
    { pos = { x = 199.8, y = 0, z = 1.5 }, fixed = true } }, 2, 6, function(f)
    f.nodes.s.pos = { x = 200, y = 0, z = 3 }
    f.nodes.t.pos = { x = 400, y = 0, z = 6 }
    f.route = { points = { { 0, 0 }, { 0, 0.02 } }, dist = 200, dur = 10 }
  end)
  T.check(table.concat(f.drives[1], ',') == 'j,s', 'GPS hands off the sloped final edge through its projected endpoint')
  f:mapArrives(); f:step()
  T.check(table.concat(f.drives[#f.drives], ',') == 'j,s', 'a map refresh retains the sloped final approach')
  f.x, f.z = 125, 0.75
  f.path = { { pos = { x = 125, y = 0, z = 0.75 }, linkCount = 2 },
    { pos = { x = 199.8, y = 0, z = 2.994 }, linkCount = 2 },
    { pos = { x = 199.8, y = 0, z = 1.5 }, fixed = true } }
  f:mapArrives(); f:step()
  T.check(table.concat(f.drives[#f.drives], ',') == 's' and #f.modes == 0,
    'a map refresh inside the final edge resumes even when the planner removed every waypoint id')
  f.x, f.z = 175, 2.25; f:step()
  T.check(not f.gps.autodrive() and not f.gps.status(), 'the vehicle can reach the normal arrival threshold on the slope')
end

-- A display route can omit a required sub-metre graph node even without a graph replacement.
do
  local function configure(f)
    f.x = 20
    f.nodes = { w = { pos = { x = 0, y = 0, z = 0 }, links = { j = {} }, radius = 6 },
      j = { pos = { x = 40, y = 0, z = 0 }, links = { s = {} }, radius = 6 },
      s = { pos = { x = 40.07, y = 0, z = 0 }, links = { t = {} }, radius = 6 },
      t = { pos = { x = 400, y = 0, z = 0 }, links = {}, radius = 6 } }
    f.map.nodes = f.nodes
    f.route = { points = { { 0, 0 }, { 0, 0.04 } }, dist = 400, dur = 30 }
  end
  local f = fixture({ 'w', 'j', 't' }, 2, 6, configure)
  T.check(table.concat(f.drives[1], ',') == 'j,s,t', 'merged internal node is restored before the first AI start')
  f:mapArrives(); f:step()
  T.check(table.concat(f.drives[#f.drives], ',') == 'j,s,t', 'map replacement retains the restored connection')
  f = fixture({ 'w', 'j' }, 2, 6, configure)
  f.x, f.speed = 39.5, 0
  f.gps.onAiRouteDone(1); f:step(0.1); f:step(35)
  T.check(#f.drives == 1 and f.modes[#f.modes] == 'stop', 'a truly ended local path still waits safely')
  f.path = { 'w', 'j', 't' }; f:step()
  T.check(#f.drives == 2 and table.concat(f.drives[2], ',') == 'j,s,t',
    'a continuation with a merged node resumes instead of waiting at the old endpoint')
  T.check(not f.gps.noProgressReplans and not f.gps.autoRecoveries, 'continuation needs no recovery or progress retry')
end

do
  local f = fixture()
  f.x, f.y = 380, -60
  f:step()
  T.check(not f.gps.autodrive() and f.gps.status() == nil, 'arrival clears Autodrive and its destination')
  T.check(f.modes[#f.modes] == 'stop', 'arrival brakes with the stock AI rather than releasing its controls')
  local modes = #f.modes
  f:step(10)
  T.check(#f.modes == modes, 'clearing the arrived destination does not override the stop command')
  f = fixture()
  f.gps.clear()
  T.check(f.modes[#f.modes] == 'disabled', 'manual route clearing still releases control immediately')
  f = fixture()
  f.gps.setAutodrive(false)
  f.x, f.y = 380, -60; f:step()
  T.check(f.modes[#f.modes] == 'disabled', 'manual driving to the destination does not engage AI braking')
  f = fixture(nil, nil, nil, function(f)
    f.route.points[#f.route.points] = { -0.006, 0.08 }
    f.route.dist = 825
  end)
  T.check(f.stopDistance == nil, 'a temporary graph endpoint does not become a destination braking point')
end

-- Exercise the actual recovery through the no-progress timer, not just edge selection.
do
  local oldSpawn, oldQuat, oldLog = spawn, quatFromDir, log
  local placed
  spawn = { safeTeleport = function(_, p, q) placed = { pos = p, direction = q } end }
  quatFromDir = function(d) return d end
  log = function() end
  local f = fixture({ 'j', 's', 't' }, 0, 6, function(f)
    f.speed = 0
    f.nodes = { w = { pos = { x = 0, y = 0, z = 0 }, links = {}, radius = 6 },
      j = { pos = { x = 100, y = 0, z = 0 }, links = { w = {} }, radius = 6 },
      s = { pos = { x = 300, y = 0, z = 0 }, links = { j = {} }, radius = 6 },
      t = { pos = { x = 500, y = 0, z = 0 }, links = { s = {} }, radius = 6 } }
    f.map.nodes = f.nodes
    f.route = { points = { { 0, 0 }, { 0, 0.05 } }, dist = 500, dur = 30 }
  end)
  f:step(31)
  T.check(not placed, 'the first no-progress interval retries without teleporting')
  f:step(); f:step(31)
  T.check(placed and f.gps.autoRecoveries == 1, 'the second no-progress interval invokes recovery')
  T.check(placed and math.abs(placed.pos.x - 190) < 1e-9 and placed.pos.y == 0,
    'recovery places the vehicle 150 metres ahead on the route')
  T.check(placed and placed.direction.x > 0.99 and placed.direction.y == 0,
    'recovery faces east even though the selected edge is stored westbound')
  spawn, quatFromDir, log = oldSpawn, oldQuat, oldLog
end

for _, name in ipairs({ 'getPlayerVehicle', 'beamngEarth_core', 'map', 'core_groundMarkers', 'vec3' }) do
  _G[name] = saved[name]
end
os.clock, package.loaded['beamngEarth/navgraph'] = savedClock, savedNav
T.done('test_gps_autodrive')
