-- luajit mod/tests/test_gps_autodrive.lua
-- Exercise the public GPS update lifecycle, including the production route-joining call.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local saved = {}
for _, name in ipairs({ 'getPlayerVehicle', 'beamngEarth_core', 'map', 'core_groundMarkers', 'vec3' }) do
  saved[name] = rawget(_G, name)
end
local savedClock, savedNav = os.clock, package.loaded['beamngEarth/navgraph']

local function fixture(path, y, radius)
  local G = dofile(dir .. '/../beamng_earth/lua/ge/extensions/beamngEarth/gps.lua')
  local f = { gps = G, now = 0, serial = 1, drives = {}, extensions = {}, modes = {}, arrows = 0, requests = 0,
    healthy = true, speed = 20, x = 40, y = y or 2, path = path or { 'j', 's', 't' } }
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
  f.anchor = { geodeticToEnu = function(_, lat, lon) return lon * 10000, lat * 10000, 0 end }
  os.clock = function() return f.now end
  package.loaded['beamngEarth/navgraph'] = { applies = function() return f.serial end }
  vec3 = function(x, y, z) return { x = x, y = y, z = z } end
  map = { getMap = function() return f.map end }
  getPlayerVehicle = function() return {
    getId = function() return 1 end,
    getPosition = function() return { x = f.x, y = f.y, z = 0 } end,
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
    G.update(self.anchor, { x = self.x, y = self.y, lat = self.y / 10000, lon = self.x / 10000,
      vx = self.speed, vy = 0 })
  end
  local goal = f.route.points[#f.route.points]
  G.setDestination(goal[1], goal[2], 'test destination')
  G.setAutodrive(true)
  f:mapArrives()
  f:step() -- request the route
  f:step() -- accept it and drive the planner's path
  return f
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

for _, name in ipairs({ 'getPlayerVehicle', 'beamngEarth_core', 'map', 'core_groundMarkers', 'vec3' }) do
  _G[name] = saved[name]
end
os.clock, package.loaded['beamngEarth/navgraph'] = savedClock, savedNav
T.done('test_gps_autodrive')
