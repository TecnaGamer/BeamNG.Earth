local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local A = dofile(dir .. '/../beamng_earth/lua/vehicle/extensions/beamngEarth/autodrive.lua')
local function same(a, b) return a and table.concat(a, ',') == b end

T.check(same(A.extension({'a','b','c'}, {'b','c','d'}), 'c,d'), 'send only the suffix starting at the old endpoint')
T.check(same(A.extension({'a','b','c'}, {'b','c'}), 'c'), 'identical remaining route needs no resubmission')
T.check(A.extension({'a','b','c'}, {'a','b'}) == nil, 'a nearer destination is a replacement, not an extension')
T.check(A.extension({'a','b','c'}, {'b','x','c','d'}) == nil, 'sharing an endpoint is insufficient when the path forks')
T.check(A.extension({'a','b','a'}, {'a','d'}) == nil, 'an ambiguous loop is not extended')
T.check(A.extension({'a','b'}, {'b','c','b'}) == nil, 'a new loop is not extended')
T.check(A.extension({'a','b','c'}, {'b','c','a'}) == nil, 'an extension cannot loop through a previously passed node')
T.check(A.extension({'a','b'}, {'x','y'}) == nil, 'a disconnected path is not an extension')

local starts, suffixes, stops, requests = {}, {}, {}, 0
ai = { mode = 'disabled', driveUsingPath = function(args)
  starts[#starts + 1] = args; ai.mode = 'manual'
end, setPath = function(ids) suffixes[#suffixes + 1] = ids end,
  setMode = function(mode) stops[#stops + 1] = mode; ai.mode = mode end }
local function graph()
  return {graph = {a={b={}}, b={a={},c={}}, c={b={},d={}}, d={c={}}}}
end
mapmgr = { mapData = graph(), requestMap = function() requests = requests + 1 end }

T.check(A.drive({'a','b','c'}, 1, false, nil, .3) == 'start', 'first request starts the AI')
T.check(starts[1].wpTargetList and not starts[1].path and starts[1].driveInLane == 'on'
  and starts[1].avoidCars == 'on' and starts[1].routeSpeedMode == 'legal', 'start retains lane and traffic awareness through waypoint input')
T.check(A.drive({'b','c'}, 1, false, nil, .3) == 'keep' and #starts == 1 and #suffixes == 0,
  'advancing along the same route preserves the existing plan')
T.check(A.drive({'b','c','d'}, 1, false, nil, .3) == 'extend' and same(suffixes[1], 'c,d'),
  'compatible extension uses the engine append contract without resetting mode')
T.check(A.drive({'c','d'}, 1, false, nil, .3) == 'keep', 'tracking remains correct after an extension')
mapmgr.mapData = graph()
T.check(A.drive({'c','d'}, 1, false, nil, .3) == 'start', 'same ids on a replacement map require a fresh start')
T.check(A.drive({'c','d'}, 2, false, nil, .3) == 'start', 'a changed destination/route version starts afresh')
T.check(A.drive({'c','d'}, 2, true, nil, .3) == 'start', 'stall recovery bypasses duplicate suppression')
T.check(A.drive({'c','d'}, 2, false, 20, .3) == 'start' and starts[#starts].routeSpeed == 20,
  'changed speed settings are applied')
ai.mode = 'stop'
T.check(A.drive({'c','d'}, 2, false, 20, .3) == 'start', 'resume from stop cannot be mistaken for a live plan')
A.onReset()
T.check(A.drive({'c','d'}, 2, false, 20, .3) == 'start', 'a reset vehicle cannot reuse stale handoff state')
T.check(A.drive({'a','d'}, 2, false, nil, .3) == 'waiting' and stops[#stops] == 'stop' and requests == 1,
  'a missing vehicle-graph edge holds and requests the graph rather than crashing AI')
mapmgr.mapData.graph.c.d = {oneWay=true,inNode='d'}
T.check(A.drive({'c','d'}, 2, false, nil, .3) == 'waiting', 'vehicle-side validation rejects reverse one-way steps')
A.stop('disabled')
T.check(ai.mode == 'disabled', 'disable clears helper state and disables AI')

mapmgr.mapData = graph()
T.check(A.drive({'b'}, 3, false, nil, .3) == 'start' and same(starts[#starts].wpTargetList, 'b'),
  'one forward waypoint starts through the stock waypoint API')
local count = #starts
T.check(A.drive({'b'}, 3, false, nil, .3) == 'keep' and #starts == count,
  'an unchanged single waypoint preserves the active plan')
T.check(A.drive({'b','c'}, 3, false, nil, .3) == 'extend' and same(suffixes[#suffixes], 'b,c'),
  'a single waypoint can extend through its endpoint')
T.check(A.drive({'c'}, 3, false, nil, .3) == 'keep', 'a live path can reduce to its last waypoint')
mapmgr.mapData = graph()
T.check(A.drive({'c'}, 3, false, nil, .3) == 'start', 'one waypoint restarts after map replacement')
T.check(A.drive({}, 3, false, nil, .3) == 'waiting', 'an empty waypoint list still holds')
T.check(A.drive({'missing'}, 3, false, nil, .3) == 'waiting', 'an unknown single waypoint still holds')
T.done('test_autodrive_handoff')
