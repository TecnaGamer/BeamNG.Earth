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
do
  local savedObj, savedMap = obj, mapmgr.mapData
  local function roads()
    local md = graph()
    md.positions = { a = {x=0,y=0,z=0}, b = {x=100,y=0,z=0}, c = {x=200,y=0,z=0}, d = {x=300,y=0,z=0} }
    md.radius = {a=4,b=4,c=4,d=4}
    return md
  end
  local md, ids, heading = roads(), {'b','c','d'}, {x=1,y=0,z=0}
  T.check(same(A.forwardStart(ids, md, {x=102,y=2,z=0}, heading), 'c,d') and same(ids, 'b,c,d'),
    'trim a passed prefix without mutating the requested route')
  T.check(same(A.forwardStart(ids, md, {x=98,y=2,z=0}, heading), 'b,c,d'), 'a waypoint ahead remains mandatory')
  T.check(same(A.forwardStart(ids, md, {x=100,y=2,z=0}, heading), 'c,d'), 'a waypoint at the front plane is reached')
  T.check(same(A.forwardStart(ids, md, {x=102,y=6,z=0}, heading), 'b,c,d'), 'a nearby parallel road does not count as passing')
  T.check(same(A.forwardStart(ids, md, {x=102,y=2,z=4}, heading), 'b,c,d'), 'a road on another level does not count as passing')
  T.check(same(A.forwardStart(ids, md, {x=102,y=2,z=0}, {x=-1,y=0}), 'b,c,d'),
    'an intended turn back to the waypoint is not removed')
  md.graph.d.b, md.graph.b.d = {}, {}
  T.check(same(A.forwardStart({'b','c','d','b'}, md, {x=102,y=2,z=0}, heading), 'c,d,b'),
    'a later planned return to the starting waypoint remains in the route')
  md.positions.c, md.positions.d = {x=100,y=100,z=0}, {x=100,y=200,z=0}
  T.check(same(A.forwardStart(ids, md, {x=98,y=2,z=0}, heading), 'b,c,d'), 'a corner still ahead cannot be cut')
  md.positions.c, md.positions.d = {x=100.01,y=0,z=0}, {x=200,y=0,z=0}
  T.check(same(A.forwardStart(ids, md, {x=102,y=2,z=0}, heading), 'd'), 'a passed short connector can be skipped through its forward continuation')
  md = roads(); md.graph.b.c = {inNode='c',inRadius=8,outRadius=2}
  T.check(same(A.forwardStart(ids, md, {x=102,y=3,z=0}, heading), 'b,c,d'), 'reverse-stored edge radii keep their orientation')
  md.positions.c = {x=100,y=0,z=0}
  T.check(same(A.forwardStart(ids, md, {x=102,y=2,z=0}, heading), 'b,c,d'), 'a degenerate edge is left intact')
  obj = { getFrontPosition = function() return {x=102,y=2,z=1} end,
    getPosition = function() return {x=98,y=2,z=0} end, getDirectionVector = function() return heading end }
  mapmgr.mapData = roads(); A.reset()
  A.drive({'b','c','d'}, 6, false, nil, .3)
  T.check(same(starts[#starts].wpTargetList, 'c,d'),
    'a fresh handoff targets ahead of the native AI front even while the body reference is behind the waypoint')
  T.check(A.drive({'b','c','d'}, 6, false, nil, .3) == 'keep',
    'a repeated request does not reintroduce the passed first waypoint')
  mapmgr.mapData = roads()
  A.drive({'b','c'}, 6, false, nil, .3)
  T.check(same(starts[#starts].wpTargetList, 'c'), 'a map replacement starts on the forward suffix')
  A.drive({'b'}, 7, true, nil, .3)
  T.check(same(starts[#starts].wpTargetList, 'b'), 'the final single waypoint remains available for arrival')
  obj, mapmgr.mapData = savedObj, savedMap; A.reset()
end
local stopDistance, speedMode
ai.setStopPoint = function(_, distance) stopDistance = distance end
ai.setSpeedMode = function(mode) speedMode = mode end
A.drive({'b','c'}, 4, true, 20, .3)
A.approach(105)
T.check(stopDistance == 100 and speedMode == 'limit', 'a selected speed no longer overrides the final stopping profile')
speedMode = nil
A.drive({'b','c'}, 5, true, nil, .3)
A.approach(50)
T.check(stopDistance == 45 and speedMode == nil, 'normal approaches retain legal-speed planning')
A.approach(2)
T.check(stopDistance == 0, 'a very near destination does not request a stop behind the car')
ai.mode = 'disabled'; stopDistance = nil
A.approach(50)
T.check(stopDistance == nil, 'an approach update cannot restart a stopped local path')
A.arrive()
T.check(stops[#stops - 1] == 'manual' and stops[#stops] == 'stop',
  'arrival activates braking with no old target to turn back toward')
A.drive({'b','c'}, 5, false, nil, .3)
T.check(starts[#starts].routeSpeedMode == 'legal', 'a new drive releases arrival control and restores its speed mode')
A.stop('disabled')
T.check(stops[#stops] == 'disabled', 'manual cancellation still releases control')
T.done('test_autodrive_handoff')
