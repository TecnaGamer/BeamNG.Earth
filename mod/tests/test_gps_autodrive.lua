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

for _, name in ipairs({ 'getPlayerVehicle', 'beamngEarth_core', 'map', 'core_groundMarkers', 'vec3' }) do
  _G[name] = saved[name]
end
os.clock, package.loaded['beamngEarth/navgraph'] = savedClock, savedNav
T.done('test_gps_autodrive')
