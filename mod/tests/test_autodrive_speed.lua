local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local A = dofile(dir .. '/../beamng_earth/lua/vehicle/extensions/beamngEarth/autodrive.lua')
local mph = 0.44704
local function near(a, b) return math.abs(a - b) < 1e-9 end
local start, stopDistance
ai = { mode = 'disabled', driveUsingPath = function(args) start = args; ai.mode = 'manual' end,
  setMode = function(mode) ai.mode = mode end, setPath = function() end,
  setStopPoint = function(_, distance) stopDistance = distance end,
  setSpeedMode = function(mode) error('offset must retain legal-speed planning: ' .. mode) end }
local function graph()
  local highway, town = {speedLimit = 75 * mph}, {speedLimit = 35 * mph}
  return {a = {b = highway}, b = {a = highway, c = town}, c = {b = town,
    d = {speedLimit = math.huge}, e = {}, f = {speedLimit = 2 * mph}, g = {speedLimit = 0}}}
end
mapmgr = {mapData = {graph = graph()}, requestMap = function() end}
local g = mapmgr.mapData.graph
local function drive(offset, force, speed) return A.drive({'a','b','c'}, 1, force, speed, .3, offset) end

drive(0)
T.check(near(g.a.b.speedLimit, 75 * mph) and start.routeSpeedMode == 'legal', 'default keeps the road limit')
T.check(drive(5) == 'start' and near(g.a.b.speedLimit, 80 * mph), '+5 replans a 75 mph road at 80 mph')
T.check(near(g.b.c.speedLimit, 40 * mph), 'lower limits ahead receive the same offset')
T.check(start.routeSpeedMode == 'legal' and start.routeSpeed == nil and start.avoidCars == 'on',
  'the native curve, traffic and stopping speed planner remains active')
T.check(drive(5) == 'keep' and near(g.a.b.speedLimit, 80 * mph), 'repeated handoffs never compound the offset')
drive(10)
T.check(near(g.a.b.speedLimit, 85 * mph) and near(g.b.c.speedLimit, 45 * mph), '+10 targets 85 mph and follows lower limits')
drive(-10)
T.check(near(g.a.b.speedLimit, 65 * mph) and near(g.b.c.speedLimit, 25 * mph), 'negative offsets use the original limits')
drive(-100)
T.check(near(g.b.c.speedLimit, 5 * mph), 'large negative offsets keep a positive 5 mph cruise target')
T.check(near(g.c.f.speedLimit, 2 * mph), 'the floor never raises an already lower road limit')
T.check(g.c.d.speedLimit == math.huge and g.c.e.speedLimit == nil and g.c.g.speedLimit == 0,
  'unlimited, missing and zero speed limits are not fabricated')
drive(10)
A.approach(105)
T.check(stopDistance == 100 and near(g.a.b.speedLimit, 85 * mph), 'arrival braking retains the relative speed mode')
mapmgr.mapData = {graph = graph()}
drive(10)
T.check(near(g.a.b.speedLimit, 75 * mph) and near(mapmgr.mapData.graph.a.b.speedLimit, 85 * mph),
  'a streaming graph replacement restores the old graph and offsets the new one once')
g = mapmgr.mapData.graph
drive(0)
T.check(near(g.a.b.speedLimit, 75 * mph), 'resetting the offset restores original limits while driving')
drive(10, true)
T.check(near(g.a.b.speedLimit, 85 * mph), 'forced recovery does not compound the offset')
drive(10, false, 20)
T.check(near(g.a.b.speedLimit, 75 * mph) and start.routeSpeed == 20 and start.routeSpeedMode == 'set',
  'the performance tool absolute override still takes precedence')
for _, action in ipairs({'reset', 'onReset', 'onExtensionUnloaded', 'arrive', 'stop'}) do
  drive(10)
  A[action]()
  T.check(near(g.a.b.speedLimit, 75 * mph), action .. ' restores the vehicle road limits')
end
drive(10)
T.check(A.drive({'missing'}, 1, false, nil, .3, 10) == 'waiting' and near(g.a.b.speedLimit, 75 * mph),
  'waiting for a missing road restores limits while stopped')
T.done('test_autodrive_speed')
