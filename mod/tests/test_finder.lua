-- luajit mod/tests/test_finder.lua  (the station finder: the car's kind of energy, the low fuel prompt's state, Nearest
-- through the stations message and the GPS; the user, 2026-11-09: "nearest button is nice as well, if we can do a quick
-- action outside map too when fuel get low would be sick as well")
local T = dofile((arg[0]:match('^(.*)[/\\]') or '.') .. '/testlib.lua')
local dir = arg[0]:match('^(.*)[/\\]') or '.'
dofile(dir .. '/stubs.lua')

local F = require('beamngEarth/finder')

-- the car's kind and level
local petrol = { name = 'mainTank', energyType = 'gasoline', currentEnergy = 10, maxEnergy = 50 }
local petrol2 = { name = 'aux', energyType = 'gasoline', currentEnergy = 0, maxEnergy = 50 }
local battery = { name = 'mainBattery', energyType = 'electricEnergy', currentEnergy = 30, maxEnergy = 100 }
local nitrous = { name = 'n2o', energyType = 'n2o', currentEnergy = 1, maxEnergy = 2 }
T.check(F.kindOf({ petrol }) == 'fuel', 'petrol: fuel')
T.check(F.kindOf({ battery }) == 'ev', 'battery only: ev')
T.check(F.kindOf({ battery, petrol }) == 'fuel', 'a hybrid counts as fuel')
T.check(F.kindOf({ nitrous }) == nil, 'nitrous alone: nothing to find')
T.check(F.kindOf({}) == nil, 'no tanks')
T.near(F.level({ petrol, petrol2 }, 'fuel'), 0.1, 1e-9, 'two petrol tanks')
T.near(F.level({ battery, petrol }, 'ev'), 0.3, 1e-9, 'the battery for ev')
T.check(F.level({ nitrous }, 'fuel') == nil, 'no tank of the kind')

-- the low fuel prompt: once below LOW, again only after REARM or another car
local st, p = {}, nil
st, p = F.lowFuelStep(st, 0.5, 1)  T.check(not p, '50 %: quiet')
st, p = F.lowFuelStep(st, 0.14, 1) T.check(p, '14 %: prompt')
st, p = F.lowFuelStep(st, 0.10, 1) T.check(not p, '10 %: said already')
st, p = F.lowFuelStep(st, 0.20, 1) T.check(not p, '20 %: not re-armed yet')
st, p = F.lowFuelStep(st, 0.12, 1) T.check(not p, '12 %: still said')
st, p = F.lowFuelStep(st, 0.26, 1) T.check(not p, '26 %: re-armed, quiet')
st, p = F.lowFuelStep(st, 0.12, 1) T.check(p, '12 % after a refuel: prompt again')
st, p = F.lowFuelStep(st, 0.10, 2) T.check(p, 'another car low: prompt')
st, p = F.lowFuelStep(st, nil, 2)  T.check(not p, 'no level: quiet')

-- Nearest: the stations message, then the GPS to the first item
local sent, connected, dest, msgs = {}, true, nil, {}
local core = { requestStations = function(id, lat, lon, rKm, kinds, mode)
  if not connected then return false end
  sent[#sent + 1] = { id = id, lat = lat, lon = lon, rKm = rKm, kinds = kinds, mode = mode }
  return true
end, _state = function() return { inOurLevel = true, player = { lat = 34.85, lon = -117.08 } } end }
F.core = function() return core end
F.gps = { setDestination = function(lat, lon, name) dest = { lat = lat, lon = lon, name = name } return true end,
          status = function() return dest end }
F.say = function(text) msgs[#msgs + 1] = text end
local clock = 0
F.now = function() return clock end
F.carKind = function(cb) cb('ev', 0.5) end

F.nearest()
T.check(#sent == 1 and sent[1].mode == 'near' and sent[1].rKm == 25 and sent[1].kinds[1] == 'ev',
  'Nearest asks near, 25 km, the car\'s kind')
F.onStations({ id = sent[1].id, items = { { lat = 34.849, lon = -117.085, name = 'Tesla Supercharger', kind = 'ev', km = 0.4 } },
               partial = false })
T.check(dest and dest.lat == 34.849 and dest.name == 'Tesla Supercharger', 'the GPS set to the first item')
T.check(msgs[#msgs]:find('Tesla Supercharger', 1, true) ~= nil, 'said where: ' .. tostring(msgs[#msgs]))

-- partial answers: asked again until done; the best so far when the time runs out
sent, dest = {}, nil
F.nearest()
F.onStations({ id = sent[1].id, items = {}, partial = true })
T.check(dest == nil, 'partial and nothing yet: waiting')
clock = clock + F.POLL_S + 0.01
F.onUpdate(0.1)
T.check(#sent == 2, 'asked again after a partial answer')
F.onStations({ id = sent[2].id, items = { { lat = 1, lon = 2, name = '', kind = 'ev', km = 9 } }, partial = true })
clock = clock + F.NEAR_TIMEOUT_S + 1
F.onUpdate(0.1)
T.check(dest and dest.lat == 1 and dest.name == 'EV charger', 'time out: the best so far, an unnamed charger named')

-- nothing near; not connected (Review Focus 4, 5)
sent, dest = {}, nil
F.nearest()
F.onStations({ id = sent[1].id, items = {}, partial = false })
T.check(dest == nil and msgs[#msgs] == 'No station within 25 km', 'none: ' .. tostring(msgs[#msgs]))
connected = false
F.nearest()
T.check(msgs[#msgs] == 'Companion server not connected', 'not connected: ' .. tostring(msgs[#msgs]))
connected = true

-- an answer nobody waits for (the world map's) is kept for take
F.onStations({ id = 9999, items = { { lat = 1, lon = 1, kind = 'fuel', km = 1 } }, partial = false })
local r = F.take(9999)
T.check(r and #r.items == 1 and F.take(9999) == nil, 'the map\'s answer taken once')

-- the low fuel watch: every CHECK_S, only with the setting on
local asked = 0
F.carKind = function(cb) asked = asked + 1 cb('fuel', 0.1) end
F.promptOn = function() return false end
clock = clock + 100
F.onUpdate(0.1)
T.check(asked == 0, 'setting off: the gauge is not read')
F.promptOn = function() return true end
sent, msgs, dest = {}, {}, nil
F.onUpdate(0.1)
T.check(asked == 1, 'setting on: the gauge read')
F.onUpdate(0.1)
T.check(asked == 1, 'not again before CHECK_S')
T.check(#sent == 1 and sent[1].kinds[1] == 'fuel', 'low: the nearest asked')
F.onStations({ id = sent[1].id, items = { { lat = 5, lon = 6, name = 'Sheetz', kind = 'fuel', km = 2.34 } }, partial = false })
T.check(dest == nil, 'the prompt does not route by itself')
T.check(msgs[#msgs] and msgs[#msgs]:find('Fuel low: nearest station 2.3 km', 1, true) ~= nil, 'prompt: ' .. tostring(msgs[#msgs]))
-- a route to a station already set: no prompt
F.lowState = {}
F.stationDest = { lat = 5, lon = 6 }
dest = { lat = 5, lon = 6 }
sent, msgs = {}, {}
clock = clock + F.CHECK_S + 1
F.onUpdate(0.1)
T.check(#sent == 0 and #msgs == 0, 'already routed to a station: quiet')

-- the radial menu's entry: an action in the stock "Other" ring, never on the top level (whose items are categories with
-- a goto: an action there broke the menu's navigation in game, "attempt to index field 'goto'", 2026-11-09)
local added
core_quickAccess = { addEntry = function(a) added = a end }
F.onQuickAccessLoaded()
T.check(added and added.level == '/root/sandbox/other/', 'the Other ring: ' .. tostring(added and added.level))
T.check(added.uniqueID == 'beamngEarthFinder', 'a uniqueID: registered again (an extension reload) it replaces itself')
local entries = {}
added.generator(entries)
T.check(#entries == 1 and type(entries[1].onSelect) == 'function' and entries[1].title == 'Nearest station',
  'one Nearest station action')
core_quickAccess = nil

T.done('test_finder')
