-- luajit mod/tests/test_heat.lua  (police heat: plates and looks remembered, chase build-up, footage, busted)
local T = dofile((arg[0]:match('^(.*)[/\\]') or '.') .. '/testlib.lua')
local saved, msgs = nil, {}
jsonWriteFile = function(_, t) saved = t end
jsonReadFile = function() return nil end
guihooks = { trigger = function(_, m) msgs[#msgs + 1] = m.msg end }
local W = require('beamngEarth/wallet')
local H = require('beamngEarth/heat')

T.check(H.colourKey('0.81234 0.1 0.1 1.2') == '0.8 0.1 0.1', 'colour key: rgb to tenths')
T.near(H.build(0, 1, 60), 21, 1e-9, 'a minute at level 1')
T.near(H.build(0, 3, 60), 63, 1e-9, 'a minute at level 3: three times')
T.near(H.build(90, 3, 600), 100, 1e-9, 'capped')
T.check(H.footage(0, 1) == 150 and H.footage(120, 2) == 540, 'footage: longer and higher pays more')
local b = H.busted(10)
T.check(b.fine == 270 and b.impound == 0, 'low heat: a fine only (' .. b.fine .. ')')
b = H.busted(80)
T.check(b.fine == 1110 and b.impound == H.IMPOUND_FEE and b.total == 1110 + 750, 'high heat: bigger fine and impound')
T.check(H.busted(40).fine > H.busted(20).fine, 'more heat, bigger fine')
local id = { plate = 'ABC123', look = 'pickup|0.8 0.1 0.1' }
T.near(H.heatOf(id, { ABC123 = 40 }, {}), 40, 1e-9, 'plate known')
T.near(H.heatOf({ plate = 'NEW1', look = id.look }, { ABC123 = 40 }, { [id.look] = 40 }), 20, 1e-9,
  'new plate, same look: half')
T.near(H.heatOf({ plate = 'NEW1', look = 'pickup|0.1 0.1 0.8' }, { ABC123 = 40 }, { [id.look] = 40 }), 0, 1e-9,
  'new plate and colour: a new car')

-- a chase, an escape, the plate remembered, then busted later
local plate, colour = 'ABC123', '0.8 0.1 0.1 1.2'
local veh = { JBeam = 'pickup', getDynDataFieldbyName = function() return plate end,
              getField = function() return colour end }
local mode = 2
be = { getPlayerVehicleID = function() return 5 end, getPlayerVehicle = function() return veh end }
local started = {}
gameplay_police = { getPursuitData = function() return { mode = mode } end,
                    getNearestPoliceVehicle = function() return 9, 30 end,
                    setPursuitMode = function(m) started[#started + 1] = m end }
W.add(-W.balance(), 'reset')
H._reset()
H.onPursuitAction(5, 'start', { mode = 2 })
for _ = 1, 60 do H.update(1) end
T.near(H._chase().heat, 42, 1e-6, 'a minute at level 2: 42 heat')
H.onPursuitAction(5, 'evade', {})
T.near(W.balance(), H.footage(60, 2), 1e-9, 'footage sold')
T.near(H._state().plates.ABC123, 42, 1e-6, 'the plate remembered')
H.update(1)
T.check(#started == 1 and started[1] == 1, 'seen by a cop: a chase starts at once')
colour = '0.1 0.1 0.8 1.2'; plate = 'ZZZ999'
started = {}
H.update(1)
T.check(#started == 0, 'new paint and plates: not recognised')
colour = '0.8 0.1 0.1 1.2'
H.update(1)
T.check(#started == 1, 'new plates, same paint: half the heat, still known (21 >= 15)')
plate = 'ABC123'
H.onPursuitAction(5, 'start', { mode = 1 })
local before = W.balance()
mode = 3
for _ = 1, 30 do H.update(1) end      -- 42 + 30 s x 3 x 0.35 = 73.5: impound
H.onPursuitAction(5, 'arrest', {})
T.near(before - W.balance(), H.busted(73.5).total, 1e-6, 'busted: fine and impound fee')
T.check(H._state().plates.ABC123 == 0, 'heat cleared after paying')
H.onPursuitAction(7, 'start', {})
T.check(H._chase() == nil, 'another car\'s chase: ignored')
T.done('test_heat')
