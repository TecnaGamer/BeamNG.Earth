-- luajit mod/tests/test_busline.lua  (driving a real bus line: busline.lua's stops served, missed and skipped, the line
-- on the city bus's signs; the user, 2026-11-17: "yes start the bus lines")
local T = dofile((arg[0]:match('^(.*)[/\\]') or '.') .. '/testlib.lua')
local dir = arg[0]:match('^(.*)[/\\]') or '.'
dofile(dir .. '/stubs.lua')

local B = require('beamngEarth/busline')

-- a world where lat/lon are metres (lat * 1000 = y), a bus the test moves, and what the bus and the player hear
local pos, vel, jbeam, cmds, said, routed, cleared = { x = 0, y = 0, z = 5 }, { x = 0, y = 0, z = 0 }, 'citybus', {}, {}, {}, 0
serialize = function() return '{}' end
local veh = { getPosition = function() return pos end, getVelocity = function() return vel end,
  getJBeamFilename = function() return jbeam end, getID = function() return 7 end,
  queueLuaCommand = function(_, s) cmds[#cmds + 1] = s end }
B.vehicle = function() return veh end
B.api = function() return { toGame = function(lat, lon) return lon * 1000, lat * 1000, 0 end,
  position = function() return 0, 0 end } end
B.gps = function() return { setRoute = function(p) routed[#routed + 1] = p end,
  setDestination = function(la, lo, n) routed[#routed + 1] = { { lat = la, lon = lo, name = n } } end,
  clear = function() cleared = cleared + 1 end } end
B.say = function(t) said[#said + 1] = t end
local function lastSaid() return said[#said] or '' end
local function heard(event) local n = 0 for _, c in ipairs(cmds) do if c:find(event, 1, true) then n = n + 1 end end return n end

local function line()
  return { id = 42, ref = '64', to = 'Fyrktorget', colour = '#007f52', near_i = 0, stops = {
    { name = 'Vasaplatsen', lat = 0, lon = 0, on_road = true },
    { name = 'Kapellplatsen', lat = 0.2, lon = 0, on_road = true },
    { name = 'Chalmers', lat = 0.4, lon = 0, on_road = false },
    { name = 'Wavrinskys Plats', lat = 0.6, lon = 0, on_road = true },
    { name = 'Fyrktorget', lat = 0.8, lon = 0, on_road = true } } }
end
local function at(y, moving) pos = { x = 0, y = y, z = 5 }; vel = { x = 0, y = moving and 10 or 0, z = 0 } end
local function wait(s) for _ = 1, math.floor(s / B.TICK_S + 0.5) do B.step(B.TICK_S) end end
local function doors(open) B.onBusUpdate({ event = 'onTriggerTick', triggerName = B.triggerName(B.running().line,
  B.running().idx), bus_dooropen = open }) end

-- the line on the signs: destination folded to ASCII, the next stops in order
T.check(B.ascii('Östra Sjukhuset') == 'Ostra Sjukhuset', 'the destination sign has no Ö')
T.check(B.start(line()), 'start')
T.check(heard('bus_setLineInfo') == 1, 'the line went to the bus')
local info = B.lineInfo(B.running(), 5)
T.check(info.routeID == '64' and info.direction == 'Fyrktorget' and info.routeColor == '#007f52', 'route, sign, colour')
T.check(#info.tasklist == 5 and info.tasklist[2][2] == 'Kapellplatsen' and info.tasklist[2][3][2] == 200,
  'stops in order, at their places')
T.check(#routed[1] == 5 and routed[1][1].name == 'Vasaplatsen', 'the GPS through the stops')

-- a bus standing at the stop with the doors shut: not yet; doors open DWELL_S: served
at(5, false) wait(1)
T.check(B.running().served == 0, 'doors shut: not served')
doors(true) wait(B.DWELL_S)
T.check(B.running().served == 1 and heard('bus_onAtStop') == 1, 'doors open, standing: served')
T.check(lastSaid():find('Vasaplatsen', 1, true), 'said the stop')
at(60, true) wait(0.25)
T.check(B.running().idx == 2 and heard('bus_onDepartedStop') == 1, 'left it: next stop')
T.check(heard('bus_setLineInfo') == 2 and #B.lineInfo(B.running(), 5).tasklist == 4, 'the signs from the next stop on')

-- driving through a stop without stopping: missed
at(200, true) wait(0.5) at(240, true) wait(0.25)
T.check(B.running().missed == 1 and B.running().idx == 3 and lastSaid():find('Missed stop: Kapellplatsen', 1, true),
  'drove through: missed')

-- at a later stop without reaching this one (a platform-only stop, its wider radius): the ones before are missed
at(600, true) wait(0.25)
T.check(B.running().idx == 4 and B.running().missed == 2, 'at the next stop: Chalmers missed')
T.check(B.PLATFORM_RADIUS_M > B.STOP_RADIUS_M, 'a platform-only stop counts from farther')

-- the last stop: done, the GPS cleared
at(600, false) doors(true) wait(B.DWELL_S) at(700, true) wait(0.25)
at(800, false) doors(true) wait(B.DWELL_S)
at(900, true) wait(0.25)
T.check(B.running() == nil and cleared == 1, 'line done')
T.check(lastSaid():find('3 of 5 stops served, 2 missed', 1, true), 'the summary: ' .. lastSaid())

-- any other car: standing is enough
jbeam = 'etk800'
T.check(B.start(line(), 1), 'start in a car')
at(0, false) wait(B.DWELL_S)
T.check(B.running().served == 1, 'a car: standing serves the stop')
B.stop()
T.check(B.running() == nil, 'stopped')

-- the lines' answer: only the one asked for
B._reset()
local asked = {}
B.core = function() return { requestBusLines = function(id, la, lo) asked[#asked + 1] = id return true end } end
T.check(B.request(), 'asked')
B.onLines({ id = 999, lines = { line() } })
T.check(B.lines() == nil, 'another answer: ignored')
B.onLines({ id = asked[1], lines = { line() }, attribution = 'x' })
T.check(#B.lines() == 1, 'the answer')
B.core = function() return { requestBusLines = function() return false end } end
T.check(not B.request() and select(2, B.lines()) == 'Companion server not connected', 'not connected: said')

T.done('test_busline')
